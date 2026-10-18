# NyaySutra AI — Agentic RAG for Lawyers

LangChain + LangGraph. Hybrid retrieval, relevance grading, self-correcting query rewrite,
citation auditing, and deterministic tools for deadline maths.

## Run
```bash
pip install -r requirements.txt
cp .env.example .env            # add ANTHROPIC_API_KEY
# put PDFs in data/statutes | data/judgments | data/contracts
python ingest.py                # builds .chroma/ (local embeddings, nothing leaves your machine)
python cli.py                   # terminal chat with live node trace
streamlit run app.py            # web UI with upload + ingest
pytest tests                    # offline tests
```

## Graph
plan → retrieve (dense + BM25 → RRF) → grade → [rewrite → retrieve]* → generate → verify → finalize
Branches: calculation → ReAct agent (limitation tools) · chitchat · abstain when nothing relevant is found.

## Design choices
- Section/Article-aware chunking, so every chunk carries a `section` tag used in citations.
- BM25 fused with vectors because statute numbers and case names are lexical.
- Answers only from numbered context; an auditor LLM checks each claim against its cited passage.
- Abstains instead of answering from model memory.
- Limitation table in tools.py is SEED DATA — verify against the bare Act.

## Next steps
Cross-encoder reranker · judgment metadata (court/year/bench) filters · citation-graph tool
(overruled/followed) · eval set with RAGAS or LangSmith · auth + per-matter document isolation.
