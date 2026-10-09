import tempfile
import uuid
from pathlib import Path

import streamlit as st

from graph import _text, build_graph
from ingest import ingest_paths

st.set_page_config(page_title="Nyayasutra AI", page_icon="⚖️")
st.title("⚖️ Nyayasutra AI")

@st.cache_resource
def graph():
    return build_graph()

if "thread" not in st.session_state:
    st.session_state.thread = str(uuid.uuid4())
    st.session_state.history = []

with st.sidebar:
    st.header("Knowledge base")
    doc_type = st.selectbox("Document type", ["statutes", "judgments", "contracts", "other"])
    files = st.file_uploader("Add PDFs / text", type=["pdf", "txt", "md"], accept_multiple_files=True)
    if files and st.button("Ingest"):
        tmp = Path(tempfile.mkdtemp()) / doc_type
        tmp.mkdir(parents=True)
        paths = []
        for f in files:
            p = tmp / f.name
            p.write_bytes(f.getvalue())
            paths.append(p)
        with st.spinner("Indexing…"):
            st.success(f"Indexed {ingest_paths(paths)} chunks")

for role, text in st.session_state.history:
    st.chat_message(role).markdown(text)

if q := st.chat_input("Ask a legal question, request a draft, or compute a deadline"):
    st.session_state.history.append(("user", q))
    st.chat_message("user").markdown(q)
    cfg = {"configurable": {"thread_id": st.session_state.thread}}
    with st.chat_message("assistant"):
        final, trace = None, []
        with st.status("Working…") as status:
            for upd in graph().stream({"messages": [("user", q)]}, cfg, stream_mode="updates"):
                for node, delta in upd.items():
                    trace.append(node)
                    status.update(label=" → ".join(trace))
                    if delta and delta.get("messages"):
                        final = delta["messages"][-1]
        answer = _text(final) if final else "No answer produced."
        st.markdown(answer)
    st.session_state.history.append(("assistant", answer))
