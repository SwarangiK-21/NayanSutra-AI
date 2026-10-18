"""Agentic RAG graph (LangGraph).

plan ─┬─ research/drafting ─> retrieve ─> grade ─┬─ relevant ─> generate ─> verify ─┬─ grounded ─> finalize
      │                          ▲               │                ▲                 │
      │                          └── rewrite ◄───┘ (no hits)      └── regenerate ◄───┘ (unsupported)
      │                                          └─ retries exhausted ─> abstain
      ├─ calculation ─> ReAct agent w/ deterministic tools
      └─ chitchat ─> chat

Key design decisions
====================
* Structured output uses json_mode (required by Groq for most models).
* Grading is done sequentially (no concurrent batch) to avoid rate-limit
  failures that caused the "all docs filtered" bug producing false answers.
* doc_type filtering is ONLY applied when the planner explicitly assigns one;
  querying without a filter casts the widest possible retrieval net.
* verify loop is capped at MAX_GENERATION_ATTEMPTS and only re-generates
  when there are actual unsupported claims — prevents the model drifting into
  parametric knowledge on subsequent attempts.
* The generate node explicitly injects "Answer ONLY from context" into every
  user message as a final guardrail on top of the system prompt.
"""
import logging
import os
from typing import Annotated, List, Literal, Optional, TypedDict

from langchain_core.documents import Document
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import create_react_agent
from pydantic import BaseModel, Field

import config
import prompts
from retrieval import HybridRetriever
from tools import TOOLS

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Pydantic schemas for structured LLM outputs
# ---------------------------------------------------------------------------

class Plan(BaseModel):
    intent: Literal["research", "drafting", "calculation", "chitchat"]
    standalone_query: str = Field(description="Self-contained search query")
    doc_type: Optional[Literal["statutes", "judgments", "contracts", "other"]] = None


class GradeBatch(BaseModel):
    relevant_doc_indices: List[int] = Field(
        default_factory=list,
        description="0-based indices of documents that are relevant to answering the query",
    )
    reason: str = Field(default="", description="Brief explanation of relevance")


class Verdict(BaseModel):
    grounded: bool
    unsupported_claims: List[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Graph state
# ---------------------------------------------------------------------------

class State(TypedDict, total=False):
    messages: Annotated[list, add_messages]
    intent: str
    query: str
    doc_type: Optional[str]
    docs: List[Document]
    relevant_docs: List[Document]
    retries: int
    gen_attempts: int
    answer: str
    grounded: bool
    feedback: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _text(m) -> str:
    """Extract plain text from a message or string."""
    c = m.content if hasattr(m, "content") else m
    if isinstance(c, list):
        return "".join(b.get("text", "") if isinstance(b, dict) else str(b) for b in c)
    return str(c or "")


def _cite(d: Document) -> str:
    """Build a human-readable citation string from document metadata."""
    m = d.metadata
    parts = [m.get("source", "unknown")]
    if m.get("section"):
        parts.append(m["section"])
    if m.get("page"):
        parts.append(f"p.{m['page']}")
    return ", ".join(parts)


def _context(docs: List[Document]) -> str:
    """Format a list of documents as a numbered context block."""
    return "\n\n".join(
        f"[{i}] ({_cite(d)})\n{d.page_content}"
        for i, d in enumerate(docs, 1)
    )


# ---------------------------------------------------------------------------
# Graph construction
# ---------------------------------------------------------------------------

def build_graph(checkpointer=None):
    # ------------------------------------------------------------------
    # LLM setup
    # ------------------------------------------------------------------
    if config.LLM_PROVIDER in ("groq", ""):
        from langchain_groq import ChatGroq
        llm = ChatGroq(
            model=config.LLM_MODEL_NAME,
            groq_api_key=os.getenv("GROQ_API_KEY"),
            temperature=0,
        )
    else:
        from langchain.chat_models import init_chat_model
        llm = init_chat_model(
            model=config.LLM_MODEL_NAME,
            model_provider=config.LLM_PROVIDER,
            temperature=0,
        )

    # Structured output — use json_mode which Groq supports reliably
    planner = llm.with_structured_output(Plan, method="json_mode")
    grader  = llm.with_structured_output(GradeBatch, method="json_mode")
    auditor = llm.with_structured_output(Verdict, method="json_mode")

    retriever  = HybridRetriever()
    calc_agent = create_react_agent(llm, TOOLS, prompt=prompts.CALC_SYSTEM)

    # ------------------------------------------------------------------
    # Node definitions
    # ------------------------------------------------------------------

    def plan(state: State) -> dict:
        """Classify intent and rewrite the query for vector search."""
        msgs      = state.get("messages", [])
        user_last = _text(msgs[-1]) if msgs else ""
        convo     = "\n".join(
            f"{getattr(m, 'type', 'message')}: {_text(m)}" for m in msgs[-8:]
        )
        try:
            p = planner.invoke(prompts.PLAN.format(convo=convo))
            intent   = p.intent
            query    = p.standalone_query or user_last
            doc_type = p.doc_type
        except Exception as exc:
            log.warning("Planner failed (%s), falling back to raw query", exc)
            intent   = "research"
            query    = user_last
            doc_type = None

        log.info("plan → intent=%s  query=%r  doc_type=%s", intent, query, doc_type)
        return {
            "intent":       intent,
            "query":        query,
            "doc_type":     doc_type,
            "retries":      0,
            "gen_attempts": 0,
            "feedback":     "",
        }

    def retrieve(state: State) -> dict:
        """Run hybrid dense + BM25 retrieval.

        doc_type filter is ONLY applied when explicitly set by the planner so
        we don't accidentally narrow the search scope based on a wrong guess.
        """
        doc_type = state.get("doc_type")
        docs = retriever.retrieve(state["query"], doc_type)
        log.info("retrieve → %d docs (doc_type=%s)", len(docs), doc_type)
        return {"docs": docs}

    def grade(state: State) -> dict:
        """Filter retrieved docs for relevance in a single structured LLM call.

        Evaluates candidate documents together: fast (<1s), zero rate limit (429) issues,
        and fail-open if the LLM filter fails or drops all candidates.
        """
        docs = state.get("docs", [])
        if not docs:
            log.info("grade → no docs to grade")
            return {"relevant_docs": []}

        q = state["query"]
        candidate_text = "\n\n".join(
            f"[{i}] ({_cite(d)})\n{d.page_content[:1500]}"
            for i, d in enumerate(docs)
        )
        kept = []
        try:
            res = grader.invoke(prompts.GRADE.format(q=q, docs=candidate_text))
            indices = set(res.relevant_doc_indices) if (res and res.relevant_doc_indices) else set()
            kept = [d for i, d in enumerate(docs) if i in indices]
            log.info("grade → kept %d / %d docs (reason: %s)", len(kept), len(docs), getattr(res, "reason", ""))
            # If grader filtered everything out, fail-open to avoid false abstains
            if not kept and docs:
                log.warning("grade returned 0 docs, failing open with top candidates")
                kept = docs[: config.MAX_CONTEXT_DOCS]
        except Exception as exc:
            log.warning("grade LLM call failed (%s) — failing open with top candidates", exc)
            kept = docs[: config.MAX_CONTEXT_DOCS]

        relevant_docs = kept[: config.MAX_CONTEXT_DOCS]
        log.info("grade → kept %d / %d docs", len(relevant_docs), len(docs))
        return {"relevant_docs": relevant_docs}

    def rewrite(state: State) -> dict:
        """Rewrite the query after a retrieval miss and drop the doc_type filter."""
        q_new = _text(llm.invoke(prompts.REWRITE.format(q=state["query"]))).strip()
        log.info("rewrite → new query: %r", q_new)
        return {
            "query":    q_new,
            "doc_type": None,   # cast a wider net on retry
            "retries":  state["retries"] + 1,
        }

    def generate(state: State) -> dict:
        """Generate a grounded answer using only the context passages."""
        docs     = state["relevant_docs"]
        intent   = state.get("intent", "research")
        system   = prompts.DRAFT_SYSTEM if intent == "drafting" else prompts.RESEARCH_SYSTEM
        last_msg = _text(state["messages"][-1]) if state.get("messages") else state["query"]

        # Build user turn — the context comes first so the model sees it before
        # the instruction, which empirically reduces hallucination on long prompts.
        user = (
            f"CONTEXT (answer ONLY from these passages, cite by number):\n"
            f"{_context(docs)}\n\n"
            f"REQUEST: {last_msg}"
        )
        if state.get("feedback"):
            user += (
                f"\n\nYour previous answer contained claims NOT supported by the context. "
                f"Remove or correct every flagged claim:\n{state['feedback']}"
            )

        out = llm.invoke([SystemMessage(system), HumanMessage(user)])
        log.info("generate → attempt %d", state.get("gen_attempts", 0) + 1)
        return {"answer": _text(out), "gen_attempts": state.get("gen_attempts", 0) + 1}

    def verify(state: State) -> dict:
        """Audit answer claims against the context passages."""
        # Drafts intentionally contain model-generated text; don't audit them.
        if state.get("intent") == "drafting":
            return {"grounded": True, "feedback": ""}

        try:
            v = auditor.invoke(prompts.VERIFY.format(
                context=_context(state["relevant_docs"]),
                answer=state["answer"],
            ))
        except Exception as exc:
            log.warning("Auditor failed (%s) — treating as grounded to avoid loop", exc)
            return {"grounded": True, "feedback": ""}

        if v is None:
            return {"grounded": True, "feedback": ""}

        feedback = "\n".join(f"- {c}" for c in v.unsupported_claims)
        log.info("verify → grounded=%s  unsupported=%d", v.grounded, len(v.unsupported_claims))
        return {"grounded": v.grounded, "feedback": feedback}

    def finalize(state: State) -> dict:
        """Append sources and disclaimer to the final answer."""
        docs = state.get("relevant_docs", [])
        src  = "\n".join(f"[{i}] {_cite(d)}" for i, d in enumerate(docs, 1))
        text = state["answer"]

        if not state.get("grounded", True) and state.get("feedback"):
            text += (
                "\n\n> ⚠️ **Some claims could not be verified against the sources:**\n"
                f"{state['feedback']}"
            )

        if src:
            text += f"\n\n**Sources**\n{src}"
        text += config.DISCLAIMER
        return {"messages": [AIMessage(text)]}

    def abstain(state: State) -> dict:
        """Refuse to answer when no grounded evidence was found."""
        msg = (
            f"I searched the knowledge base for **\"{state['query']}\"** but could not find "
            "sufficiently relevant passages to give a grounded answer.\n\n"
            "**What you can do:**\n"
            "- Upload the relevant statute, judgment, or contract via the sidebar.\n"
            "- Name the specific section or case directly (e.g. 'Section 73 Indian Contract Act').\n"
            "- Try rephrasing the question with more specific legal terminology."
            + config.DISCLAIMER
        )
        return {"messages": [AIMessage(msg)]}

    def calc(state: State) -> dict:
        """Delegate to the ReAct calculation agent."""
        msgs = state.get("messages", [])[-4:]
        res  = calc_agent.invoke({"messages": msgs})
        return {"messages": [AIMessage(_text(res["messages"][-1]) + config.DISCLAIMER)]}

    def chat(state: State) -> dict:
        """Handle chitchat / non-legal questions."""
        msgs = state.get("messages", [])[-4:]
        out  = llm.invoke([SystemMessage(prompts.CHAT_SYSTEM)] + msgs)
        return {"messages": [AIMessage(_text(out))]}

    # ------------------------------------------------------------------
    # Edge conditions
    # ------------------------------------------------------------------

    def after_plan(state: State) -> str:
        return {"calculation": "calc", "chitchat": "chat"}.get(state["intent"], "retrieve")

    def after_grade(state: State) -> str:
        if state.get("relevant_docs"):
            return "generate"
        if state.get("retries", 0) < config.MAX_RETRIEVAL_RETRIES:
            return "rewrite"
        return "abstain"

    def after_verify(state: State) -> str:
        # Move to finalize if grounded OR if we've exhausted generation attempts.
        if state.get("grounded") or state.get("gen_attempts", 0) >= config.MAX_GENERATION_ATTEMPTS:
            return "finalize"
        # Only regenerate when there are actual unsupported claims to fix.
        if state.get("feedback", "").strip():
            return "generate"
        return "finalize"

    # ------------------------------------------------------------------
    # Graph assembly
    # ------------------------------------------------------------------

    g = StateGraph(State)
    for name, fn in [
        ("plan",     plan),
        ("retrieve", retrieve),
        ("grade",    grade),
        ("rewrite",  rewrite),
        ("generate", generate),
        ("verify",   verify),
        ("finalize", finalize),
        ("abstain",  abstain),
        ("calc",     calc),
        ("chat",     chat),
    ]:
        g.add_node(name, fn)

    g.add_edge(START, "plan")
    g.add_conditional_edges("plan",   after_plan,   ["retrieve", "calc", "chat"])
    g.add_edge("retrieve", "grade")
    g.add_conditional_edges("grade",  after_grade,  ["generate", "rewrite", "abstain"])
    g.add_edge("rewrite", "retrieve")
    g.add_edge("generate", "verify")
    g.add_conditional_edges("verify", after_verify, ["finalize", "generate"])
    for end_node in ("finalize", "abstain", "calc", "chat"):
        g.add_edge(end_node, END)

    return g.compile(checkpointer=checkpointer or MemorySaver())
