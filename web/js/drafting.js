/* Modules 6, 9, 11 — document-aware drafting studio, legal notices, court/forum customisation, editor and export. */
import { $, $$, aiConsent, api, confirmDialog, crumbs, download, fmtDateTime, formData, html, markdown, mount, pollJob,
  progressBar, raw, spinner, state, toast } from "./core.js";
import { lang, t } from "./i18n.js";

const ORIGIN_CLS = { file: "o-file", user: "o-user", case: "o-case" };
let cfgCache = null;
const config = async () => (cfgCache ||= await api("/api/drafting/config"));

// ── Studio ──
export async function viewDrafting(main, r) {
  if (r.q.get("draft")) return viewEditor(main, r.q.get("draft"));
  crumbs([[t("nav.drafting")]]);
  const [cfg, { cases }, recent, lib] = await Promise.all([config(), api("/api/cases"), api("/api/drafts"), api("/api/library")]);
  const caseId = r.q.get("case") || "";
  const cur = cases.find((c) => c.id === caseId);
  const types = cfg.types.filter((x) => x.kind === "draft");
  mount(main, html`
    <div class="row"><h1 class="page-title">${t("drafting.title")}</h1><span class="spacer"></span><a class="btn btn-ghost" href="#/notice${caseId ? `?case=${encodeURIComponent(caseId)}` : ""}">✉ ${t("qa.notice")}</a></div>
    <p class="page-lead">${t("drafting.lead")}</p>
    <div class="notice" style="margin-bottom:14px">${t("drafting.review_label")}</div>
    <div class="grid g-side">
      <form class="card" id="dForm" novalidate><div class="card-h"><h2>${t("drafting.setup")}</h2></div><div class="card-b stack">
        <div class="form-grid">
          <div class="span-3"><label class="f" for="d_case">${t("drafting.case")}</label><select id="d_case" name="case_id"><option value="">${t("drafting.no_case")}</option>${cases.map((c) => html`<option value="${c.id}" ${c.id === caseId ? raw("selected") : ""}>[${c.case_type}] ${c.title}</option>`)}</select></div>
          <div><label class="f" for="d_type">${t("drafting.doc_type")}</label><select id="d_type" name="doc_type">${types.map((x) => html`<option value="${x.id}">${x.label}</option>`)}</select></div>
          <div><label class="f" for="d_forum">${t("drafting.forum")}</label><select id="d_forum" name="forum">${cfg.forums.filter((f) => f.id !== "none").map((f) => html`<option value="${f.id}" ${f.id === defaultForum(cur) ? raw("selected") : ""}>${f.name}</option>`)}</select></div>
          <div><label class="f" for="d_style">${t("drafting.style")}</label><select id="d_style" name="style">${cfg.styles.map((s) => html`<option value="${s.id}" ${s.id === cfg.saved_style ? raw("selected") : ""}>${t(`style.${s.id}`, {}, s.label)}</option>`)}</select>
            <label class="small"><input type="checkbox" name="save_style" value="1"> ${t("drafting.remember_style")}</label></div>
          <div><label class="f" for="d_lang">${t("drafting.output_lang")}</label><select id="d_lang" name="lang"><option value="en">English</option><option value="hi" ${lang() === "hi" ? raw("selected") : ""}>हिंदी</option><option value="mr">मराठी</option></select></div>
        </div>
        <fieldset class="fs"><legend>${t("drafting.base")}</legend>
          <div class="form-grid">
            <div><label class="f" for="d_tpl">${t("drafting.template")}</label><select id="d_tpl" name="template_doc_id"><option value="">${t("drafting.auto_template")}</option>${lib.draft.map((x) => html`<option value="${x.id}" ${x.id === r.q.get("template") ? raw("selected") : ""}>${x.title}</option>`)}</select></div>
            <div class="span-2"><label class="f" for="d_base">${t("drafting.base_doc")}</label><select id="d_base" name="base_doc_id"><option value="">—</option></select></div>
            <div class="span-3"><label class="f" for="d_base_text">${t("drafting.paste")}</label><textarea id="d_base_text" name="base_text" rows="3" placeholder="${t("drafting.paste_ph")}"></textarea></div>
          </div></fieldset>
        <fieldset class="fs"><legend>${t("drafting.supporting")}</legend><div id="d_docs" class="small">${t("drafting.choose_case_docs")}</div></fieldset>
        <fieldset class="fs"><legend>${t("drafting.known")}</legend><div id="d_known" class="form-grid"></div></fieldset>
        <div><label class="f" for="d_instr">${t("drafting.instructions")}</label><textarea id="d_instr" name="instructions" rows="4" placeholder="${t("drafting.instructions_ph")}"></textarea></div>
        <div class="row"><button class="btn btn-saffron" type="submit" id="genBtn">✍ ${t("drafting.generate")}</button></div>
        <div id="genBox" aria-live="polite"></div>
      </div></form>
      <div class="stack"><div class="card"><div class="card-h"><h2>${t("drafting.forum_reqs")}</h2></div><div class="card-b" id="forumBox">${spinner()}</div></div>
        <div class="card"><div class="card-h"><h2>${t("drafting.type_law")}</h2></div><div class="card-b" id="typeBox">${spinner()}</div></div>
        <div class="card"><div class="card-h"><h2>${t("drafting.recent")}</h2></div><div class="card-b">${recentList(recent.drafts)}</div></div></div>
    </div>`);
  bindStudio({ cases, kind: "draft" });
}

const defaultForum = (c) => {
  if (!c) return "district";
  if (/supreme court/i.test(c.court || "")) return "sci";
  if (/bombay|high court/i.test(c.court || "")) return "bombay_hc";
  return { Criminal: "criminal_district", Family: "family", Consumer: "consumer_district", Revenue: "revenue_mh" }[c.case_type] || "district";
};
const recentList = (list) => list.length ? html`<ul class="list-plain">${list.slice(0, 8).map((d) => html`<li><a href="#/drafting?draft=${encodeURIComponent(d.id)}">${d.title}</a><br><span class="small muted">${d.kind === "notice" ? t("drafting.notice") : t("drafting.draft")} · v${d.version} · ${fmtDateTime(d.updated_at)}</span></li>`)}</ul>` : html`<div class="empty">${t("drafting.none")}</div>`;

function bindStudio({ kind }) {
  const form = $("#dForm");
  const loadCaseDocs = async () => {
    const cid = $("#d_case").value;
    if (!cid) { mount($("#d_docs"), html`${t("drafting.choose_case_docs")}`); mount($("#d_base"), html`<option value="">—</option>`); return; }
    const { documents } = await api(`/api/documents?case_id=${encodeURIComponent(cid)}`);
    const ready = documents.filter((d) => d.status === "ready");
    mount($("#d_docs"), ready.length ? html`${ready.map((d) => html`<label class="check"><input type="checkbox" name="doc" value="${d.id}"> ${d.title} <span class="muted">(${d.meta?.lang_name || ""})</span></label>`)}` : html`<span class="muted">${t("drafting.no_docs")}</span>`);
    if ($("#d_base")) mount($("#d_base"), html`<option value="">—</option>${ready.map((d) => html`<option value="${d.id}">${d.title}</option>`)}`);
  };
  const loadType = async () => {
    const p = await api(`/api/drafting/types/${encodeURIComponent($("#d_type").value)}`);
    mount($("#d_known"), html`${p.required.map(([k, lbl]) => html`<div class="span-3"><label class="f" for="k_${k}">${lbl}</label><input type="text" id="k_${k}" data-known="${k}" placeholder="${t("drafting.leave_blank")}"></div>`)}`);
    mount($("#typeBox"), p.provisions.length ? html`${p.provisions.map(provCard)}${(p.formalities || []).map((f) => html`<div class="notice" style="margin-top:6px">${f}</div>`)}` : html`<div class="empty">${t("drafting.no_type_law")}</div>`);
  };
  const loadForum = async () => {
    const fid = $("#d_forum")?.value || "none";
    const f = await api(`/api/drafting/forums/${encodeURIComponent(fid)}`);
    mount($("#forumBox"), forumPanel(f));
    $("#reqForm")?.addEventListener("submit", async (e) => {
      e.preventDefault();
      try { await api(`/api/drafting/forums/${encodeURIComponent(fid)}/requirements`, { method: "POST", body: formData(e.target) }); toast(t("common.saved")); loadForum(); }
      catch (err) { toast(err.message, true); }
    });
  };
  $("#d_case").onchange = loadCaseDocs; $("#d_type").onchange = loadType; $("#d_forum")?.addEventListener("change", loadForum);
  loadCaseDocs(); loadType(); loadForum();
  form.onsubmit = async (e) => {
    e.preventDefault();
    const b = formData(form);
    const docIds = $$("input[name=doc]:checked", form).map((x) => x.value);
    const known = Object.fromEntries($$("[data-known]", form).map((i) => [i.dataset.known, i.value.trim()]).filter(([, v]) => v));
    $$("[data-notice]", form).forEach((i) => { if (i.value.trim()) known[i.dataset.notice] = i.value.trim(); });
    const body = { doc_type: b.doc_type, forum: b.forum || "none", style: b.style || "formal", lang: b.lang, case_id: b.case_id || null,
      doc_ids: docIds, known, instructions: b.instructions || "", template_doc_id: b.template_doc_id || null,
      base_doc_id: b.base_doc_id || null, base_text: b.base_text || null, save_style: !!b.save_style };
    if (docIds.length || body.base_doc_id) { if (!(await aiConsent())) return; body.consent = true; }
    $("#genBtn").disabled = true;
    try {
      const job = await api("/api/drafts/generate", { method: "POST", body });
      const d = await pollJob(job, (j) => mount($("#genBox"), progressBar(j)));
      toast(kind === "notice" ? t("notice.ready") : t("drafting.ready"));
      location.hash = `#/drafting?draft=${encodeURIComponent(d.id)}`;
    } catch (err) { mount($("#genBox"), html`<div class="notice notice-red">${err.message}</div>`); }
    finally { $("#genBtn").disabled = false; }
  };
}

const provCard = (p) => p.found ? html`<div class="prov"><div><strong>${p.label}</strong> — ${p.act_title} <span class="chip chip-ok">${t("prov.verified")}</span></div>
  <div class="small muted">${p.heading.split(" — ").slice(1).join(" — ")}</div>
  <div class="row small"><a href="${p.pdf_url}" target="_blank" rel="noopener">${t("acts.open_pdf")} (p. ${p.page}) ↗</a>${p.official_url ? html` · <a href="${p.official_url}" target="_blank" rel="noopener noreferrer">India Code ↗</a>` : ""}${p.fetched_at ? html` · <span class="muted">${t("prov.fetched", { date: p.fetched_at.slice(0, 10) })}</span>` : ""}</div>
  <details><summary class="small">${t("prov.show_text")}</summary><pre class="passage">${p.text}</pre></details></div>`
  : html`<div class="prov"><strong>${p.ref}</strong> — ${p.act} <span class="chip chip-warn">${t("prov.not_found")}</span></div>`;

function forumPanel(f) {
  return html`<div class="small"><strong>${f.name}</strong></div>
    ${f.official.length ? html`<h3 class="mini">${t("forum.official")}</h3><ul class="small">${f.official.map((o) => html`<li><a href="${o.url}" target="_blank" rel="noopener noreferrer">${o.title} ↗</a> <span class="muted">(${t("lookup.checked", { date: o.verified_on })})</span></li>`)}</ul>` : ""}
    <h3 class="mini">${t("forum.requirements")}</h3>
    ${f.requirements.length ? f.requirements.map((r) => r.provision ? html`<div class="small" style="margin-bottom:6px">${r.text}: ${provCard(r.provision)}</div>`
      : html`<div class="prov small">${r.text} <span class="chip">${t("forum.user_supplied")}</span> <a href="${r.url}" target="_blank" rel="noopener noreferrer">${t("forum.source")} ↗</a> <span class="muted">${r.verified_on}</span></div>`) : html`<div class="small muted">${t("forum.none")}</div>`}
    ${f.confirm.map((c) => html`<div class="notice" style="margin-top:6px">⚠ ${c}</div>`)}
    ${f.id !== "none" ? html`<details style="margin-top:8px"><summary class="small">${t("forum.add")}</summary><form id="reqForm" class="stack small" style="margin-top:6px">
      <input type="text" name="text" placeholder="${t("forum.req_text")}" required aria-label="${t("forum.req_text")}">
      <input type="text" name="url" placeholder="https://… (${t("forum.official_url")})" required aria-label="${t("forum.official_url")}">
      <input type="date" name="verified_on" required aria-label="${t("forum.verified_on")}">
      <button class="btn btn-sm" type="submit">${t("common.save")}</button></form></details>` : ""}`;
}

// ── Legal notice ──
export async function viewNotice(main, r) {
  crumbs([[t("nav.drafting"), "#/drafting"], [t("qa.notice")]]);
  const [cfg, { cases }, recent] = await Promise.all([config(), api("/api/cases"), api("/api/drafts?kind=notice")]);
  const caseId = r.q.get("case") || "";
  const types = cfg.types.filter((x) => x.kind === "notice");
  const party = (p) => html`<fieldset class="fs"><legend>${t(`notice.${p}`)}</legend><div class="form-grid">
    <div><label class="f" for="n_${p}_name">${t("notice.name")}</label><input type="text" id="n_${p}_name" data-notice="${p}_name"></div>
    <div><label class="f" for="n_${p}_role">${t("notice.role")}</label><input type="text" id="n_${p}_role" data-notice="${p}_role" placeholder="${t(`notice.${p}_role_ph`)}"></div>
    <div class="span-3"><label class="f" for="n_${p}_addr">${t("notice.address")}</label><textarea id="n_${p}_addr" data-notice="${p}_address" rows="2"></textarea></div></div></fieldset>`;
  mount(main, html`
    <h1 class="page-title">${t("notice.title")}</h1>
    <p class="page-lead">${t("notice.lead")}</p>
    <div class="notice" style="margin-bottom:14px">${t("notice.caution")}</div>
    <div class="grid g-side">
      <form class="card" id="dForm" novalidate><div class="card-b stack">
        <div class="form-grid">
          <div class="span-2"><label class="f" for="d_type">${t("notice.type")}</label><select id="d_type" name="doc_type">${types.map((x) => html`<option value="${x.id}">${x.label}</option>`)}</select></div>
          <div><label class="f" for="d_lang">${t("drafting.output_lang")}</label><select id="d_lang" name="lang"><option value="en">English</option><option value="hi" ${lang() === "hi" ? raw("selected") : ""}>हिंदी</option><option value="mr">मराठी</option></select></div>
          <div class="span-2"><label class="f" for="d_case">${t("drafting.case")}</label><select id="d_case" name="case_id"><option value="">${t("drafting.no_case")}</option>${cases.map((c) => html`<option value="${c.id}" ${c.id === caseId ? raw("selected") : ""}>[${c.case_type}] ${c.title}</option>`)}</select></div>
          <div><label class="f" for="d_style">${t("drafting.style")}</label><select id="d_style" name="style">${cfg.styles.map((s) => html`<option value="${s.id}" ${s.id === cfg.saved_style ? raw("selected") : ""}>${t(`style.${s.id}`, {}, s.label)}</option>`)}</select></div>
        </div>
        ${party("sender")}${party("recipient")}
        <div><label class="f" for="n_facts">${t("notice.facts")}</label><textarea id="n_facts" data-notice="facts_and_dates" rows="4" placeholder="${t("notice.facts_ph")}"></textarea></div>
        <div class="form-grid">
          <div class="span-3"><label class="f" for="n_refs">${t("notice.refs")}</label><input type="text" id="n_refs" data-notice="contract_or_legal_references" placeholder="${t("notice.refs_ph")}"></div>
          <div class="span-3"><label class="f" for="n_purpose">${t("notice.purpose")}</label><input type="text" id="n_purpose" data-notice="purpose"></div>
          <div class="span-3"><label class="f" for="n_demand">${t("notice.demand")}</label><input type="text" id="n_demand" data-notice="demand"></div>
          <div class="span-3"><label class="f" for="n_deadline">${t("notice.deadline")}</label><input type="text" id="n_deadline" data-notice="deadline_supplied_by_user" placeholder="${t("notice.deadline_ph")}"><div class="hint">${t("notice.deadline_hint")}</div></div>
        </div>
        <fieldset class="fs"><legend>${t("drafting.supporting")}</legend><div id="d_docs" class="small">${t("drafting.choose_case_docs")}</div></fieldset>
        <div id="d_known" class="hidden"></div>
        <div><label class="f" for="d_instr">${t("drafting.instructions")}</label><textarea id="d_instr" name="instructions" rows="2"></textarea></div>
        <div class="row"><button class="btn btn-saffron" type="submit" id="genBtn">✉ ${t("notice.generate")}</button></div>
        <div id="genBox" aria-live="polite"></div></div></form>
      <div class="stack"><div class="card"><div class="card-h"><h2>${t("drafting.type_law")}</h2></div><div class="card-b" id="typeBox">${spinner()}</div></div>
        <div class="card hidden"><div class="card-b" id="forumBox"></div></div>
        <div class="card"><div class="card-h"><h2>${t("notice.recent")}</h2></div><div class="card-b">${recentList(recent.drafts)}</div></div></div>
    </div>`);
  bindStudio({ kind: "notice" });
}

// ── Editor (side-by-side) ──
async function viewEditor(main, id) {
  const d = await api(`/api/drafts/${encodeURIComponent(id)}`);
  crumbs([[t("nav.drafting"), "#/drafting"], [d.title]]);
  const facts = Object.fromEntries((d.facts || []).map((f) => [f.id, f]));
  const ck = d.inputs?.checks || {};
  mount(main, html`
    <div class="row"><h1 class="page-title">${d.title}</h1><span class="chip">v${d.version}</span><span class="chip chip-warn">${t("drafting.needs_review")}</span><span class="spacer"></span>
      <button class="btn" id="saveBtn">💾 ${t("common.save")}</button>
      <button class="btn btn-ghost" id="dlDocx">⬇ Word (.docx)</button><button class="btn btn-ghost" id="dlPdf">⬇ PDF</button>
      <button class="btn btn-ghost" id="regenBtn">↻ ${t("drafting.regenerate")}</button><button class="btn btn-danger" id="delBtn">${t("common.delete")}</button></div>
    <div class="row small" style="margin-bottom:10px">
      <span class="chip ${ck.placeholders?.length ? "chip-warn" : "chip-ok"}">${t("drafting.placeholders", { n: ck.placeholders?.length || 0 })}</span>
      ${ck.invalid_citations?.length ? html`<span class="chip chip-err">${t("drafting.bad_cites", { list: ck.invalid_citations.join(", ") })}</span>` : ""}
      ${ck.unknown_fact_tags?.length ? html`<span class="chip chip-err">${t("drafting.bad_tags", { list: ck.unknown_fact_tags.join(", ") })}</span>` : ""}
      <span class="legend"><span class="fact o-file">F</span> ${t("drafting.from_file")} <span class="fact o-user">F</span> ${t("drafting.from_user")} <span class="fact o-case">F</span> ${t("drafting.from_case")} <button class="cite" type="button" tabindex="-1">n</button> ${t("drafting.from_library")} <mark class="ph">[●]</mark> ${t("drafting.placeholder")}</span></div>
    <div class="editor">
      <div class="card"><div class="card-h"><h2>${t("drafting.edit")}</h2><span class="small muted">Markdown</span></div><textarea id="edText" class="ed-text" aria-label="${t("drafting.edit")}" spellcheck="true">${d.content}</textarea></div>
      <div class="card"><div class="card-h"><h2>${t("drafting.preview")}</h2></div><div class="card-b md doc-preview" id="edPrev"></div></div>
    </div>
    <div class="grid g-2" style="margin-top:16px">
      <div class="stack">
        ${d.missing?.length ? html`<form class="card" id="missForm"><div class="card-h"><h2>${t("drafting.missing")}</h2></div><div class="card-b stack">
          <p class="small muted">${t("drafting.missing_hint")}</p>
          ${d.missing.map((m, i) => html`<div><label class="f" for="m_${i}">${m.question} ${m.essential ? html`<span class="chip chip-warn">${t("drafting.essential")}</span>` : ""}</label><input type="text" id="m_${i}" data-key="${m.key || `q${i}`}"></div>`)}
          <div><button class="btn btn-saffron btn-sm" type="submit">↻ ${t("drafting.regen_answers")}</button></div><div id="regenBox"></div></div></form>` : ""}
        ${(d.inputs?.inconsistencies || []).length || (d.inputs?.unsupported || []).length ? html`<div class="card"><div class="card-h"><h2>${t("drafting.issues")}</h2></div><div class="card-b small">
          ${(d.inputs.inconsistencies || []).map((x) => html`<div class="notice notice-red" style="margin-bottom:6px">${t("drafting.inconsistency")}: ${x}</div>`)}
          ${(d.inputs.unsupported || []).map((x) => html`<div class="notice" style="margin-bottom:6px">${t("drafting.unsupported")}: ${x}</div>`)}</div></div>` : ""}
        <div class="card"><div class="card-h"><h2>${t("drafting.facts")}</h2><span class="small muted">${t("drafting.facts_n", { n: (d.facts || []).length })}</span></div><div class="table-wrap"><table class="gov compact"><thead><tr><th>#</th><th>${t("drafting.fact")}</th><th>${t("drafting.origin")}</th></tr></thead>
          <tbody>${(d.facts || []).map((f) => html`<tr id="fact-${f.id}"><td><span class="fact ${ORIGIN_CLS[f.origin] || ""}">${f.id}</span></td><td>${f.text}</td><td class="small">${t(`origin.${f.origin}`, {}, f.origin)}${f.ref ? html`<br><span class="muted">${f.ref}</span>` : ""}</td></tr>`)}</tbody></table>
          ${(d.facts || []).length ? "" : html`<div class="empty">${t("drafting.no_facts")}</div>`}</div></div>
      </div>
      <div class="stack">
        <div class="card"><div class="card-h"><h2>${t("drafting.sources")}</h2></div><div class="card-b">${(d.sources || []).length ? html`<ol class="small">${d.sources.map((s) => html`<li>${s.citation} — <a href="/api/documents/${encodeURIComponent(s.doc_id)}/file#page=${s.page || 1}" target="_blank" rel="noopener">${t("acts.open_pdf")} ↗</a></li>`)}</ol>` : html`<div class="empty">${t("drafting.no_sources")}</div>`}</div></div>
        <div class="card"><div class="card-h"><h2>${t("drafting.confirm")}</h2></div><div class="card-b small">
          ${(d.inputs?.forum_confirm || []).map((c) => html`<div class="notice" style="margin-bottom:6px">⚠ ${c}</div>`)}
          ${(d.inputs?.formalities || []).map((c) => html`<div class="notice" style="margin-bottom:6px">⚖ ${c}</div>`)}
          ${(d.inputs?.forum_official || []).map((o) => html`<div><a href="${o.url}" target="_blank" rel="noopener noreferrer">${o.title} ↗</a></div>`)}
          ${d.inputs?.template ? html`<div class="muted" style="margin-top:6px">${t("drafting.based_on", { title: d.inputs.template.title })}</div>` : ""}</div></div>
        <div class="card"><div class="card-h"><h2>${t("drafting.versions")}</h2></div><div class="card-b"><ul class="list-plain small">${(d.versions || []).map((v) => html`<li>v${v.version} · ${t(`drafting.note_${v.note}`, {}, v.note)} · ${fmtDateTime(v.created_at)} <button type="button" class="btn btn-ghost btn-sm" data-ver="${v.version}">${t("drafting.load")}</button></li>`)}</ul></div></div>
      </div>
    </div>`);
  const prev = () => {
    mount($("#edPrev"), markdown($("#edText").value));
    $$(".fact", $("#edPrev")).forEach((b) => { const f = facts[b.dataset.fact]; b.classList.add(ORIGIN_CLS[f?.origin] || "o-unknown"); b.title = f ? `${f.text} — ${t(`origin.${f.origin}`, {}, f.origin)} ${f.ref || ""}` : t("drafting.unknown_fact"); });
  };
  prev();
  let dirty = false, timer;
  $("#edText").addEventListener("input", () => { dirty = true; clearTimeout(timer); timer = setTimeout(prev, 250); });
  $("#edPrev").addEventListener("click", (e) => {
    const f = e.target.closest(".fact"); if (f) { document.getElementById(`fact-${f.dataset.fact}`)?.scrollIntoView({ behavior: "smooth", block: "center" }); return; }
    const c = e.target.closest("button.cite"); if (c) { const s = (d.sources || []).find((x) => String(x.n) === c.dataset.cite); if (s) window.open(`/api/documents/${encodeURIComponent(s.doc_id)}/file#page=${s.page || 1}`, "_blank", "noopener"); }
  });
  const save = async () => {
    const x = await api(`/api/drafts/${encodeURIComponent(id)}`, { method: "PUT", body: { content: $("#edText").value } });
    dirty = false; toast(t("drafting.saved_v", { v: x.version })); return x;
  };
  $("#saveBtn").onclick = async () => { try { await save(); viewEditor(main, id); } catch (e) { toast(e.message, true); } };
  const dl = async (fmt) => { try { if (dirty) await save(); download(`/api/drafts/${encodeURIComponent(id)}/export?format=${fmt}`); } catch (e) { toast(e.message, true); } };
  $("#dlDocx").onclick = () => dl("docx"); $("#dlPdf").onclick = () => dl("pdf");
  $("#delBtn").onclick = async () => { if (await confirmDialog(t("drafting.delete_title"), t("drafting.delete_body"), t("common.delete"))) { await api(`/api/drafts/${encodeURIComponent(id)}`, { method: "DELETE" }); location.hash = "#/drafting"; } };
  $$("[data-ver]").forEach((b) => { b.onclick = async () => { const v = await api(`/api/drafts/${encodeURIComponent(id)}/versions/${b.dataset.ver}`); $("#edText").value = v.content; dirty = true; prev(); toast(t("drafting.version_loaded", { v: v.version })); }; });
  const regenerate = async (extraKnown, box) => {
    const inp = d.inputs || {};
    const body = { doc_type: d.doc_type, forum: d.forum || "none", style: d.style, lang: d.lang, case_id: d.case_id, doc_ids: inp.doc_ids || [],
      known: { ...(inp.known || {}), ...extraKnown }, instructions: inp.instructions || "", template_doc_id: inp.template_doc_id || null,
      base_doc_id: inp.base_doc_id || null, draft_id: id, title: d.title };
    if (body.doc_ids.length || body.base_doc_id) { if (!(await aiConsent())) return; body.consent = true; }
    try { const job = await api("/api/drafts/generate", { method: "POST", body }); await pollJob(job, (j) => mount(box, progressBar(j))); viewEditor(main, id); }
    catch (e) { mount(box, html`<div class="notice notice-red">${e.message}</div>`); }
  };
  $("#regenBtn").onclick = () => { const box = document.createElement("div"); $("#edPrev").before(box); regenerate({}, box); };
  $("#missForm")?.addEventListener("submit", (e) => {
    e.preventDefault();
    const ans = Object.fromEntries($$("[data-key]", e.target).map((i) => [i.dataset.key, i.value.trim()]).filter(([, v]) => v));
    if (!Object.keys(ans).length) { toast(t("drafting.answer_one"), true); return; }
    regenerate(ans, $("#regenBox"));
  });
  window.onbeforeunload = () => (dirty ? true : undefined);
  state.leaveGuard = () => !dirty || confirm(t("drafting.unsaved"));
}
