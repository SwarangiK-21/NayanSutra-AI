"""Agentic RAG graph (LangGraph).

plan ─┬─ research/drafting ─> retrieve ─> grade ─┬─ relevant ─> generate ─> verify ─┬─ grounded ─> finalize
      │                          ▲               │                ▲                 │
      │                          └── rewrite ◄───┘ (no hits)      └── regenerate ◄───┘ (unsupported)
      │                                          └─ retries exhausted ─> abstain
      ├─ calculation ─> ReAct agent w/ deterministic tools
      └─ chitchat ─> chat
"""
from typing import Annotated, List, Literal, Optional, TypedDict

from langchain.chat_models import init_chat_model
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


class Plan(BaseModel):
    intent: Literal["research", "drafting", "calculation", "chitchat"]
    standalone_query: str = Field(description="Self-contained search query")
    doc_type: Optional[Literal["statutes", "judgments", "contracts", "other"]] = None


class Grade(BaseModel):
    relevant: bool
    reason: str = ""


class Verdict(BaseModel):
    grounded: bool
    unsupported_claims: List[str] = Field(default_factory=list)


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


def _text(m) -> str:
    c = m.content if hasattr(m, "content") else m
    if isinstance(c, list):
        return "".join(b.get("text", "") if isinstance(b, dict) else str(b) for b in c)
    return c or ""


def _cite(d: Document) -> str:
    m = d.metadata
    parts = [m.get("source", "unknown")]
    if m.get("section"):
        parts.append(m["section"])
    if m.get("page"):
        parts.append(f"p.{m['page']}")
    return ", ".join(parts)


def _context(docs: List[Document]) -> str:
    return "\n\n".join(f"[{i}] ({_cite(d)})\n{d.page_content}" for i, d in enumerate(docs, 1))


def build_graph(checkpointer=None):
    llm = init_chat_model(config.LLM_MODEL, temperature=0)
    planner = llm.with_structured_output(Plan)
    grader = llm.with_structured_output(Grade)
    auditor = llm.with_structured_output(Verdict)
    retriever = HybridRetriever()
    calc_agent = create_react_agent(llm, TOOLS, prompt=prompts.CALC_SYSTEM)

    def plan(state: State):
        convo = "\n".join(f"{m.type}: {_text(m)}" for m in state["messages"][-7:])
        p = planner.invoke(prompts.PLAN.format(convo=convo))
        return {"intent": p.intent, "query": p.standalone_query, "doc_type": p.doc_type,
                "retries": 0, "gen_attempts": 0, "feedback": ""}

    def retrieve(state: State):
        return {"docs": retriever.retrieve(state["query"], state.get("doc_type"))}

    def grade(state: State):
        docs = state.get("docs", [])
        if not docs:
            return {"relevant_docs": []}
        inputs = [prompts.GRADE.format(q=state["query"], doc=d.page_content[:1500]) for d in docs]
        grades = grader.batch(inputs, config={"max_concurrency": 5})
        keep = [d for d, g in zip(docs, grades) if g and g.relevant]
        return {"relevant_docs": keep[: config.MAX_CONTEXT_DOCS]}

    def rewrite(state: State):
        q = _text(llm.invoke(prompts.REWRITE.format(q=state["query"]))).strip()
        # Drop the doc_type filter on retry: it may have been the reason for the miss.
        return {"query": q, "doc_type": None, "retries": state["retries"] + 1}

    def generate(state: State):
        docs = state["relevant_docs"]
        system = prompts.DRAFT_SYSTEM if state["intent"] == "drafting" else prompts.RESEARCH_SYSTEM
        user = f"CONTEXT:\n{_context(docs)}\n\nREQUEST: {_text(state['messages'][-1])}"
        if state.get("feedback"):
            user += f"\n\nYour previous attempt had unsupported claims. Fix them or remove them:\n{state['feedback']}"
        out = llm.invoke([SystemMessage(system), HumanMessage(user)])
        return {"answer": _text(out), "gen_attempts": state["gen_attempts"] + 1}

    def verify(state: State):
        if state["intent"] == "drafting":  # drafts are new text; only research is claim-checked
            return {"grounded": True}
        v = auditor.invoke(prompts.VERIFY.format(
            context=_context(state["relevant_docs"]), answer=state["answer"]))
        if v is None:
            return {"grounded": False, "feedback": "Verifier failed; re-check all citations."}
        return {"grounded": v.grounded, "feedback": "\n".join(f"- {c}" for c in v.unsupported_claims)}

    def finalize(state: State):
        docs = state["relevant_docs"]
        src = "\n".join(f"[{i}] {_cite(d)}" for i, d in enumerate(docs, 1))
        text = state["answer"]
        if not state.get("grounded", True):
            text += ("\n\n> ⚠️ **Unverified:** the auditor flagged claims that could not be tied to "
                     f"the sources:\n{state['feedback']}")
        text += f"\n\n**Sources**\n{src}{config.DISCLAIMER}"
        return {"messages": [AIMessage(text)]}

    def abstain(state: State):
        msg = ("I couldn't find sufficiently relevant material in the knowledge base for this "
               f"question (searched: \"{state['query']}\"). Try adding the relevant statute/judgment "
               "or naming the section or case directly." + config.DISCLAIMER)
        return {"messages": [AIMessage(msg)]}

    def calc(state: State):
        res = calc_agent.invoke({"messages": state["messages"][-3:]})
        return {"messages": [AIMessage(_text(res["messages"][-1]) + config.DISCLAIMER)]}

    def chat(state: State):
        out = llm.invoke([SystemMessage(prompts.CHAT_SYSTEM)] + state["messages"][-3:])
        return {"messages": [AIMessage(_text(out))]}

    def after_plan(state: State):
        return {"calculation": "calc", "chitchat": "chat"}.get(state["intent"], "retrieve")

    def after_grade(state: State):
        if state["relevant_docs"]:
            return "generate"
        return "rewrite" if state["retries"] < config.MAX_RETRIEVAL_RETRIES else "abstain"

    def after_verify(state: State):
        if state["grounded"] or state["gen_attempts"] >= config.MAX_GENERATION_ATTEMPTS:
            return "finalize"
        return "generate"

    g = StateGraph(State)
    for name, fn in [("plan", plan), ("retrieve", retrieve), ("grade", grade), ("rewrite", rewrite),
                     ("generate", generate), ("verify", verify), ("finalize", finalize),
                     ("abstain", abstain), ("calc", calc), ("chat", chat)]:
        g.add_node(name, fn)
    g.add_edge(START, "plan")
    g.add_conditional_edges("plan", after_plan, ["retrieve", "calc", "chat"])
    g.add_edge("retrieve", "grade")
    g.add_conditional_edges("grade", after_grade, ["generate", "rewrite", "abstain"])
    g.add_edge("rewrite", "retrieve")
    g.add_edge("generate", "verify")
    g.add_conditional_edges("verify", after_verify, ["finalize", "generate"])
    for end_node in ("finalize", "abstain", "calc", "chat"):
        g.add_edge(end_node, END)
    return g.compile(checkpointer=checkpointer or MemorySaver())
