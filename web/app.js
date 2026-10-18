/* NyaySutra AI — frontend (no external libraries; CSP: script-src 'self'). */
"use strict";
(() => {
  // ── Safe HTML templating: every interpolated value is escaped unless wrapped in raw() ──
  const RAW = Symbol("raw");
  const ESC = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ESC[c]);
  const raw = (s) => ({ [RAW]: String(s) });
  const val = (v) => v == null || v === false ? "" : Array.isArray(v) ? v.map(val).join("")
    : typeof v === "object" && RAW in v ? v[RAW] : esc(v);
  const html = (strings, ...vals) => raw(strings.reduce((o, s, i) => o + s + (i < vals.length ? val(vals[i]) : ""), ""));
  const mount = (el, tpl) => { el.innerHTML = tpl[RAW]; return el; };
  const $ = (s, el = document) => el.querySelector(s);
  const $$ = (s, el = document) => [...el.querySelectorAll(s)];

  const store = {
    get(k, d) { try { const v = localStorage.getItem("nyaya." + k); return v === null ? d : JSON.parse(v); } catch { return d; } },
    set(k, v) { try { localStorage.setItem("nyaya." + k, JSON.stringify(v)); } catch { /* storage unavailable */ } },
  };

  const state = { csrf: null, status: null, caseId: store.get("caseId", null), chatId: null, scope: store.get("scope", "all"),
    streaming: false, pollTimer: null, msgSources: new Map(), msgSeq: 0 };

  const TYPE_BADGE = { Civil: "b-civil", Criminal: "b-criminal", Family: "b-family", Revenue: "b-revenue" };
  const COLL_LABEL = { case: "Case file", bare_act: "Bare Act", judgment: "Judgment", draft: "Template" };
  const fmtDate = (d) => { if (!d) return "—"; const x = new Date(d); return isNaN(x) ? d : x.toLocaleDateString("en-IN", { day: "2-digit", month: "short", year: "numeric" }); };
  const daysTo = (d) => Math.round((new Date(d) - new Date(new Date().toDateString())) / 864e5);

  // ── Toast ──
  let toastT;
  function toast(msg, err = false) {
    const t = $("#toast"); t.textContent = msg; t.className = "toast" + (err ? " err" : ""); t.hidden = false;
    clearTimeout(toastT); toastT = setTimeout(() => { t.hidden = true; }, 4200);
  }

  // ── API ──
  async function initSession() {
    const r = await fetch("/api/session", { credentials: "same-origin" });
    const s = await r.json();
    state.csrf = s.csrf || null;
    $("#logoutBtn").classList.toggle("hidden", !s.password_required);
    return s;
  }
  async function api(path, { method = "GET", body, form, retry = true } = {}) {
    const opt = { method, headers: {}, credentials: "same-origin" };
    if (method !== "GET") opt.headers["X-Nyaya-CSRF"] = state.csrf || "";
    if (form) opt.body = form;
    else if (body !== undefined) { opt.headers["Content-Type"] = "application/json"; opt.body = JSON.stringify(body); }
    const r = await fetch(path, opt);
    if (r.status === 401 && retry) {
      const s = await initSession();
      if (!s.authenticated) { renderLogin(); throw new Error("Please sign in"); }
      return api(path, { method, body, form, retry: false });
    }
    if (!r.ok) {
      let msg = ""; try { msg = (await r.json()).detail; } catch { /* not json */ }
      throw new Error(typeof msg === "string" && msg ? msg : `Request failed (${r.status})`);
    }
    return r.json();
  }

  // ── Markdown (input is escaped first, so the output cannot contain foreign HTML) ──
  function inline(s) {
    return s
      .replace(/`([^`]+)`/g, "<code>$1</code>")
      .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
      .replace(/(^|[\s(])\*([^*\s][^*]*?)\*(?=[\s.,;:)]|$)/g, "$1<em>$2</em>")
      .replace(/【\s*(\d{1,2})(?:†[^】]*)?】/g, "[$1]")
      .replace(/\[(\d{1,2})\]/g, '<button type="button" class="cite" data-cite="$1" aria-label="Source $1">$1</button>');
  }
  function markdown(src) {
    const L = esc(src || "").split("\n");
    const out = []; let i = 0;
    const isBlockStart = (l) => /^(#{1,4})\s|^```|^\s*([-*+]|\d+[.)])\s+|^\s*&gt;|^\s*\|.*\|\s*$|^\s*(-{3,}|\*{3,})\s*$/.test(l);
    while (i < L.length) {
      const l = L[i];
      if (/^```/.test(l)) {
        const buf = []; i++;
        while (i < L.length && !/^```/.test(L[i])) buf.push(L[i++]);
        i++; out.push(`<pre><code>${buf.join("\n")}</code></pre>`); continue;
      }
      if (!l.trim()) { i++; continue; }
      let m;
      if ((m = l.match(/^(#{1,4})\s+(.*)$/))) { const n = m[1].length; out.push(`<h${n}>${inline(m[2])}</h${n}>`); i++; continue; }
      if (/^\s*(-{3,}|\*{3,}|_{3,})\s*$/.test(l)) { out.push("<hr>"); i++; continue; }
      if (/^\s*\|.*\|\s*$/.test(l) && i + 1 < L.length && /^\s*\|?\s*:?-{2,}/.test(L[i + 1])) {
        const cells = (r) => r.trim().replace(/^\||\|$/g, "").split("|").map((c) => inline(c.trim()));
        const head = cells(l); i += 2; const rows = [];
        while (i < L.length && /^\s*\|.*\|\s*$/.test(L[i])) rows.push(cells(L[i++]));
        out.push(`<table><thead><tr>${head.map((c) => `<th>${c}</th>`).join("")}</tr></thead><tbody>${
          rows.map((r) => `<tr>${r.map((c) => `<td>${c}</td>`).join("")}</tr>`).join("")}</tbody></table>`);
        continue;
      }
      if (/^\s*&gt;\s?/.test(l)) {
        const buf = [];
        while (i < L.length && /^\s*&gt;\s?/.test(L[i])) buf.push(L[i++].replace(/^\s*&gt;\s?/, ""));
        out.push(`<blockquote>${inline(buf.join("<br>"))}</blockquote>`); continue;
      }
      if (/^\s*([-*+]|\d+[.)])\s+/.test(l)) {
        const ordered = /^\s*\d+[.)]\s+/.test(l); const items = [];
        while (i < L.length && (/^\s*([-*+]|\d+[.)])\s+/.test(L[i]) || (/^\s{2,}\S/.test(L[i]) && items.length))) {
          if (/^\s*([-*+]|\d+[.)])\s+/.test(L[i])) items.push(L[i].replace(/^\s*([-*+]|\d+[.)])\s+/, ""));
          else items[items.length - 1] += " " + L[i].trim();
          i++;
        }
        const tag = ordered ? "ol" : "ul";
        out.push(`<${tag}>${items.map((t) => `<li>${inline(t)}</li>`).join("")}</${tag}>`); continue;
      }
      const buf = [];
      while (i < L.length && L[i].trim() && !(buf.length && isBlockStart(L[i]))) buf.push(L[i++]);
      out.push(`<p>${inline(buf.join("<br>"))}</p>`);
    }
    return raw(out.join(""));
  }

  // ── Status chips ──
  async function refreshStatus() {
    try {
      const s = await api("/api/status"); state.status = s;
      const llm = s.llm_reachable === false ? html`<span class="chip chip-err" title="Check internet / OLLAMA_API_KEY">LLM offline</span>`
        : html`<span class="chip chip-ok" title="${s.llm_host}">● ${s.llm_model}</span>`;
      const idx = html`<span class="chip ${s.indexed_chunks ? "chip-ok" : "chip-warn"}">Index: ${s.indexed_chunks.toLocaleString("en-IN")} passages</span>`;
      const models = s.models_ready ? "" : html`<span class="chip chip-wait">Loading search models…</span>`;
      const proc = s.processing ? html`<span class="chip chip-wait">Indexing ${s.processing} file(s)…</span>` : "";
      mount($("#mastStatus"), html`${llm}${idx}${models}${proc}`);
    } catch { mount($("#mastStatus"), html`<span class="chip chip-err">Server unreachable</span>`); }
  }

  // ── Router ──
  const routes = { dashboard: viewDashboard, cases: viewCases, research: viewResearch, acts: viewActs,
    drafts: viewDrafts, limitation: viewLimitation, about: viewAbout };
  function parseHash() {
    const h = location.hash.replace(/^#\/?/, "") || "dashboard";
    const [path, qs] = h.split("?");
    const parts = path.split("/").map(decodeURIComponent);
    return { name: parts[0], arg: parts[1], q: new URLSearchParams(qs || "") };
  }
  async function route() {
    clearInterval(state.pollTimer);
    const r = parseHash();
    const fn = r.name === "cases" && r.arg ? viewCase : routes[r.name] || viewDashboard;
    $$("[data-nav]").forEach((a) => a.classList.toggle("active", a.dataset.nav === r.name));
    $("#navList").classList.remove("open");
    const main = $("#main");
    try { await fn(main, r); } catch (e) { mount(main, html`<div class="notice notice-red">${e.message}</div>`); }
    if (r.name !== "research") main.focus({ preventScroll: true });
  }
  function crumbs(items) {
    mount($("#crumbs"), html`<a href="#/dashboard">Home</a>${items.map(([t, href]) => html` › ${href ? html`<a href="${href}">${t}</a>` : t}`)}`);
  }

  // ── Login (only when APP_PASSWORD is set) ──
  function renderLogin() {
    crumbs([["Sign in"]]);
    mount($("#main"), html`<div class="card" style="max-width:420px;margin:40px auto">
      <div class="card-h"><h2>Sign in</h2></div>
      <form class="card-b stack" id="loginForm">
        <div><label class="f" for="pw">Password</label><input type="password" id="pw" autocomplete="current-password" required></div>
        <button class="btn" type="submit">Sign in</button></form></div>`);
    $("#loginForm").onsubmit = async (e) => {
      e.preventDefault();
      try { const r = await api("/api/login", { method: "POST", body: { password: $("#pw").value }, retry: false });
        state.csrf = r.csrf; route(); refreshStatus(); } catch (err) { toast(err.message, true); }
    };
  }

  // ── Dashboard ──
  async function viewDashboard(main) {
    crumbs([["Dashboard"]]);
    const d = await api("/api/dashboard");
    const lib = d.library || {};
    mount(main, html`
      <h1 class="page-title">Dashboard</h1>
      <p class="page-lead">Your matters, upcoming hearings and research at a glance.</p>
      <div class="banner">
        <div><h2>Ask a legal question, grounded in your files and the bare acts</h2>
          <p>Upload pleadings, judgments and agreements to a case, then research, summarise, build timelines or draft — every answer is cited.</p></div>
        <div class="row"><a class="btn btn-saffron" href="#/research">Start research</a><a class="btn btn-ghost" href="#/cases?new=1">New case</a></div>
      </div>
      <div class="grid g-4 tiles" style="margin-top:16px">
        ${tile("⚖", d.total_cases, "Case files", "#/cases")}
        ${tile("📄", lib.case || 0, "Case documents", "#/cases")}
        ${tile("📘", lib.bare_act || 0, "Bare Acts indexed", "#/acts")}
        ${tile("✍", lib.draft || 0, "Draft templates", "#/drafts")}
      </div>
      <div class="grid g-side" style="margin-top:16px">
        <div class="card"><div class="card-h"><h2>Upcoming hearings (45 days)</h2><a class="small" href="#/cases">All cases →</a></div>
          <div class="table-wrap">${d.upcoming.length ? html`<table class="gov"><thead><tr><th>Date</th><th>Matter</th><th>Type</th><th>Court</th></tr></thead><tbody>
            ${d.upcoming.map((c) => html`<tr class="clickable" data-href="#/cases/${encodeURIComponent(c.id)}"><td><strong>${fmtDate(c.next_hearing_date)}</strong><br><span class="small muted">in ${daysTo(c.next_hearing_date)} days</span></td>
              <td>${c.title}</td><td><span class="badge ${TYPE_BADGE[c.case_type] || ""}">${c.case_type}</span></td><td class="small">${c.court}</td></tr>`)}
            </tbody></table>` : html`<div class="empty">No hearings in the next 45 days.</div>`}</div></div>
        <div class="stack">
          <div class="card"><div class="card-h"><h2>Recent research</h2></div><div class="card-b">
            ${d.recent_chats.length ? html`<ul class="list-plain">${d.recent_chats.map((c) => html`<li><a href="#/research?${c.case_id ? `case=${encodeURIComponent(c.case_id)}&` : ""}chat=${encodeURIComponent(c.id)}">${c.title}</a><br>
              <span class="small muted">${c.case_title || "General research"} · ${fmtDate(c.updated_at)}</span></li>`)}</ul>` : html`<div class="empty">No research yet.</div>`}
          </div></div>
          <div class="card"><div class="card-h"><h2>Quick actions</h2></div><div class="card-b quick">
            <a class="btn btn-ghost" href="#/acts">🔎 Look up a section</a>
            <a class="btn btn-ghost" href="#/limitation">⏱ Compute a limitation deadline</a>
            <a class="btn btn-ghost" href="#/drafts">✍ Browse draft templates</a></div></div>
        </div>
      </div>`);
    bindRowLinks(main);
  }
  const tile = (ico, n, lbl, href) => html`<a class="card tile" href="${href}" style="text-decoration:none"><span class="tile-ico" aria-hidden="true">${ico}</span>
    <span><span class="tile-num">${Number(n || 0).toLocaleString("en-IN")}</span><br><span class="tile-lbl">${lbl}</span></span></a>`;
  function bindRowLinks(el) { $$("tr[data-href]", el).forEach((tr) => { tr.onclick = () => { location.hash = tr.dataset.href; }; }); }

  // ── Cases list ──
  async function viewCases(main, r) {
    crumbs([["Case Files"]]);
    const { cases, types, statuses } = await api("/api/cases");
    mount(main, html`
      <div class="row"><h1 class="page-title">Case Files</h1><span class="spacer"></span><button class="btn btn-saffron" id="newCaseBtn">＋ New case</button></div>
      <p class="page-lead">Each matter keeps its own documents, notes and research history. Documents of one case are never used to answer questions about another.</p>
      <div class="card ${r.q.get("new") ? "" : "hidden"}" id="newCaseCard" style="margin-bottom:16px">
        <div class="card-h"><h2>Register a new matter</h2></div>
        <form class="card-b" id="caseForm">${caseFormFields({}, types, statuses)}
          <div class="row" style="margin-top:12px"><button class="btn" type="submit">Create case</button><button class="btn btn-ghost" type="button" id="cancelNew">Cancel</button></div></form>
      </div>
      <div class="card"><div class="card-h"><h2>All matters (${cases.length})</h2>
        <div class="row"><select id="fType" aria-label="Filter by type"><option value="">All types</option>${types.map((t) => html`<option>${t}</option>`)}</select>
        <input type="search" id="fSearch" placeholder="Search title, number, client, court" aria-label="Search cases" style="min-width:240px"></div></div>
        <div class="table-wrap"><table class="gov"><thead><tr><th>Matter</th><th>Type</th><th>Case no.</th><th>Court / forum</th><th>Client</th><th>Next hearing</th><th>Docs</th><th>Status</th></tr></thead>
        <tbody id="caseRows"></tbody></table></div></div>`);
    const rows = () => {
      const t = $("#fType").value, q = $("#fSearch").value.toLowerCase();
      const list = cases.filter((c) => (!t || c.case_type === t) && (!q || [c.title, c.case_number, c.client_name, c.court, c.opposite_party].join(" ").toLowerCase().includes(q)));
      mount($("#caseRows"), list.length ? html`${list.map((c) => html`<tr class="clickable" data-href="#/cases/${encodeURIComponent(c.id)}">
        <td><strong>${c.title}</strong><br><span class="small muted">v. ${c.opposite_party || "—"}</span></td>
        <td><span class="badge ${TYPE_BADGE[c.case_type] || ""}">${c.case_type}</span></td><td class="small">${c.case_number || "—"}</td>
        <td class="small">${c.court || "—"}</td><td class="small">${c.client_name || "—"}</td><td class="small">${fmtDate(c.next_hearing_date)}</td>
        <td>${c.n_docs}</td><td class="small">${c.status}</td></tr>`)}` : html`<tr><td colspan="8"><div class="empty">No matching cases.</div></td></tr>`);
      bindRowLinks($("#caseRows"));
    };
    rows();
    $("#fType").onchange = rows; $("#fSearch").oninput = rows;
    $("#newCaseBtn").onclick = () => { $("#newCaseCard").classList.remove("hidden"); $("#f_title").focus(); };
    $("#cancelNew").onclick = () => $("#newCaseCard").classList.add("hidden");
    $("#caseForm").onsubmit = async (e) => {
      e.preventDefault();
      try { const c = await api("/api/cases", { method: "POST", body: formData(e.target) });
        toast("Case created"); location.hash = `#/cases/${encodeURIComponent(c.id)}`; } catch (err) { toast(err.message, true); }
    };
  }
  function caseFormFields(c, types, statuses) {
    const f = (id, label, type = "text", cls = "") => html`<div class="${cls}"><label class="f" for="f_${id}">${label}</label>
      <input type="${type}" id="f_${id}" name="${id}" value="${c[id] || ""}" ${id === "title" ? raw("required") : ""}></div>`;
    return html`<div class="form-grid">
      ${f("title", "Case title *", "text", "span-2")}
      <div><label class="f" for="f_case_type">Case type</label><select id="f_case_type" name="case_type">${types.map((t) => html`<option ${t === c.case_type ? raw("selected") : ""}>${t}</option>`)}</select></div>
      ${f("case_number", "Case / filing number")}${f("court", "Court / forum", "text", "span-2")}
      ${f("client_name", "Client name")}${f("client_role", "Client role (e.g. Petitioner)")}${f("opposite_party", "Opposite party")}
      ${f("filing_date", "Filing date", "date")}${f("next_hearing_date", "Next hearing", "date")}
      <div><label class="f" for="f_status">Status</label><select id="f_status" name="status">${statuses.map((s) => html`<option ${s === (c.status || "Active / In Court") ? raw("selected") : ""}>${s}</option>`)}</select></div>
      ${f("acts_sections", "Acts / sections involved", "text", "span-3")}
      <div class="span-3"><label class="f" for="f_description">Facts and background</label><textarea id="f_description" name="description" rows="4">${c.description || ""}</textarea></div>
    </div>`;
  }
  const formData = (form) => Object.fromEntries([...new FormData(form).entries()].map(([k, v]) => [k, String(v).trim()]));

  // ── Single case ──
  async function viewCase(main, r) {
    const id = r.arg;
    const [c, meta, docsRes] = await Promise.all([api(`/api/cases/${encodeURIComponent(id)}`), api("/api/cases"),
      api(`/api/documents?case_id=${encodeURIComponent(id)}`)]);
    crumbs([["Case Files", "#/cases"], [c.title]]);
    state.caseId = id; store.set("caseId", id);
    const types = docsRes.types;
    mount(main, html`
      <div class="row"><h1 class="page-title">${c.title}</h1><span class="badge ${TYPE_BADGE[c.case_type] || ""}">${c.case_type}</span>
        <span class="spacer"></span><a class="btn btn-saffron" href="#/research?case=${encodeURIComponent(id)}">💬 Research this case</a></div>
      <p class="page-lead">${c.case_number || "No case number"} · ${c.court || "Court not set"} · ${c.status}</p>
      <div class="grid g-side">
        <div class="stack">
          <div class="card dossier"><div class="card-h"><h2>Case dossier</h2><button class="btn btn-ghost btn-sm" id="editBtn">Edit</button></div>
            <div class="card-b" id="dossierView"><dl>
              <dt>Client</dt><dd>${c.client_name || "—"} (${c.client_role || "—"})</dd>
              <dt>Opposite party</dt><dd>${c.opposite_party || "—"}</dd>
              <dt>Filing date</dt><dd>${fmtDate(c.filing_date)}</dd>
              <dt>Next hearing</dt><dd>${fmtDate(c.next_hearing_date)}</dd>
              <dt>Acts / sections</dt><dd>${c.acts_sections || "—"}</dd>
              <dt>Facts</dt><dd>${c.description || "—"}</dd></dl></div>
            <form class="card-b hidden" id="dossierForm">${caseFormFields(c, meta.types, meta.statuses)}
              <div class="row" style="margin-top:12px"><button class="btn" type="submit">Save</button><button class="btn btn-ghost" type="button" id="cancelEdit">Cancel</button>
              <span class="spacer"></span><button class="btn btn-danger" type="button" id="deleteCase">Delete case</button></div></form>
          </div>
          <div class="card"><div class="card-h"><h2>Case documents</h2><span class="small muted">PDF · DOCX · TXT · MD — up to 40 MB each</span></div>
            <div class="card-b">
              <div class="row" style="margin-bottom:10px"><label class="f" for="docType" style="margin:0">Document type</label>
                <select id="docType" style="max-width:260px"><option value="auto">Detect automatically</option>${Object.entries(types).filter(([k]) => k !== "template").map(([k, v]) => html`<option value="${k}">${v}</option>`)}</select></div>
              <label class="dropzone" id="drop"><input type="file" id="fileInput" multiple accept=".pdf,.docx,.txt,.md">
                <strong>Drop files here or click to upload</strong><br><span class="small muted">Files are parsed, chunked and embedded on this computer. Text-based PDFs only (scanned PDFs need OCR first).</span></label>
              <div id="docList" style="margin-top:8px"></div>
            </div></div>
        </div>
        <div class="stack">
          <div class="card"><div class="card-h"><h2>Notes &amp; orders</h2></div><div class="card-b">
            <form id="noteForm" class="stack"><textarea id="noteText" placeholder="e.g. Court issued notice returnable on 28 Nov; reply in 2 weeks." aria-label="New note"></textarea>
              <button class="btn btn-sm" type="submit">Add note</button></form>
            <div style="margin-top:12px">${(c.notes || []).length ? c.notes.map((n) => html`<div class="note"><div class="small muted">${n.author} · ${fmtDate(n.created_at)}</div>${n.content}</div>`) : html`<div class="empty">No notes yet.</div>`}</div>
          </div></div>
          <div class="card"><div class="card-h"><h2>Ask about this case</h2></div><div class="card-b quick">
            ${["What are the strongest grounds for my client?", "What defences can the opposite party raise?", "List the key dates and limitation issues.", "Draft a legal notice for this matter."].map((q) =>
              html`<a class="btn btn-ghost" href="#/research?case=${encodeURIComponent(id)}&q=${encodeURIComponent(q)}">${q}</a>`)}
          </div></div>
        </div>
      </div>`);

    const renderDocs = (docs) => {
      mount($("#docList"), docs.length ? html`${docs.map((d) => html`<div class="doc-item">
        <span class="doc-ico">${(d.filename.split(".").pop() || "").toUpperCase()}</span>
        <div class="doc-main"><div class="doc-title">${d.title}</div>
          <div class="small muted">${types[d.doc_type] || d.doc_type} · ${d.pages ? `${d.pages} pages · ` : ""}${d.n_chunks ? `${d.n_chunks} passages · ` : ""}${fmtDate(d.created_at)}</div>
          ${d.status === "processing" ? html`<div class="status-line small"><span class="spinner"></span> Reading and indexing…</div>` : ""}
          ${d.status === "failed" ? html`<div class="small" style="color:var(--red)">⚠ ${d.error || "Failed"}</div>` : ""}
          <div class="doc-actions">
            ${d.status === "ready" ? html`
              <a class="btn btn-ghost btn-sm" href="#/research?case=${encodeURIComponent(id)}&doc=${encodeURIComponent(d.id)}&task=summary">Summarise</a>
              <a class="btn btn-ghost btn-sm" href="#/research?case=${encodeURIComponent(id)}&doc=${encodeURIComponent(d.id)}&task=timeline">Timeline</a>
              <a class="btn btn-ghost btn-sm" href="#/research?case=${encodeURIComponent(id)}&doc=${encodeURIComponent(d.id)}&task=issues">Issues</a>
              <a class="btn btn-ghost btn-sm" href="/api/documents/${encodeURIComponent(d.id)}/file" target="_blank" rel="noopener">Open</a>` : ""}
            <button class="btn btn-danger btn-sm" data-del="${d.id}">Delete</button></div></div></div>`)}` : html`<div class="empty">No documents uploaded for this case yet.</div>`);
      $$("[data-del]", $("#docList")).forEach((b) => { b.onclick = async () => {
        if (!confirm("Delete this document and its index? This cannot be undone.")) return;
        try { await api(`/api/documents/${encodeURIComponent(b.dataset.del)}`, { method: "DELETE" }); toast("Document deleted"); loadDocs(); } catch (e) { toast(e.message, true); }
      }; });
      clearInterval(state.pollTimer);
      if (docs.some((d) => d.status === "processing")) state.pollTimer = setInterval(loadDocs, 2500);
    };
    const loadDocs = async () => { try { renderDocs((await api(`/api/documents?case_id=${encodeURIComponent(id)}`)).documents); refreshStatus(); } catch { /* transient */ } };
    renderDocs(docsRes.documents);

    const upload = async (files) => {
      for (const f of files) {
        const fd = new FormData(); fd.append("file", f); fd.append("doc_type", $("#docType").value);
        try { const d = await api(`/api/cases/${encodeURIComponent(id)}/documents`, { method: "POST", form: fd });
          toast(d.duplicate ? `“${f.name}” is already in this case` : `Uploaded “${f.name}” — indexing…`); }
        catch (e) { toast(`${f.name}: ${e.message}`, true); }
      }
      loadDocs();
    };
    const drop = $("#drop");
    $("#fileInput").onchange = (e) => { upload([...e.target.files]); e.target.value = ""; };
    ["dragenter", "dragover"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.add("drag"); }));
    ["dragleave", "drop"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.remove("drag"); }));
    drop.addEventListener("drop", (e) => upload([...e.dataTransfer.files]));

    $("#editBtn").onclick = () => { $("#dossierView").classList.add("hidden"); $("#dossierForm").classList.remove("hidden"); };
    $("#cancelEdit").onclick = () => { $("#dossierForm").classList.add("hidden"); $("#dossierView").classList.remove("hidden"); };
    $("#dossierForm").onsubmit = async (e) => {
      e.preventDefault();
      try { await api(`/api/cases/${encodeURIComponent(id)}`, { method: "PATCH", body: formData(e.target) }); toast("Saved"); route(); } catch (err) { toast(err.message, true); }
    };
    $("#deleteCase").onclick = async () => {
      if (!confirm("Delete this case with all its documents, notes and research? This cannot be undone.")) return;
      try { await api(`/api/cases/${encodeURIComponent(id)}`, { method: "DELETE" }); state.caseId = null; store.set("caseId", null); toast("Case deleted"); location.hash = "#/cases"; } catch (err) { toast(err.message, true); }
    };
    $("#noteForm").onsubmit = async (e) => {
      e.preventDefault(); const t = $("#noteText").value.trim(); if (!t) return;
      try { await api(`/api/cases/${encodeURIComponent(id)}/notes`, { method: "POST", body: { content: t } }); route(); } catch (err) { toast(err.message, true); }
    };
  }

  // ── Research (chat) ──
  async function viewResearch(main, r) {
    const qCase = r.q.get("case");
    if (qCase !== null) { state.caseId = qCase || null; store.set("caseId", state.caseId); }
    state.chatId = r.q.get("chat") || null;
    const { cases } = await api("/api/cases");
    if (state.caseId && !cases.some((c) => c.id === state.caseId)) state.caseId = null;
    const cur = cases.find((c) => c.id === state.caseId);
    if (!cur && state.scope === "case") state.scope = "all";
    crumbs([["Legal Research"], ...(cur ? [[cur.title, `#/cases/${encodeURIComponent(cur.id)}`]] : [])]);
    mount(main, html`
      <h1 class="page-title">Legal Research</h1>
      <p class="page-lead">Agentic RAG over ${cur ? "this case's documents and " : ""}the law library — answers cite numbered sources and are audited against them.</p>
      <div class="research">
        <aside class="side" aria-label="Research settings">
          <div class="card"><div class="card-h"><h3>Matter</h3></div><div class="card-b">
            <select id="caseSel" aria-label="Select case"><option value="">General research (no case)</option>${cases.map((c) => html`<option value="${c.id}" ${c.id === state.caseId ? raw("selected") : ""}>[${c.case_type}] ${c.title}</option>`)}</select>
            <div class="scope" role="radiogroup" aria-label="Search scope" style="margin-top:10px">
              <label><input type="radio" name="scope" value="all" ${state.scope === "all" ? raw("checked") : ""}> <span><strong>Case + Library</strong><br><span class="small muted">Case files and bare acts</span></span></label>
              <label><input type="radio" name="scope" value="case" ${state.scope === "case" ? raw("checked") : ""} ${cur ? "" : raw("disabled")}> <span><strong>Case files only</strong><br><span class="small muted">Facts from uploaded documents</span></span></label>
              <label><input type="radio" name="scope" value="library" ${state.scope === "library" ? raw("checked") : ""}> <span><strong>Law library only</strong><br><span class="small muted">Bare acts, judgments, templates</span></span></label>
            </div>
            <div id="caseDocs" class="small" style="margin-top:8px"></div></div></div>
          <div class="card"><div class="card-h"><h3>Conversations</h3><button class="btn btn-sm" id="newChat">＋ New</button></div>
            <ul class="chat-list" id="chatList"></ul></div>
        </aside>
        <section class="card chatbox" aria-label="Conversation">
          <div class="thread" id="thread" aria-live="polite"></div>
          <form class="composer" id="composer">
            <div class="suggest" id="suggest"></div>
            <div class="row" style="align-items:flex-end;flex-wrap:nowrap">
              <textarea id="q" placeholder="Ask a legal question, request a draft, or ask for a summary / timeline of a document…" aria-label="Your question" rows="2"></textarea>
              <button class="btn btn-saffron" type="submit" id="sendBtn" style="height:54px">Send ➤</button></div>
            <div class="small muted" style="margin-top:6px">Enter to send · Shift+Enter for a new line · ${cur ? "Using case dossier + documents" : "Select a case to use its documents"}</div>
          </form>
        </section>
        <aside class="card sources-col" aria-label="Sources"><div class="card-h"><h3>Sources</h3><span class="small muted" id="srcCount"></span></div>
          <div class="card-b" id="sourcesPanel"><div class="empty">Sources used for the latest answer appear here.</div></div></aside>
      </div>`);

    $("#caseSel").onchange = (e) => { location.hash = `#/research?case=${encodeURIComponent(e.target.value)}`; };
    $$("input[name=scope]").forEach((el) => { el.onchange = () => { state.scope = el.value; store.set("scope", el.value); }; });
    $("#newChat").onclick = () => {
      const h = `#/research?case=${encodeURIComponent(state.caseId || "")}`;
      if (location.hash === h) route(); else location.hash = h;
    };
    const qEl = $("#q");
    qEl.addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); $("#composer").requestSubmit(); } });
    $("#composer").onsubmit = (e) => { e.preventDefault(); const t = qEl.value.trim(); if (!t || state.streaming) return; qEl.value = ""; send(t); };
    $("#thread").addEventListener("click", onCiteClick);
    $("#sourcesPanel").addEventListener("click", (e) => { const card = e.target.closest("[data-src]"); if (card) openSource(JSON.parse(card.dataset.src)); });

    const suggestions = cur ? ["Summarise the main document in this case", "Build a timeline of events from the documents", "What are the strongest defences available to the opposite party?", `Draft a ${cur.case_type === "Criminal" ? "bail application" : "legal notice"} for this case`]
      : ["What is the punishment for cheating under BNS and the old IPC?", "Explain Order XXXIX Rule 1 CPC (temporary injunction)", "Ingredients of an offence under Section 138 NI Act", "Limitation for breach of contract broken on 2024-03-15"];
    mount($("#suggest"), html`${suggestions.map((s) => html`<button type="button" data-s="${s}">${s}</button>`)}`);
    $$("[data-s]", $("#suggest")).forEach((b) => { b.onclick = () => { if (!state.streaming) send(b.dataset.s); }; });

    loadChatList();
    if (cur) loadCaseDocs(cur.id);
    if (state.chatId) await loadChat(state.chatId);
    else mount($("#thread"), html`<div class="empty" id="welcome"><strong>Namaste.</strong> Ask anything about ${cur ? html`<em>${cur.title}</em> or ` : ""}Indian law.<br>Examples: “Is anticipatory bail available for an offence under Section 318 BNS?”, “Summarise the judgment”, “Draft a reply to the legal notice”.</div>`);

    const doc = r.q.get("doc"), task = r.q.get("task"), pre = r.q.get("q");
    history.replaceState(null, "", `#/research?case=${encodeURIComponent(state.caseId || "")}${state.chatId ? `&chat=${encodeURIComponent(state.chatId)}` : ""}`);
    if (doc && task) {
      const label = { summary: "Summarise", timeline: "Build a timeline of", issues: "Identify the legal issues in" }[task] || "Analyse";
      let title = "this document"; try { title = `“${(await api(`/api/documents/${encodeURIComponent(doc)}`)).title}”`; } catch { /* keep generic */ }
      send(`${label} ${title}`, { doc_id: doc, task });
    } else if (pre) { qEl.value = pre; qEl.focus(); }
    else qEl.focus();
  }

  async function loadCaseDocs(caseId) {
    try {
      const { documents } = await api(`/api/documents?case_id=${encodeURIComponent(caseId)}`);
      mount($("#caseDocs"), documents.length ? html`<div class="muted" style="margin-bottom:4px">Documents in this case:</div>${documents.slice(0, 8).map((d) => html`<div>• ${d.title} ${d.status !== "ready" ? html`<span class="chip ${d.status === "failed" ? "chip-err" : "chip-wait"}">${d.status}</span>` : ""}</div>`)}`
        : html`<div class="notice">No documents yet — <a href="#/cases/${encodeURIComponent(caseId)}">upload to this case</a>.</div>`);
    } catch { /* ignore */ }
  }

  async function loadChatList() {
    try {
      const { chats } = await api(`/api/chats${state.caseId ? `?case_id=${encodeURIComponent(state.caseId)}` : ""}`);
      mount($("#chatList"), chats.length ? html`${chats.map((c) => html`<li><a href="#/research?case=${encodeURIComponent(state.caseId || "")}&chat=${encodeURIComponent(c.id)}" class="${c.id === state.chatId ? "active" : ""}" title="${c.title}">${c.title}</a>
        <button type="button" data-delchat="${c.id}" aria-label="Delete conversation" title="Delete">✕</button></li>`)}` : html`<li class="empty small">No conversations yet</li>`);
      $$("[data-delchat]", $("#chatList")).forEach((b) => { b.onclick = async () => {
        if (!confirm("Delete this conversation?")) return;
        await api(`/api/chats/${encodeURIComponent(b.dataset.delchat)}`, { method: "DELETE" });
        if (b.dataset.delchat === state.chatId) location.hash = `#/research?case=${encodeURIComponent(state.caseId || "")}`; else loadChatList();
      }; });
    } catch { /* ignore */ }
  }

  async function loadChat(chatId) {
    const { messages } = await api(`/api/chats/${encodeURIComponent(chatId)}`);
    const thread = $("#thread"); thread.innerHTML = "";
    let last = null;
    for (const m of messages) {
      if (m.role === "user") addUserMsg(m.content);
      else { const el = addAiMsg(); renderAi(el, m.content, m.sources, m.meta || {}); last = m; }
    }
    if (last) renderSourcesPanel(last.sources || []);
    thread.scrollTop = thread.scrollHeight;
  }

  function addUserMsg(text) {
    $("#welcome")?.remove();
    const el = document.createElement("div"); el.className = "msg msg-user"; el.textContent = text;
    $("#thread").appendChild(el); return el;
  }
  function addAiMsg() {
    const el = document.createElement("div"); el.className = "msg msg-ai"; el.dataset.mid = String(++state.msgSeq);
    $("#thread").appendChild(el); return el;
  }
  const INTENT = { research: "Research", drafting: "Draft", analyze_document: "Document analysis", calculation: "Limitation tool", chitchat: "Chat" };

  function renderAi(el, content, sources, meta, live = null) {
    state.msgSources.set(el.dataset.mid, sources || []);
    const v = meta.verify;
    const metaRow = html`<div class="msg-meta">
      ${meta.intent ? html`<span class="chip">${INTENT[meta.intent] || meta.intent}</span>` : ""}
      ${sources && sources.length ? html`<span class="chip">${sources.length} source${sources.length > 1 ? "s" : ""}</span>` : ""}
      ${meta.elapsed ? html`<span>${meta.elapsed}s</span>` : ""}</div>`;
    const status = live ? html`<div class="status-line"><span class="spinner"></span><span>${live}</span></div>` : "";
    const verify = v ? (v.grounded
      ? html`<div class="verify verify-ok">✓ Citation audit passed — every cited claim was checked against its source.</div>`
      : html`<div class="verify verify-warn">⚠ Citation audit: please verify these points.${v.unsupported && v.unsupported.length ? html`<ul>${v.unsupported.map((u) => html`<li>${u}</li>`)}</ul>` : ""}</div>`) : "";
    const err = meta.error && meta.error !== "internal" && !content.includes(meta.error) ? html`<div class="notice notice-red">${meta.error}</div>` : "";
    const disclaimer = !live && meta.intent && meta.intent !== "chitchat" ? html`<div class="small muted">AI research aid, not legal advice — verify citations in the primary source.</div>` : "";
    mount(el, html`${metaRow}<div class="md">${markdown(content)}</div>${status}<div class="msg-foot">${err}${verify}${disclaimer}</div>`);
  }

  function renderSourcesPanel(sources) {
    $("#srcCount").textContent = sources.length ? `${sources.length} used` : "";
    mount($("#sourcesPanel"), sources.length ? html`${sources.map((s) => html`<div class="src-card" role="button" tabindex="0" data-src="${JSON.stringify(s)}">
      <div><span class="src-n">${s.n}</span><span class="badge b-${s.collection}">${COLL_LABEL[s.collection] || s.kind}</span> ${s.exact ? html`<span class="chip chip-ok" title="Exact match for the cited provision">exact</span>` : ""}</div>
      <div class="src-title" style="margin-top:4px">${s.title}</div>
      <div class="small muted">${[s.label, s.page && !/^pp?\./.test(s.label || "") ? `p. ${s.page}` : ""].filter(Boolean).join(" · ")}</div>
      <div class="src-snip">${s.snippet}</div></div>`)}` : html`<div class="empty">No sources for this answer.</div>`);
  }

  function onCiteClick(e) {
    const b = e.target.closest("button.cite"); if (!b) return;
    const msg = b.closest(".msg-ai"); const list = state.msgSources.get(msg?.dataset.mid) || [];
    const s = list.find((x) => String(x.n) === b.dataset.cite);
    if (s) openSource(s); else toast("That source number is not in this answer's sources.", true);
  }

  async function openSource(s) {
    const body = $("#drawerBody");
    $("#drawerTitle").textContent = `Source [${s.n}] — ${COLL_LABEL[s.collection] || s.kind}`;
    const fileLink = `/api/documents/${encodeURIComponent(s.doc_id)}/file${s.page ? `#page=${s.page}` : ""}`;
    mount(body, html`<h3 style="margin-top:0;color:var(--navy)">${s.title}</h3><p class="small muted">${[s.label, s.page ? `page ${s.page}` : ""].filter(Boolean).join(" · ")}</p>
      <div id="passage"><div class="status-line"><span class="spinner"></span> Loading passage…</div></div>
      <div class="row" style="margin-top:12px"><a class="btn" href="${fileLink}" target="_blank" rel="noopener">Open original document${s.page ? ` at p. ${s.page}` : ""} ↗</a></div>`);
    showDrawer();
    if (s.chunk_id) {
      try { const c = await api(`/api/chunks/${encodeURIComponent(s.chunk_id)}`);
        mount($("#passage"), html`<div class="small muted" style="margin-bottom:6px">${c.header}</div><pre class="passage">${c.text}</pre>`); }
      catch (e) { mount($("#passage"), html`<div class="notice notice-red">${e.message}</div>`); }
    } else mount($("#passage"), html`<pre class="passage">${s.snippet}</pre>`);
  }
  function showDrawer() { const d = $("#drawer"); d.hidden = false; $("#drawerClose").focus(); }
  function hideDrawer() { $("#drawer").hidden = true; }

  async function send(text, extra = {}) {
    if (state.streaming) return;
    state.streaming = true; $("#sendBtn").disabled = true;
    addUserMsg(text);
    const el = addAiMsg(); const thread = $("#thread");
    let content = "", sources = [], meta = {}, live = "Starting…", pending = false;
    const paint = () => { pending = false; renderAi(el, content, sources, meta, live); thread.scrollTop = thread.scrollHeight; };
    const schedule = () => { if (!pending) { pending = true; requestAnimationFrame(paint); } };
    paint();
    try {
      const r = await fetch("/api/chat", { method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/json", "X-Nyaya-CSRF": state.csrf || "" },
        body: JSON.stringify({ message: text, chat_id: state.chatId, case_id: state.caseId, scope: state.scope, ...extra }) });
      if (!r.ok) { let m = ""; try { m = (await r.json()).detail; } catch { /* */ } throw new Error(m || `Request failed (${r.status})`); }
      const reader = r.body.getReader(); const dec = new TextDecoder(); let buf = "";
      for (;;) {
        const { value, done } = await reader.read(); if (done) break;
        buf += dec.decode(value, { stream: true });
        let i;
        while ((i = buf.indexOf("\n\n")) >= 0) {
          const line = buf.slice(0, i); buf = buf.slice(i + 2);
          if (!line.startsWith("data: ")) continue;
          let evt; try { evt = JSON.parse(line.slice(6)); } catch { continue; }
          switch (evt.type) {
            case "chat": if (state.chatId !== evt.chat_id) { state.chatId = evt.chat_id; history.replaceState(null, "", `#/research?case=${encodeURIComponent(state.caseId || "")}&chat=${encodeURIComponent(evt.chat_id)}`); } break;
            case "status": live = evt.text; break;
            case "meta": meta.intent = evt.intent; break;
            case "sources": sources = evt.items; renderSourcesPanel(sources); break;
            case "token": content += evt.text; if (live && !/Checking/.test(live)) live = "Writing…"; break;
            case "verify": meta.verify = evt; break;
            case "error": meta.error = evt.text; break;
            case "done": meta = { ...meta, ...(evt.meta || {}), elapsed: evt.elapsed }; live = null; break;
          }
          schedule();
        }
      }
    } catch (e) {
      meta.error = e.message; if (!content) content = "";
    } finally {
      live = null; paint();
      state.streaming = false; $("#sendBtn").disabled = false; $("#q")?.focus();
      loadChatList(); refreshStatus();
    }
  }

  // ── Bare acts ──
  async function viewActs(main, r) {
    crumbs([["Bare Acts"]]);
    const lib = await api("/api/library");
    const acts = lib.bare_act;
    mount(main, html`
      <h1 class="page-title">Bare Acts</h1>
      <p class="page-lead">Official text from India Code, indexed provision-by-provision. Look up any section instantly — no AI involved.</p>
      <div class="card"><div class="card-h"><h2>Provision lookup</h2></div>
        <form class="card-b row" id="lookupForm" style="align-items:flex-end">
          <div style="flex:2;min-width:240px"><label class="f" for="lq">Section, article, rule or keywords</label><input type="search" id="lq" placeholder="e.g. Section 138 NI Act · Order 39 Rule 1 CPC · Article 55 Limitation · anticipatory bail" value="${r.q.get("q") || ""}"></div>
          <div style="flex:1;min-width:200px"><label class="f" for="lact">Act</label><select id="lact"><option value="">All acts</option>${acts.map((a) => html`<option value="${a.title}">${a.title}</option>`)}</select></div>
          <button class="btn" type="submit">Search</button></form>
        <div id="lookupResults"></div></div>
      <h2 style="color:var(--navy);font-size:1.05rem;margin:22px 0 10px">Acts in the library (${acts.length})</h2>
      <div class="grid g-3">${acts.map((a) => html`<div class="card act-card">
        <h3>${a.title}</h3>
        <div class="row">${a.meta.short ? html`<span class="chip">${a.meta.short}</span>` : ""}${(a.meta.areas || []).map((x) => html`<span class="badge ${TYPE_BADGE[x] || ""}">${x}</span>`)}
          ${a.meta.repealed ? html`<span class="chip chip-warn" title="${a.meta.note || "Repealed"}">Repealed</span>` : ""}
          ${a.status !== "ready" ? html`<span class="chip chip-wait">${a.status}</span>` : ""}</div>
        <div class="small muted">${a.pages || "?"} pages · ${a.n_chunks || 0} indexed passages${a.meta.act_number ? ` · Act ${a.meta.act_number} of ${a.meta.year}` : ""}</div>
        <div class="row"><a class="btn btn-ghost btn-sm" href="/api/documents/${encodeURIComponent(a.id)}/file" target="_blank" rel="noopener">Open PDF ↗</a>
          <a class="btn btn-ghost btn-sm" href="#/research?case=&q=${encodeURIComponent(`Under the ${a.title}, `)}">Ask about this Act</a></div></div>`)}</div>
      ${acts.length ? "" : html`<div class="notice">The library is empty. Run <code>python -m nyaya.ingest library</code> once to index the bare acts.</div>`}`);
    const run = async () => {
      const q = $("#lq").value.trim(); if (!q) return;
      mount($("#lookupResults"), html`<div class="card-b status-line"><span class="spinner"></span> Searching…</div>`);
      try {
        const res = await api(`/api/library/lookup?q=${encodeURIComponent(q)}${$("#lact").value ? `&act=${encodeURIComponent($("#lact").value)}` : ""}`);
        mount($("#lookupResults"), res.results.length ? html`${res.results.map((x) => html`<div class="result"><h3>${x.label ? html`<span class="chip ${x.exact ? "chip-ok" : ""}">${x.label}</span> ` : ""}${x.header.split(" — ").slice(1).join(" — ") || x.header}</h3>
          <div class="small muted" style="margin-bottom:6px">${x.title}${x.page ? ` · p. ${x.page}` : ""} · <a href="/api/documents/${encodeURIComponent(x.doc_id)}/file#page=${x.page || 1}" target="_blank" rel="noopener">open PDF ↗</a></div><pre>${x.text}</pre></div>`)}`
          : html`<div class="card-b empty">No matching provisions. Try different words or pick an act.</div>`);
      } catch (e) { mount($("#lookupResults"), html`<div class="card-b notice notice-red">${e.message}</div>`); }
    };
    $("#lookupForm").onsubmit = (e) => { e.preventDefault(); run(); };
    if (r.q.get("q")) run();
  }

  // ── Draft templates ──
  async function viewDrafts(main) {
    crumbs([["Draft Templates"]]);
    const [lib, { cases }] = await Promise.all([api("/api/library"), api("/api/cases")]);
    const drafts = lib.draft; const areas = ["All", ...new Set(drafts.map((d) => d.meta.area || "General"))];
    let area = "All";
    mount(main, html`
      <h1 class="page-title">Draft Templates</h1>
      <p class="page-lead">Model drafts for Civil, Criminal, Family and Revenue practice. Open one to read it, or let the assistant adapt it to a case.</p>
      <div class="tabs" role="tablist">${areas.map((a) => html`<button type="button" role="tab" data-area="${a}" class="${a === "All" ? "active" : ""}">${a}</button>`)}</div>
      <div class="grid g-3" id="draftGrid"></div>`);
    const grid = () => {
      const list = drafts.filter((d) => area === "All" || (d.meta.area || "General") === area);
      mount($("#draftGrid"), list.length ? html`${list.map((d) => html`<div class="card act-card"><h3>${d.title}</h3>
        <div class="row"><span class="badge ${TYPE_BADGE[d.meta.area] || "b-draft"}">${d.meta.area || "General"}</span></div>
        <div class="row"><button class="btn btn-ghost btn-sm" data-open="${d.id}">Read template</button></div></div>`)}` : html`<div class="empty">No templates.</div>`);
      $$("[data-open]", $("#draftGrid")).forEach((b) => { b.onclick = () => openDraft(drafts.find((d) => d.id === b.dataset.open), cases); });
    };
    $$("[data-area]").forEach((b) => { b.onclick = () => { area = b.dataset.area; $$("[data-area]").forEach((x) => x.classList.toggle("active", x === b)); grid(); }; });
    grid();
  }
  async function openDraft(d, cases) {
    $("#drawerTitle").textContent = "Draft template";
    mount($("#drawerBody"), html`<div class="status-line"><span class="spinner"></span> Loading…</div>`); showDrawer();
    try {
      const t = await api(`/api/documents/${encodeURIComponent(d.id)}/text`);
      mount($("#drawerBody"), html`<div class="card" style="margin-bottom:12px"><div class="card-b row">
          <select id="useCase" aria-label="Case to draft for"><option value="">Choose a case…</option>${cases.map((c) => html`<option value="${c.id}">[${c.case_type}] ${c.title}</option>`)}</select>
          <button class="btn btn-saffron btn-sm" id="useBtn">Draft for this case</button></div></div>
        <div class="md">${markdown(t.text)}</div>`);
      $("#useBtn").onclick = () => {
        const c = $("#useCase").value; if (!c) { toast("Choose a case first", true); return; }
        hideDrawer(); location.hash = `#/research?case=${encodeURIComponent(c)}&q=${encodeURIComponent(`Draft a ${d.title} for this case using the case dossier and documents.`)}`;
      };
    } catch (e) { mount($("#drawerBody"), html`<div class="notice notice-red">${e.message}</div>`); }
  }

  // ── Limitation ──
  async function viewLimitation(main) {
    crumbs([["Limitation"]]);
    const { categories, caveat } = await api("/api/limitation");
    const today = new Date().toISOString().slice(0, 10);
    mount(main, html`
      <h1 class="page-title">Limitation Calculator</h1>
      <p class="page-lead">Deterministic date arithmetic in Python — the language model is never used for deadlines.</p>
      <div class="grid g-side">
        <div class="card"><div class="card-h"><h2>Compute a deadline</h2></div>
          <form class="card-b stack" id="limForm">
            <div><label class="f" for="lc">Proceeding</label><select id="lc">${categories.map((c) => html`<option value="${c.key}">[${c.area}] ${c.label} — ${c.period}</option>`)}</select></div>
            <div><label class="f" for="ld">Start date</label><input type="date" id="ld" value="${today}" required><div class="small muted" id="lstarts"></div></div>
            <button class="btn" type="submit">Calculate</button></form>
          <div id="limResult"></div></div>
        <div class="card"><div class="card-h"><h2>Important</h2></div><div class="card-b small">${caveat}</div></div>
      </div>
      <div class="card" style="margin-top:16px"><div class="card-h"><h2>Supported periods</h2></div><div class="table-wrap"><table class="gov">
        <thead><tr><th>Proceeding</th><th>Period</th><th>Starts from</th><th>Basis</th></tr></thead>
        <tbody>${categories.map((c) => html`<tr><td>${c.label} <span class="badge ${TYPE_BADGE[c.area] || ""}">${c.area}</span></td><td>${c.period}</td><td class="small">${c.starts}</td><td class="small">${c.basis}</td></tr>`)}</tbody></table></div></div>`);
    const starts = () => { const c = categories.find((x) => x.key === $("#lc").value); $("#lstarts").textContent = c ? `Period starts: ${c.starts}` : ""; };
    $("#lc").onchange = starts; starts();
    $("#limForm").onsubmit = async (e) => {
      e.preventDefault();
      try {
        const r = await api("/api/limitation", { method: "POST", body: { cause: $("#lc").value, start_date: $("#ld").value } });
        mount($("#limResult"), html`<div class="card-b"><div class="notice ${r.days_left >= 0 ? "notice-green" : "notice-red"}">
          <div class="small">${r.label} · ${r.period} · ${r.basis}</div>
          <div style="font-size:1.4rem;font-weight:800;color:var(--navy);margin:4px 0">Last date: ${fmtDate(r.deadline)}</div>
          <div><strong>${r.status}</strong> (start date ${fmtDate(r.start_date)})</div></div></div>`);
      } catch (err) { toast(err.message, true); }
    };
  }

  // ── About ──
  async function viewAbout(main) {
    crumbs([["About"]]);
    const s = state.status || await api("/api/status");
    mount(main, html`
      <h1 class="page-title">About NyaySutra AI</h1>
      <div class="grid g-2">
        <div class="card"><div class="card-h"><h2>How an answer is produced</h2></div><div class="card-b md">${markdown(
`1. **Plan** — a fast model classifies the request (research, drafting, document analysis, limitation, chat), rewrites it as a standalone query and adds old/new-code equivalents (IPC↔BNS, CrPC↔BNSS, Evidence Act↔BSA).
2. **Retrieve** — four signals are fused with weighted Reciprocal Rank Fusion: exact provision lookup (*Section 138 NI Act*), BM25 keyword search (SQLite FTS5), dense vectors (local embeddings) and an act-focused vector search.
3. **Rerank** — a cross-encoder scores each passage against the question; weak evidence triggers an automatic query rewrite, and if evidence is still weak the assistant declines instead of guessing.
4. **Generate** — the answer is written only from numbered sources and streamed live.
5. **Audit** — every cited claim is checked against its source; unsupported points are flagged under the answer.`)}</div></div>
        <div class="card"><div class="card-h"><h2>Configuration</h2></div><div class="card-b"><table class="gov"><tbody>
          <tr><td>Answer model</td><td>${s.llm_model} (${s.llm_host})</td></tr><tr><td>Fast model</td><td>${s.fast_model}</td></tr>
          <tr><td>Embeddings</td><td>${s.embed_model} (local)</td></tr><tr><td>Reranker</td><td>${s.reranker || "disabled"} (local)</td></tr>
          <tr><td>Indexed passages</td><td>${(s.indexed_chunks || 0).toLocaleString("en-IN")}</td></tr>
          <tr><td>Storage</td><td>SQLite on this computer (data/nyaysutra.db)</td></tr></tbody></table></div></div>
        <div class="card"><div class="card-h"><h2>Chunking strategy</h2></div><div class="card-b md">${markdown(
`- **Bare acts** — one passage per Section / Article / Order-Rule, tagged with act, chapter, number and title, so provisions can be retrieved exactly. Long provisions are split at sub-section boundaries and re-joined at answer time.
- **Judgments** — numbered paragraphs grouped into ~1,100-character passages ("paras 12–15") with page numbers.
- **Contracts & pleadings** — numbered clauses grouped the same way.
- **Everything else** — sentence-aware windows with overlap.`)}</div></div>
        <div class="card"><div class="card-h"><h2>Security &amp; privacy</h2></div><div class="card-b md">${markdown(
`- Runs on **127.0.0.1** only; a password is required before it can listen on a network.
- Session cookie (HttpOnly, SameSite=Strict) + CSRF token + Host/Origin checks block other websites from using the app.
- Uploads: type allow-list, content signature check, size limit, server-generated file names; PDFs parsed with PDFium.
- Strict Content-Security-Policy, no third-party scripts; all rendered text is escaped.
- Documents, embeddings and chats stay on this computer. Only the question and the selected passages are sent to the LLM.
- Matter isolation: a case's documents are only searched for that case.`)}</div></div>
      </div>`);
  }

  // ── Global wiring ──
  function applyFont(step) {
    const sizes = [13, 14, 15, 16, 17, 18]; let i = store.get("font", 2);
    i = step === 0 ? 2 : Math.max(0, Math.min(sizes.length - 1, i + step)); store.set("font", i);
    document.documentElement.style.setProperty("--fs", sizes[i] + "px");
  }
  function applyContrast(on) {
    document.documentElement.dataset.contrast = on ? "on" : "off"; $("#contrastBtn").setAttribute("aria-pressed", String(on)); store.set("contrast", on);
  }
  document.addEventListener("DOMContentLoaded", async () => {
    $$("[data-font]").forEach((b) => { b.onclick = () => applyFont(Number(b.dataset.font)); });
    document.documentElement.style.setProperty("--fs", [13, 14, 15, 16, 17, 18][store.get("font", 2)] + "px");
    applyContrast(store.get("contrast", false));
    $("#contrastBtn").onclick = () => applyContrast(document.documentElement.dataset.contrast !== "on");
    $("#navToggle").onclick = () => { const ul = $("#navList"); ul.classList.toggle("open"); $("#navToggle").setAttribute("aria-expanded", String(ul.classList.contains("open"))); };
    $("#drawerClose").onclick = hideDrawer;
    $("#drawer").addEventListener("click", (e) => { if (e.target.id === "drawer") hideDrawer(); });
    document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !$("#drawer").hidden) hideDrawer(); });
    $("#logoutBtn").onclick = async () => { await api("/api/logout", { method: "POST" }); location.reload(); };
    window.addEventListener("hashchange", route);
    try {
      const s = await initSession();
      if (!s.authenticated) { renderLogin(); return; }
    } catch { mount($("#main"), html`<div class="notice notice-red">Cannot reach the NyaySutra server. Is <code>python run.py</code> running?</div>`); return; }
    refreshStatus(); setInterval(refreshStatus, 30000);
    route();
  });
})();
