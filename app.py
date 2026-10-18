import logging
import os
import tempfile
import uuid
from datetime import date, timedelta
from pathlib import Path

import streamlit as st

import config
import prompts
from cases import (
    CASE_STATUSES,
    CASE_TYPE_META,
    CASE_TYPES,
    LegalCase,
    add_note_to_case,
    create_case,
    delete_case,
    format_case_context_for_llm,
    get_case,
    load_cases,
    update_case,
)
from graph import _cite, _text, build_graph
from ingest import ingest_paths
from store import all_documents
from tools import LIMITATION_TABLE, calculate_limitation_deadline, days_between

# Enable INFO-level logging so pipeline decisions are visible in terminal
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)

# ── Page configuration ──────────────────────────────────────────────────────
st.set_page_config(
    page_title="NyaySutra AI — Legal Agentic RAG & Case Workspace",
    page_icon="⚖️",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── Premium dark-legal aesthetic ────────────────────────────────────────────
st.markdown(
    """
    <style>
    @import url('https://fonts.googleapis.com/css2?family=Cinzel:wght@500;700&family=Inter:wght@300;400;500;600;700&display=swap');

    :root {
        --gold:    #c5a059;
        --gold-lt: #dfbe82;
        --card:    #161b22;
        --border:  #30363d;
        --muted:   #8b949e;
    }

    .main-header {
        font-family: 'Cinzel', serif;
        font-size: 2.1rem;
        font-weight: 700;
        color: #e6edf3;
        display: flex;
        align-items: center;
        gap: 10px;
        margin-bottom: .15rem;
    }
    .sub-header {
        font-family: 'Inter', sans-serif;
        font-size: .9rem;
        color: var(--muted);
        margin-bottom: 1.4rem;
    }
    .badge {
        display: inline-block;
        padding: 3px 9px;
        border-radius: 10px;
        font-size: .72rem;
        font-weight: 600;
        text-transform: uppercase;
        letter-spacing: .4px;
    }
    .b-gold  { background:rgba(197,160,89,.15); color:#dfbe82; border:1px solid rgba(197,160,89,.4); }
    .b-blue  { background:rgba(56,139,253,.15);  color:#58a6ff; border:1px solid rgba(56,139,253,.4); }
    .b-green { background:rgba(46,160,67,.15);   color:#3fb950; border:1px solid rgba(46,160,67,.4); }

    .badge-civil    { background:rgba(56,139,253,.15);  color:#58a6ff; border:1px solid rgba(56,139,253,.4); }
    .badge-criminal { background:rgba(248,81,73,.15);   color:#f85149; border:1px solid rgba(248,81,73,.4); }
    .badge-family   { background:rgba(188,140,255,.15); color:#bc8cff; border:1px solid rgba(188,140,255,.4); }
    .badge-revenue  { background:rgba(227,179,65,.15);  color:#e3b341; border:1px solid rgba(227,179,65,.4); }

    .trace-node {
        background:#21262d; border:1px solid #30363d; border-radius:5px;
        padding:5px 10px; display:inline-block; margin:3px;
        font-size:.78rem; font-family:monospace; color:#c9d1d9;
    }
    .trace-active { border-color:#c5a059; color:#dfbe82; background:rgba(197,160,89,.1); }

    .case-card {
        background: #161b22;
        border: 1px solid #30363d;
        border-radius: 8px;
        padding: 16px;
        margin-bottom: 12px;
        transition: all 0.2s ease;
    }
    .case-card:hover {
        border-color: #c5a059;
        box-shadow: 0 4px 12px rgba(0,0,0,0.25);
    }
    .active-case-banner {
        background: linear-gradient(90deg, rgba(197,160,89,0.14) 0%, rgba(22,27,34,0.7) 100%);
        border-left: 4px solid #c5a059;
        border-top: 1px solid #30363d;
        border-right: 1px solid #30363d;
        border-bottom: 1px solid #30363d;
        border-radius: 6px;
        padding: 12px 16px;
        margin-bottom: 14px;
    }
    .metric-card {
        background: #161b22;
        border: 1px solid #30363d;
        border-radius: 8px;
        padding: 12px 14px;
        text-align: center;
    }
    .metric-num {
        font-size: 1.6rem;
        font-weight: 700;
        font-family: 'Cinzel', serif;
    }
    .metric-txt {
        font-size: 0.72rem;
        color: #8b949e;
        text-transform: uppercase;
        letter-spacing: .5px;
    }
    .note-item {
        background: #0d1117;
        border-left: 3px solid #c5a059;
        padding: 8px 12px;
        border-radius: 4px;
        margin-bottom: 8px;
    }
    </style>
    """,
    unsafe_allow_html=True,
)


# ── Cached resources ────────────────────────────────────────────────────────
@st.cache_resource
def get_compiled_graph():
    return build_graph()


# ── Session state ────────────────────────────────────────────────────────────
if "thread_id" not in st.session_state:
    st.session_state.thread_id = str(uuid.uuid4())
    st.session_state.history = []
    st.session_state.last_trace = []
    st.session_state.sample_q = None

# Case state
if "active_case_id" not in st.session_state:
    existing_cases = load_cases()
    st.session_state.active_case_id = existing_cases[0].case_id if existing_cases else None

if "case_search_q" not in st.session_state:
    st.session_state.case_search_q = ""

if "case_type_filter" not in st.session_state:
    st.session_state.case_type_filter = "All"

if "success_case_msg" not in st.session_state:
    st.session_state.success_case_msg = None


# ── Header ───────────────────────────────────────────────────────────────────
st.markdown(
    """
    <div class="main-header">
        ⚖️ NyaySutra AI
        <span class="badge b-gold">Agentic RAG</span>
        <span class="badge b-blue">Hybrid RRF</span>
        <span class="badge b-green">Citation Verified</span>
    </div>
    <div class="sub-header">
        Autonomous legal workspace · Matter management · Section-aware retrieval · Deterministic limitation arithmetic
    </div>
    """,
    unsafe_allow_html=True,
)


# ── Sidebar ───────────────────────────────────────────────────────────────────
with st.sidebar:
    st.subheader("💼 Active Case Dossier")
    cases = load_cases()
    if cases:
        case_options = {c.case_id: f"[{c.case_type}] {c.title}" for c in cases}
        current_case_id = st.session_state.active_case_id
        if current_case_id not in case_options:
            current_case_id = cases[0].case_id
            st.session_state.active_case_id = current_case_id

        selected_id = st.selectbox(
            "Current Case",
            options=list(case_options.keys()),
            format_func=lambda cid: case_options[cid],
            index=list(case_options.keys()).index(current_case_id),
            key="sb_case_dropdown",
        )
        if selected_id != st.session_state.active_case_id:
            st.session_state.active_case_id = selected_id
            st.rerun()

        active_case = get_case(st.session_state.active_case_id)
        if active_case:
            meta = CASE_TYPE_META.get(active_case.case_type, CASE_TYPE_META["Civil"])
            st.markdown(
                f"""
                <div style="background:#161b22; border:1px solid #30363d; border-radius:6px; padding:10px; margin-top:4px;">
                    <div><span class="badge {meta['badge_class']}">{meta['icon']} {active_case.case_type}</span> <span style="font-size:0.75rem; color:#8b949e;">{active_case.case_number}</span></div>
                    <div style="font-size:0.85rem; font-weight:600; color:#e6edf3; margin-top:6px;">{active_case.title}</div>
                    <div style="font-size:0.75rem; color:#8b949e; margin-top:2px;">🏛️ {active_case.court}</div>
                    <div style="font-size:0.75rem; color:#dfbe82; margin-top:2px;">📅 Next Hearing: {active_case.next_hearing_date or 'TBD'}</div>
                </div>
                """,
                unsafe_allow_html=True,
            )
    else:
        st.info("No cases created yet. Create your first case in Case Management.")

    st.markdown("---")
    st.subheader("📁 Add Documents")
    uploaded = st.file_uploader(
        "Upload Legal Documents",
        type=None,
        accept_multiple_files=True,
        help="Upload all legal documents (PDF, Word DOCX, Text, Markdown, etc.). Files are chunked and indexed locally with automatic classification.",
    )
    if uploaded and st.button("📥 Index into Knowledge Base", use_container_width=True):
        tmp = Path(tempfile.mkdtemp()) / "uploads"
        tmp.mkdir(parents=True, exist_ok=True)
        paths = []
        for uf in uploaded:
            dest = tmp / uf.name
            dest.write_bytes(uf.getvalue())
            paths.append(dest)
        with st.spinner("Chunking & embedding locally…"):
            n = ingest_paths(paths)
            from store import get_vectorstore
            get_vectorstore.cache_clear()
        st.success(f"Indexed **{n}** chunks from {len(paths)} file(s).")
        st.info("The knowledge base is updated — ask your question now.")

    st.markdown("---")
    if st.button("🔄 New Session", use_container_width=True):
        st.session_state.thread_id = str(uuid.uuid4())
        st.session_state.history = []
        st.session_state.last_trace = []
        st.session_state.sample_q = None
        st.rerun()


# ── Tabs ──────────────────────────────────────────────────────────────────────
tab_cases, tab1, tab2, tab3, tab4 = st.tabs([
    "💼 Case Management",
    "🏛️ Research & Drafting",
    "⏱️ Limitation Calculator",
    "📚 Knowledge Vault",
    "🧭 Architecture",
])


# ─────────────────────────────────────────────────────────────────────────────
# TAB 0 — Case Management & Creation
# ─────────────────────────────────────────────────────────────────────────────
with tab_cases:
    all_cases = load_cases()
    total_count = len(all_cases)
    civil_count = sum(1 for c in all_cases if c.case_type == "Civil")
    crim_count = sum(1 for c in all_cases if c.case_type == "Criminal")
    fam_count = sum(1 for c in all_cases if c.case_type == "Family")
    rev_count = sum(1 for c in all_cases if c.case_type == "Revenue")

    # Metrics header
    m_col1, m_col2, m_col3, m_col4, m_col5 = st.columns(5)
    with m_col1:
        st.markdown(
            f"""<div class="metric-card">
                <div class="metric-num" style="color:#dfbe82;">{total_count}</div>
                <div class="metric-txt">Total Cases</div>
            </div>""",
            unsafe_allow_html=True,
        )
    with m_col2:
        st.markdown(
            f"""<div class="metric-card">
                <div class="metric-num" style="color:#58a6ff;">{civil_count}</div>
                <div class="metric-txt">Civil Matters</div>
            </div>""",
            unsafe_allow_html=True,
        )
    with m_col3:
        st.markdown(
            f"""<div class="metric-card">
                <div class="metric-num" style="color:#f85149;">{crim_count}</div>
                <div class="metric-txt">Criminal Matters</div>
            </div>""",
            unsafe_allow_html=True,
        )
    with m_col4:
        st.markdown(
            f"""<div class="metric-card">
                <div class="metric-num" style="color:#bc8cff;">{fam_count}</div>
                <div class="metric-txt">Family Matters</div>
            </div>""",
            unsafe_allow_html=True,
        )
    with m_col5:
        st.markdown(
            f"""<div class="metric-card">
                <div class="metric-num" style="color:#e3b341;">{rev_count}</div>
                <div class="metric-txt">Revenue Matters</div>
            </div>""",
            unsafe_allow_html=True,
        )

    st.markdown("<div style='height: 16px;'></div>", unsafe_allow_html=True)

    if st.session_state.success_case_msg:
        st.success(st.session_state.success_case_msg)
        st.session_state.success_case_msg = None

    subtab_create, subtab_dossier, subtab_register = st.tabs([
        "➕ Create Case",
        "📋 Active Case Dossier",
        "📂 Case Register (All Matters)",
    ])

    # ── Subtab A: Create Case ──────────────────────────────────────────────
    with subtab_create:
        st.markdown("#### ⚖️ Create New Legal Matter")
        st.caption(
            "Register a new litigation or advisory matter. Select the branch of law "
            "(Civil, Criminal, Family, or Revenue) to initialize tailored legal workflows."
        )

        with st.container():
            col_t1, col_t2 = st.columns([1, 2])
            with col_t1:
                # Core requirement: dropdown of selecting the type of case like civil/criminal/family/revenue
                case_type_sel = st.selectbox(
                    "Case Type *",
                    options=CASE_TYPES,
                    index=0,
                    help="Select legal classification: Civil, Criminal, Family, or Revenue",
                )
                type_info = CASE_TYPE_META[case_type_sel]
                st.markdown(
                    f"""
                    <div style="background:#161b22; border-left:3px solid {type_info['color']}; padding:8px 12px; border-radius:4px; font-size:0.8rem; color:#c9d1d9;">
                        <strong>{type_info['icon']} {case_type_sel} Domain:</strong><br/>
                        <span style="color:#8b949e;">{type_info['desc']}</span>
                    </div>
                    """,
                    unsafe_allow_html=True,
                )

            with col_t2:
                case_title = st.text_input(
                    "Case Title / Matter Name *",
                    placeholder="e.g. Ramesh Sharma vs. State of Maharashtra / ABC Corp vs. Union of India",
                    help="Enter cause title or primary party names",
                )

            row2_c1, row2_c2, row2_c3 = st.columns(3)
            with row2_c1:
                case_num = st.text_input(
                    "Case / Filing Number *",
                    placeholder="e.g. CS(COMM) 204/2024 or BAIL 891/2024",
                )
            with row2_c2:
                court_sel = st.selectbox(
                    "Court / Forum *",
                    options=[
                        "Supreme Court of India",
                        "High Court of Judicature at Bombay",
                        "High Court of Delhi at New Delhi",
                        "High Court of Karnataka",
                        "Principal District & Sessions Court",
                        "Family Court",
                        "Maharashtra Revenue Tribunal / SDO",
                        "Collector & District Magistrate Court",
                        "National Company Law Tribunal (NCLT)",
                        "Debt Recovery Tribunal (DRT)",
                        "Consumer Disputes Redressal Commission",
                        "Other Judicial / Quasi-Judicial Forum",
                    ],
                )
            with row2_c3:
                status_sel = st.selectbox("Matter Status", CASE_STATUSES, index=1)

            row3_c1, row3_c2, row3_c3 = st.columns(3)
            with row3_c1:
                client_name = st.text_input(
                    "Client Name *",
                    placeholder="e.g. Ramesh Sharma / Acme Enterprises",
                )
            with row3_c2:
                client_role = st.selectbox(
                    "Client Representation *",
                    [
                        "Petitioner / Plaintiff",
                        "Respondent / Defendant",
                        "Applicant / Revisionist",
                        "Appellant",
                        "Complainant / Informant",
                        "Accused / Non-Applicant",
                    ],
                )
            with row3_c3:
                opp_party = st.text_input(
                    "Opposite Party / Respondent *",
                    placeholder="e.g. State of Maharashtra & Anr. / XYZ Bank",
                )

            row4_c1, row4_c2, row4_c3 = st.columns(3)
            with row4_c1:
                filing_date_val = st.date_input("Filing / Institution Date", value=date.today())
            with row4_c2:
                has_hearing = st.checkbox("Next Hearing Scheduled", value=True)
                next_hearing_val = (
                    st.date_input("Next Hearing Date", value=date.today() + timedelta(days=21))
                    if has_hearing
                    else None
                )
            with row4_c3:
                suggested_acts = {
                    "Civil": "Arbitration Act Sec. 11/34, CPC O.39, Contract Act Sec. 73",
                    "Criminal": "BNSS Sec. 483 (CrPC 439), BNS Sec. 316 (IPC 409)",
                    "Family": "Hindu Marriage Act Sec. 13B, 24, 25; DV Act Sec. 12",
                    "Revenue": "Land Revenue Code Sec. 44, 247, 257; Tenancy Act Sec. 84",
                }
                acts_sections = st.text_input(
                    "Relevant Acts & Sections",
                    value=suggested_acts.get(case_type_sel, ""),
                    help="Statutes and sections governing this dispute",
                )

            case_desc = st.text_area(
                "Case Brief / Factual Matrix *",
                placeholder=(
                    "Summarize the core factual background, dispute timeline, key events, "
                    "cause of action accrual date, and specific relief or prayers sought..."
                ),
                height=130,
            )

            btn_create = st.button("⚖️ Create Case & Open Dossier", type="primary", use_container_width=True)
            if btn_create:
                if not case_title.strip() or not client_name.strip() or not opp_party.strip():
                    st.error("Please fill in all required fields (Case Title, Client Name, and Opposite Party).")
                else:
                    new_case = create_case(
                        title=case_title,
                        case_type=case_type_sel,
                        case_number=case_num or "Unregistered",
                        court=court_sel,
                        client_name=client_name,
                        client_role=client_role,
                        opposite_party=opp_party,
                        filing_date=filing_date_val.strftime("%Y-%m-%d"),
                        next_hearing_date=next_hearing_val.strftime("%Y-%m-%d") if next_hearing_val else None,
                        status=status_sel,
                        acts_sections=acts_sections,
                        description=case_desc,
                    )
                    st.session_state.active_case_id = new_case.case_id
                    st.session_state.success_case_msg = (
                        f"✅ Case '{new_case.title}' [{new_case.case_type}] successfully created! "
                        f"Case ID: {new_case.case_id}. Dossier is now active."
                    )
                    st.rerun()

    # ── Subtab B: Active Case Dossier ──────────────────────────────────────
    with subtab_dossier:
        active_case = get_case(st.session_state.active_case_id) if st.session_state.active_case_id else None
        if not active_case:
            st.info("No active case selected. Select a case from the Case Register or create a new case.")
        else:
            meta = CASE_TYPE_META.get(active_case.case_type, CASE_TYPE_META["Civil"])
            st.markdown(
                f"""
                <div class="active-case-banner">
                    <div style="display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:8px;">
                        <div>
                            <span class="badge {meta['badge_class']}">{meta['icon']} {active_case.case_type} Matter</span>
                            <span class="badge b-gold">{active_case.status}</span>
                            <span class="badge b-blue">{active_case.case_number}</span>
                        </div>
                        <div style="font-size:0.8rem; color:#8b949e;">Case ID: <code>{active_case.case_id}</code></div>
                    </div>
                    <div style="font-size:1.35rem; font-weight:700; font-family:'Cinzel', serif; color:#e6edf3; margin-top:8px;">
                        {active_case.title}
                    </div>
                </div>
                """,
                unsafe_allow_html=True,
            )

            d_col1, d_col2, d_col3, d_col4 = st.columns(4)
            with d_col1:
                st.markdown(
                    f"""<div style="background:#161b22; border:1px solid #30363d; border-radius:6px; padding:10px;">
                        <span style="font-size:0.75rem; color:#8b949e;">COURT / FORUM</span><br/>
                        <strong style="color:#e6edf3;">🏛️ {active_case.court}</strong>
                    </div>""",
                    unsafe_allow_html=True,
                )
            with d_col2:
                st.markdown(
                    f"""<div style="background:#161b22; border:1px solid #30363d; border-radius:6px; padding:10px;">
                        <span style="font-size:0.75rem; color:#8b949e;">CLIENT</span><br/>
                        <strong style="color:#e6edf3;">👤 {active_case.client_name}</strong><br/>
                        <span style="font-size:0.75rem; color:#8b949e;">({active_case.client_role})</span>
                    </div>""",
                    unsafe_allow_html=True,
                )
            with d_col3:
                st.markdown(
                    f"""<div style="background:#161b22; border:1px solid #30363d; border-radius:6px; padding:10px;">
                        <span style="font-size:0.75rem; color:#8b949e;">OPPOSITE PARTY</span><br/>
                        <strong style="color:#e6edf3;">👥 {active_case.opposite_party}</strong>
                    </div>""",
                    unsafe_allow_html=True,
                )
            with d_col4:
                hearing_str = active_case.next_hearing_date or "Not scheduled"
                st.markdown(
                    f"""<div style="background:#161b22; border:1px solid #30363d; border-radius:6px; padding:10px;">
                        <span style="font-size:0.75rem; color:#8b949e;">NEXT HEARING</span><br/>
                        <strong style="color:#dfbe82;">📅 {hearing_str}</strong>
                    </div>""",
                    unsafe_allow_html=True,
                )

            st.markdown("<div style='height: 12px;'></div>", unsafe_allow_html=True)

            dos_c1, dos_c2 = st.columns([6, 4])
            with dos_c1:
                st.markdown("##### 📜 Statutory Provisions & Relevant Sections")
                st.info(active_case.acts_sections or "No statutes specified.")

                st.markdown("##### 📝 Factual Matrix & Case Brief")
                st.text_area(
                    "Factual Background",
                    active_case.description or "No case brief entered.",
                    height=180,
                    disabled=True,
                )

                # Quick workflow triggers
                st.markdown("##### ⚡ Legal Copilot Actions for this Case")
                act1, act2 = st.columns(2)
                with act1:
                    if st.button("💬 Research Legal Grounds", use_container_width=True):
                        st.session_state.sample_q = (
                            f"In the {active_case.case_type} matter of {active_case.title} ({active_case.case_number}), "
                            f"representing {active_case.client_name} ({active_case.client_role}) against {active_case.opposite_party}: "
                            f"Analyze the binding legal grounds and statutory interpretation under {active_case.acts_sections}. "
                            f"Key facts: {active_case.description}"
                        )
                        st.info("Prompt prepared for Research & Drafting tab!")
                with act2:
                    if st.button("📜 Draft Written Submissions / Notice", use_container_width=True):
                        st.session_state.sample_q = (
                            f"Draft formal legal grounds and prayer for {active_case.client_name} in the court of {active_case.court}. "
                            f"Matter: {active_case.title}. Relevant provisions: {active_case.acts_sections}. "
                            f"Factual matrix: {active_case.description}"
                        )
                        st.info("Drafting prompt prepared for Research & Drafting tab!")

            with dos_c2:
                st.markdown("##### 📖 Case Diary & Proceeding Notes")
                with st.form("add_note_form", clear_on_submit=True):
                    note_author = st.text_input("Advocate / Author", value="Advocate on Record")
                    note_content = st.text_area("Note / Order Update", placeholder="e.g. Court issued notice returnable on 28th Nov. Reply affidavit to be filed within 2 weeks.")
                    if st.form_submit_button("➕ Record Note", use_container_width=True):
                        if note_content.strip():
                            add_note_to_case(active_case.case_id, note_content, note_author)
                            st.rerun()

                # Display existing notes
                if active_case.notes:
                    for n in reversed(active_case.notes):
                        st.markdown(
                            f"""
                            <div class="note-item">
                                <div style="display:flex; justify-content:space-between; font-size:0.75rem; color:#8b949e;">
                                    <span><strong>{n.get('author','Advocate')}</strong></span>
                                    <span>{n.get('timestamp','')}</span>
                                </div>
                                <div style="font-size:0.83rem; color:#e6edf3; margin-top:4px;">
                                    {n.get('content','')}
                                </div>
                            </div>
                            """,
                            unsafe_allow_html=True,
                        )
                else:
                    st.caption("No proceeding notes logged yet.")

                st.markdown("---")
                if st.button("🗑️ Delete this Case", type="secondary", use_container_width=True):
                    delete_case(active_case.case_id)
                    all_c = load_cases()
                    st.session_state.active_case_id = all_c[0].case_id if all_c else None
                    st.success("Case deleted.")
                    st.rerun()

    # ── Subtab C: Case Register ────────────────────────────────────────────
    with subtab_register:
        st.markdown("#### 📂 Case Register & Directory")
        st.caption("Filter and review all registered matters across civil, criminal, family, and revenue law.")

        flt_col1, flt_col2, flt_col3 = st.columns([2, 2, 4])
        with flt_col1:
            type_filter = st.selectbox(
                "Filter by Case Type",
                options=["All"] + CASE_TYPES,
                index=0,
            )
        with flt_col2:
            status_filter = st.selectbox(
                "Filter by Status",
                options=["All"] + CASE_STATUSES,
                index=0,
            )
        with flt_col3:
            search_query = st.text_input("🔍 Search cases", placeholder="Search title, case number, client, court...")

        # Apply filters
        filtered_cases = all_cases
        if type_filter != "All":
            filtered_cases = [c for c in filtered_cases if c.case_type == type_filter]
        if status_filter != "All":
            filtered_cases = [c for c in filtered_cases if c.status == status_filter]
        if search_query.strip():
            sq = search_query.lower()
            filtered_cases = [
                c for c in filtered_cases
                if sq in c.title.lower()
                or sq in c.case_number.lower()
                or sq in c.client_name.lower()
                or sq in c.court.lower()
                or sq in c.acts_sections.lower()
            ]

        st.write(f"Showing **{len(filtered_cases)}** of **{len(all_cases)}** cases")

        for c in filtered_cases:
            meta = CASE_TYPE_META.get(c.case_type, CASE_TYPE_META["Civil"])
            is_active = (c.case_id == st.session_state.active_case_id)
            active_badge = "<span class='badge b-gold'>Active Matter</span>" if is_active else ""

            st.markdown(
                f"""
                <div class="case-card">
                    <div style="display:flex; justify-content:space-between; align-items:center;">
                        <div>
                            <span class="badge {meta['badge_class']}">{meta['icon']} {c.case_type}</span>
                            <span style="font-size:0.8rem; color:#8b949e; margin-left:8px;">{c.case_number}</span>
                            {active_badge}
                        </div>
                        <div style="font-size:0.8rem; color:#dfbe82;">
                            📅 Hearing: {c.next_hearing_date or 'Not scheduled'}
                        </div>
                    </div>
                    <div style="font-size:1.05rem; font-weight:700; color:#e6edf3; margin-top:6px; font-family:'Cinzel', serif;">
                        {c.title}
                    </div>
                    <div style="font-size:0.82rem; color:#8b949e; margin-top:4px;">
                        🏛️ {c.court} &nbsp;|&nbsp; 👤 Client: <strong style="color:#c9d1d9;">{c.client_name}</strong> ({c.client_role}) vs. <strong style="color:#c9d1d9;">{c.opposite_party}</strong>
                    </div>
                    <div style="font-size:0.78rem; color:#dfbe82; margin-top:4px;">
                        📜 Sections: {c.acts_sections or 'General provisions'}
                    </div>
                </div>
                """,
                unsafe_allow_html=True,
            )

            btn_col1, btn_col2 = st.columns([2, 8])
            with btn_col1:
                if not is_active:
                    if st.button(f"👉 Set as Active", key=f"set_act_{c.case_id}", use_container_width=True):
                        st.session_state.active_case_id = c.case_id
                        st.rerun()
                else:
                    st.caption("Currently Active")
            with btn_col2:
                pass


# ─────────────────────────────────────────────────────────────────────────────
# TAB 1 — Legal Copilot (Research & Drafting)
# ─────────────────────────────────────────────────────────────────────────────
with tab1:
    col_chat, col_meta = st.columns([7, 3])

    with col_chat:
        st.markdown("##### 💬 Legal Research & Drafting Copilot")

        # Active case banner if active case is present
        curr_case = get_case(st.session_state.active_case_id) if st.session_state.active_case_id else None
        if curr_case:
            meta = CASE_TYPE_META.get(curr_case.case_type, CASE_TYPE_META["Civil"])
            st.markdown(
                f"""
                <div class="active-case-banner">
                    <div style="display:flex; justify-content:space-between; align-items:center;">
                        <div>
                            <span class="badge {meta['badge_class']}">{meta['icon']} {curr_case.case_type}</span>
                            <strong style="color:#e6edf3; margin-left:6px;">{curr_case.title}</strong>
                            <span style="color:#8b949e; font-size:0.8rem; margin-left:6px;">({curr_case.case_number})</span>
                        </div>
                        <span class="badge b-gold">{curr_case.status}</span>
                    </div>
                    <div style="font-size:0.78rem; color:#8b949e; margin-top:4px;">
                        🏛️ {curr_case.court} &nbsp;|&nbsp; Client: <strong>{curr_case.client_name}</strong> ({curr_case.client_role})
                    </div>
                </div>
                """,
                unsafe_allow_html=True,
            )

            # Quick-action query prompts tailored to active case
            q1, q2, q3 = st.columns(3)
            with q1:
                if st.button(f"📜 Draft Grounds ({curr_case.case_type})", use_container_width=True):
                    st.session_state.sample_q = (
                        f"Draft the key legal grounds and relief for client {curr_case.client_name} ({curr_case.client_role}) "
                        f"in {curr_case.title} ({curr_case.case_number}) before {curr_case.court}. "
                        f"Applicable provisions: {curr_case.acts_sections}. Facts: {curr_case.description}"
                    )
            with q2:
                if st.button("⚖️ Research Precedents", use_container_width=True):
                    st.session_state.sample_q = (
                        f"What are the binding precedents relevant to {curr_case.acts_sections} "
                        f"for this dispute: {curr_case.description}"
                    )
            with q3:
                if st.button("🔍 Opposite Party Defenses", use_container_width=True):
                    st.session_state.sample_q = (
                        f"Analyze potential legal arguments and statutory defenses available to {curr_case.opposite_party} "
                        f"in: {curr_case.description}"
                    )
        else:
            # Fallback general quick-start sample queries
            q1, q2, q3 = st.columns(3)
            with q1:
                if st.button("⚖️ ONGC v Saw Pipes – Sec. 34", use_container_width=True):
                    st.session_state.sample_q = (
                        "What did the Supreme Court hold regarding Section 34 of the "
                        "Arbitration and Conciliation Act in ONGC vs Saw Pipes?"
                    )
            with q2:
                if st.button("⏱️ Breach of Contract limitation", use_container_width=True):
                    st.session_state.sample_q = (
                        "What is the limitation deadline for breach of contract if the "
                        "cause of action arose on 2024-03-15?"
                    )
            with q3:
                if st.button("📜 Cheque dishonour – NI Act", use_container_width=True):
                    st.session_state.sample_q = (
                        "What is the limitation period for a cheque dishonour complaint "
                        "under the Negotiable Instruments Act?"
                    )

        # Chat history
        for role, content in st.session_state.history:
            with st.chat_message(role):
                st.markdown(content)

        # Input
        preset = st.session_state.sample_q
        st.session_state.sample_q = None
        user_q = preset or st.chat_input(
            "Ask a legal question, request a draft, or analyze your active case…"
        )

        if user_q:
            st.session_state.history.append(("user", user_q))
            with st.chat_message("user"):
                st.markdown(user_q)

            cfg = {"configurable": {"thread_id": st.session_state.thread_id}}
            answer_text = ""
            with st.chat_message("assistant"):
                final_msg = None
                trace = []
                with st.status("Running agentic RAG pipeline…", expanded=True) as status:
                    try:
                        g = get_compiled_graph()
                        for update in g.stream(
                            {"messages": [("user", user_q)]}, cfg, stream_mode="updates"
                        ):
                            for node, delta in update.items():
                                trace.append(node)
                                status.update(
                                    label=f"Pipeline: {' → '.join(trace)}",
                                    state="running",
                                )
                                if delta and delta.get("messages"):
                                    final_msg = delta["messages"][-1]
                        status.update(
                            label=f"Done ✓  {' → '.join(trace)}",
                            state="complete",
                            expanded=False,
                        )
                    except Exception as err:
                        status.update(label=f"Error: {err}", state="error")

                answer_text = _text(final_msg) if final_msg else "No answer produced."
                st.markdown(answer_text)

            st.session_state.history.append(("assistant", answer_text))
            st.session_state.last_trace = trace
            st.rerun()

    with col_meta:
        st.markdown("##### 🔍 Execution Trace")
        if st.session_state.last_trace:
            active = {"generate", "calc", "verify", "finalize"}
            for step in st.session_state.last_trace:
                cls = "trace-active" if step in active else ""
                st.markdown(
                    f"<span class='trace-node {cls}'>● {step}</span>",
                    unsafe_allow_html=True,
                )
        else:
            st.caption("Submit a question to see live graph routing.")

        st.markdown("---")
        st.markdown("##### 🛡️ Grounding Guarantees")
        st.markdown(
            "- Answers cite only numbered context passages from your uploads\n"
            "- An auditor LLM checks every claim against the cited source\n"
            "- Limitation dates computed by deterministic Python — no LLM arithmetic\n"
            "- Refusal to answer (abstain) when no grounded evidence is found"
        )


# ─────────────────────────────────────────────────────────────────────────────
# TAB 2 — Limitation Calculator
# ─────────────────────────────────────────────────────────────────────────────
with tab2:
    st.markdown("#### ⏱️ Deterministic Legal Limitation Calculator")
    st.caption(
        "Date arithmetic runs entirely in Python. The LLM is never used for date calculations."
    )

    c1, c2 = st.columns(2)
    with c1:
        st.markdown("##### Limitation Deadline")
        key_sel = st.selectbox(
            "Cause of Action",
            list(LIMITATION_TABLE.keys()),
            format_func=lambda k: f"{k.replace('_',' ').title()} — {LIMITATION_TABLE[k][1]}",
        )
        accrual = st.date_input("Date Cause of Action Accrued", value=date.today())
        if st.button("Calculate Deadline", type="primary"):
            res = calculate_limitation_deadline.invoke({
                "cause_of_action": key_sel,
                "accrual_date": accrual.strftime("%Y-%m-%d"),
            })
            st.success(res)

    with c2:
        st.markdown("##### Days Between Two Dates")
        d_start = st.date_input("Start Date", value=date(2024, 1, 1))
        d_end = st.date_input("End Date", value=date.today())
        if st.button("Compute Days"):
            diff = days_between.invoke({
                "start_date": d_start.strftime("%Y-%m-%d"),
                "end_date": d_end.strftime("%Y-%m-%d"),
            })
            st.info(f"**{diff} days** between {d_start} and {d_end}")

    st.markdown("---")
    st.markdown("##### Pre-loaded Limitation Schedule")
    st.table([
        {"Cause of Action": k.replace("_", " ").title(), "Period": str(p), "Basis": ref}
        for k, (p, ref) in LIMITATION_TABLE.items()
    ])


# ─────────────────────────────────────────────────────────────────────────────
# TAB 3 — Knowledge Vault
# ─────────────────────────────────────────────────────────────────────────────
with tab3:
    st.markdown("#### 📚 Indexed Document Vault")
    st.caption(
        "All documents are stored locally in ChromaDB. Nothing is sent to an "
        "external embedding API."
    )

    try:
        docs = all_documents()
        total = len(docs)
    except Exception:
        docs = []
        total = 0

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Indexed Chunks", total)
    m2.metric("Embedding Model", config.EMBED_MODEL.split("/")[-1])
    m3.metric("Vector Dim", "384")
    m4.metric("Storage", "Local SQLite")

    st.markdown("---")
    ftype = st.radio("Filter category", ["All", "statutes", "judgments", "contracts", "other"], horizontal=True)
    search = st.text_input("Search content / filename")

    filtered = docs
    if ftype != "All":
        filtered = [d for d in filtered if d.metadata.get("doc_type") == ftype]
    if search:
        filtered = [
            d for d in filtered
            if search.lower() in d.page_content.lower()
            or search.lower() in d.metadata.get("source", "").lower()
        ]

    st.write(f"Showing **{min(len(filtered), 30)}** of **{len(filtered)}** chunks")
    for idx, doc in enumerate(filtered[:30], 1):
        with st.expander(f"#{idx}  {_cite(doc)}  [{doc.metadata.get('doc_type','?')}]"):
            st.caption(f"chunk_id: `{doc.metadata.get('chunk_id','')}`")
            st.text_area("Text", doc.page_content, height=130, key=f"vc_{idx}")


# ─────────────────────────────────────────────────────────────────────────────
# TAB 4 — Architecture
# ─────────────────────────────────────────────────────────────────────────────
with tab4:
    st.markdown("#### 🧭 Pipeline Architecture")
    st.code(
        """
User Query
  │
  ▼
[plan]  ─── classify intent → rewrite query for retrieval
  │
  ├─ 'chitchat'    ──► [chat]  → conversational reply
  ├─ 'calculation' ──► [calc]  → ReAct agent w/ Python date tools
  │
  └─ 'research' / 'drafting'
       │
       ▼
     [retrieve]  Dense (Chroma) + Lexical (BM25), fused by RRF
       │
       ▼
     [grade]     Per-doc relevance filter (sequential, fail-open)
       │
       ├─ no relevant docs + retries left  → [rewrite] → [retrieve]
       ├─ no relevant docs + retries used  → [abstain] (no hallucination)
       │
       └─ relevant docs found
              │
              ▼
           [generate]  Answer ONLY from numbered context passages
              │
              ▼
           [verify]    Citation auditor checks every claim
              │
              ├─ unsupported claims + attempts left  → [generate] (feedback loop)
              └─ grounded / max attempts             → [finalize]
                                                          │
                                                          ▼
                                                     Sources + Disclaimer
        """,
        language="text",
    )

    st.markdown(
        """
**Key design decisions that prevent false answers:**

| Mechanism | How it prevents hallucination |
|---|---|
| Sequential grading (fail-open) | No doc silently discarded due to batch API failure |
| `doc_type=null` on retry | Wider retrieval net if category filter caused a miss |
| Generate user message guardrail | "Answer ONLY from context" injected at every call |
| Verify → regenerate loop | Feedback-driven correction removes unsupported claims |
| Abstain node | Refuses to answer when evidence is missing — no LLM guessing |
| Deterministic date tools | Python calculates deadlines — LLM never touches date math |
        """
    )
