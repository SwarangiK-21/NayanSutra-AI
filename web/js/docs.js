/* Module 5 — multilingual documents: viewer (original / extracted text / English translation / summaries) and batch upload. */
import { $, $$, aiConsent, api, crumbs, fmtDate, fmtDateTime, html, markdown, mount, pollJob, progressBar, raw, spinner, state, toast } from "./core.js";
import { lang, t } from "./i18n.js";

const ACCEPT = ".pdf,.docx,.doc,.txt,.md,.png,.jpg,.jpeg,.tif,.tiff,.webp";
export function openDocument(id) { location.hash = `#/doc/${encodeURIComponent(id)}`; }

export async function viewDocument(main, r) {
  const id = r.arg;
  const data = await api(`/api/documents/${encodeURIComponent(id)}/intel`);
  const d = data.doc, L = data.language || {}, der = data.derivatives;
  const caseRow = d.case_id ? await api(`/api/cases/${encodeURIComponent(d.case_id)}`).catch(() => null) : null;
  crumbs([[t("nav.cases"), "#/cases"], ...(caseRow ? [[caseRow.title, `#/cases/${encodeURIComponent(caseRow.id)}`]] : []), [d.title]]);
  const tab = r.q.get("tab") || (der.facts ? "summary" : "text");
  const isEnglish = L.lang === "en";
  const ocr = data.ocr;
  mount(main, html`
    <div class="row"><h1 class="page-title">${d.title}</h1><span class="spacer"></span>
      <a class="btn btn-ghost" href="/api/documents/${encodeURIComponent(id)}/file" target="_blank" rel="noopener">${t("doc.original")} ↗</a>
      ${d.case_id ? html`<a class="btn btn-ghost" href="#/research?case=${encodeURIComponent(d.case_id)}&doc=${encodeURIComponent(id)}&task=summary">💬 ${t("doc.ask")}</a>` : ""}</div>
    <div class="row" style="margin-bottom:12px">
      <span class="chip">${t("doc.language")}: ${L.lang_name || "—"}${L.script ? ` (${L.script})` : ""}</span>
      ${L.lang_confidence === "low" ? html`<span class="chip chip-warn" title="${t("doc.lang_low_hint")}">${t("doc.lang_low")}</span>` : ""}
      <span class="chip">${t("case.pages", { n: d.pages || "?" })}</span>
      ${ocr ? html`<span class="chip ${ocr.low_conf_ratio > 0.1 ? "chip-warn" : "chip-ok"}" title="${t("doc.ocr_hint")}">OCR · ${t("doc.ocr_unclear", { pct: Math.round(100 * ocr.low_conf_ratio) })}</span>` : ""}
      ${data.ocr_needed_pages?.length ? html`<span class="chip chip-err">${t("doc.ocr_needed", { pages: data.ocr_needed_pages.join(", ") })}</span>` : ""}
      ${d.status !== "ready" ? html`<span class="chip chip-wait">${d.status}</span>` : ""}
    </div>
    ${d.status === "failed" ? html`<div class="notice notice-red">${d.error}</div>` : ""}
    <div class="card"><div class="card-h"><h2>${t("doc.ai_tools")}</h2><span class="small muted">${t("doc.ai_note")}</span></div><div class="card-b">
      <div class="row">
        ${!isEnglish ? html`<button class="btn" id="trBtn" ${d.status !== "ready" ? raw("disabled") : ""}>${der.translation_en ? t("doc.retranslate") : t("doc.translate")}</button>` : ""}
        <button class="btn btn-saffron" data-sum="en" ${d.status !== "ready" ? raw("disabled") : ""}>${t("doc.summary_en")}</button>
        <button class="btn btn-ghost" data-sum="hi" lang="hi" ${d.status !== "ready" ? raw("disabled") : ""}>${t("doc.summary_hi")}</button>
        <button class="btn btn-ghost" data-sum="mr" lang="mr" ${d.status !== "ready" ? raw("disabled") : ""}>${t("doc.summary_mr")}</button></div>
      <div id="jobBox" style="margin-top:10px" aria-live="polite"></div></div></div>
    <div class="tabs" role="tablist" style="margin-top:16px">
      ${[["summary", t("doc.tab_summary")], ["text", t("doc.tab_text")], ...(!isEnglish ? [["translation", t("doc.tab_translation")]] : []), ["original", t("doc.tab_original")]].map(([k, lbl]) =>
        html`<a role="tab" class="tab ${k === tab ? "active" : ""}" aria-selected="${String(k === tab)}" href="#/doc/${encodeURIComponent(id)}?tab=${k}">${lbl}</a>`)}</div>
    <div id="docTab"></div>`);

  const body = $("#docTab");
  if (tab === "summary") renderSummary(body, der, id, data.pages.length);
  else if (tab === "text") renderPages(body, data.pages, L, r.q.get("page"));
  else if (tab === "translation") renderPages(body, der.translation_en?.content || [], { lang_name: "English" }, r.q.get("page"), der.translation_en);
  else mount(body, html`<div class="card"><div class="card-b">${/\.(pdf)$/i.test(d.filename)
    ? html`<iframe class="doc-frame" src="/api/documents/${encodeURIComponent(id)}/file" title="${d.title}"></iframe>`
    : /\.(png|jpe?g|webp)$/i.test(d.filename) ? html`<img class="doc-img" src="/api/documents/${encodeURIComponent(id)}/file" alt="${d.title}">`
      : html`<a class="btn" href="/api/documents/${encodeURIComponent(id)}/file" target="_blank" rel="noopener">${t("doc.original")} ↗</a>`}</div></div>`);

  const run = async (path, payload) => {
    if (!(await aiConsent())) return;
    const box = $("#jobBox");
    try {
      const job = await api(path, { method: "POST", body: { consent: true, ...payload } });
      await pollJob(job, (j) => mount(box, progressBar(j)));
      toast(t("common.done")); location.hash = `#/doc/${encodeURIComponent(id)}?tab=${path.endsWith("translate") ? "translation" : "summary"}&r=${Date.now()}`;
    } catch (e) { mount(box, html`<div class="notice notice-red">${e.message}</div>`); }
  };
  $("#trBtn")?.addEventListener("click", () => run(`/api/documents/${encodeURIComponent(id)}/translate`, {}));
  $$("[data-sum]").forEach((b) => { b.onclick = () => run(`/api/documents/${encodeURIComponent(id)}/summarize`, { lang: b.dataset.sum }); });
}

function renderPages(el, pages, L, focusPage, derived) {
  if (!pages.length) { mount(el, html`<div class="card"><div class="card-b empty">${derived === undefined ? t("doc.no_text") : t("doc.no_translation")}</div></div>`); return; }
  const marked = (s) => html`${String(s).split(/(\[\?[^\]]+\]|\[unclear:[^\]]+\]|\[uncertain translation\])/).map((part) =>
    /^\[(\?|unclear:|uncertain)/.test(part) ? html`<mark class="unclear" title="${t("doc.unclear_hint")}">${part}</mark>` : part)}`;
  mount(el, html`<div class="card">${derived ? html`<div class="card-b small muted">${t("doc.translation_meta", { when: fmtDateTime(derived.created_at), model: derived.meta?.model || "", n: derived.meta?.uncertain_marks ?? 0 })}</div>` : ""}
    <div class="card-b"><div class="small muted" style="margin-bottom:8px">${t("doc.pages_note")}</div>
    ${pages.map((p, i) => html`<section class="page-block" id="pg${i + 1}" lang="${L.lang && L.lang !== "und" ? L.lang : ""}"><div class="page-no">p. ${i + 1}</div><pre class="passage">${marked(p)}</pre></section>`)}</div></div>`);
  if (focusPage) document.getElementById(`pg${focusPage}`)?.scrollIntoView();
}

function renderSummary(el, der, id, nPages) {
  const facts = der.facts?.content;
  if (!facts) { mount(el, html`<div class="card"><div class="card-b empty">${t("doc.no_summary")}</div></div>`); return; }
  const pg = (p) => Number.isInteger(p) && p >= 1 && p <= nPages ? html` <a class="pg" href="#/doc/${encodeURIComponent(id)}?tab=text&page=${p}">p. ${p}</a>` : "";
  const ver = (x) => x.verified === undefined ? "" : x.verified ? html` <span class="chip chip-ok" title="${t("doc.verified_hint")}">✓</span>` : html` <span class="chip chip-warn" title="${x.note || ""}">${t("doc.check")}</span>`;
  const list = (title, items, render) => items?.length ? html`<h3>${title}</h3><ul>${items.map((x) => html`<li>${render(x)}</li>`)}</ul>` : "";
  const local = der[`summary_${lang()}`];
  const basis = der.facts.meta?.basis;
  mount(el, html`<div class="grid g-side">
    <div class="card"><div class="card-h"><h2>${t("doc.summary_title")}</h2><span class="small muted">${t(`doc.basis_${basis}`, {}, basis)} · ${fmtDate(der.facts.created_at)}</span></div><div class="card-b md">
      <p><strong>${t("doc.type")}:</strong> ${facts.document_type || "—"}</p><p>${facts.summary || ""}</p>
      ${facts.parties?.length ? html`<h3>${t("doc.parties")}</h3><table><thead><tr><th>${t("doc.name")}</th><th>${t("doc.role")}</th><th>${t("doc.page")}</th></tr></thead><tbody>${facts.parties.map((x) => html`<tr><td>${x.name}${ver(x)}</td><td>${x.role || ""}</td><td>${pg(x.page)}</td></tr>`)}</tbody></table>` : ""}
      ${list(t("doc.dates"), facts.dates, (x) => html`<strong>${x.date}</strong> — ${x.event}${pg(x.page)}`)}
      ${list(t("doc.case_numbers"), facts.case_numbers, (x) => html`<code>${x.value}</code>${ver(x)}${pg(x.page)}`)}
      ${list(t("doc.key_facts"), facts.key_facts, (x) => html`${x.text}${pg(x.page)}`)}
      ${list(t("doc.claims"), facts.claims, (x) => html`${x.by ? html`<em>${x.by}:</em> ` : ""}${x.text}${pg(x.page)}`)}
      ${list(t("doc.relief"), facts.relief_requested, (x) => html`${x.text}${pg(x.page)}`)}
      ${list(t("doc.provisions"), facts.legal_provisions, (x) => html`${x.reference}${x.act ? ` — ${x.act}` : ""}${ver(x)}${pg(x.page)} <a class="small" href="#/acts?q=${encodeURIComponent(`${x.reference} ${x.act || ""}`)}">${t("doc.lookup")}</a>`)}
      ${list(t("doc.deadlines"), facts.deadlines, (x) => html`${x.text}${pg(x.page)}`)}
      ${list(t("doc.actions"), facts.action_items, (x) => html`${x.text}${x.basis ? html` <span class="small muted">(${x.basis})</span>` : ""}`)}
      ${facts.uncertain?.length ? html`<div class="notice" style="margin-top:8px"><strong>${t("doc.uncertain")}</strong><ul>${facts.uncertain.map((u) => html`<li>${u}</li>`)}</ul></div>` : ""}
      <p class="small muted">${t("doc.summary_disclaimer")}</p></div></div>
    <div class="stack">${["hi", "mr"].map((lg) => der[`summary_${lg}`] ? html`<div class="card"><div class="card-h"><h2 lang="${lg}">${t(`doc.summary_${lg}`)}</h2><span class="small muted">${fmtDate(der[`summary_${lg}`].created_at)}</span></div>
      <div class="card-b md" lang="${lg}">${markdown(der[`summary_${lg}`].content)}</div></div>` : "")}
      ${!local && lang() !== "en" ? html`<div class="notice">${t("doc.summary_lang_hint")}</div>` : ""}</div></div>`);
}

// ── Upload & summarise (batch) ──
export async function viewUpload(main, r) {
  crumbs([[t("nav.cases"), "#/cases"], [t("qa.upload_summarise")]]);
  const { cases } = await api("/api/cases");
  const pre = r.q.get("case") || state.caseId || "";
  mount(main, html`
    <h1 class="page-title">${t("qa.upload_summarise")}</h1>
    <p class="page-lead">${t("upload.lead")}</p>
    <div class="card"><div class="card-b stack">
      <div><label class="f" for="upCase">${t("upload.case")}</label>
        <select id="upCase"><option value="__new">${t("upload.new_case")}</option>${cases.map((c) => html`<option value="${c.id}" ${c.id === pre ? raw("selected") : ""}>[${c.case_type}] ${c.title}</option>`)}</select></div>
      <label class="dropzone" id="upDrop"><input type="file" id="upFiles" multiple accept="${ACCEPT}">
        <strong>${t("case.drop")}</strong><br><span class="small muted">${t("upload.formats")}</span></label>
      <div class="small muted">${t("upload.privacy")}</div></div></div>
    <div class="card" style="margin-top:16px"><div class="card-h"><h2>${t("upload.progress")}</h2></div><div class="card-b" id="upList"><div class="empty">${t("upload.none")}</div></div></div>`);
  const items = [];
  const render = () => mount($("#upList"), items.length ? html`${items.map((x) => html`<div class="doc-item"><span class="doc-ico">${x.ext}</span><div class="doc-main">
    <div class="doc-title">${x.name}</div>
    <div class="small">${x.state === "uploading" ? html`<span class="spinner"></span> ${t("upload.uploading")}` : x.state === "processing" ? html`<span class="spinner"></span> ${t("case.indexing")}`
      : x.state === "ready" ? html`<span class="chip chip-ok">${t("upload.ready")}</span> ${x.lang ? html`<span class="chip">${x.lang}</span>` : ""} <a class="btn btn-saffron btn-sm" href="#/doc/${encodeURIComponent(x.id)}">${t("upload.open_summarise")}</a>`
      : html`<span class="chip chip-err">${t("common.failed")}</span> ${x.error || ""}`}</div></div></div>`)}` : html`<div class="empty">${t("upload.none")}</div>`);
  const poll = async () => {
    const pending = items.filter((x) => x.state === "processing");
    for (const x of pending) {
      try { const d = await api(`/api/documents/${encodeURIComponent(x.id)}`); if (d.status !== "processing") { x.state = d.status === "ready" ? "ready" : "failed"; x.error = d.error; x.lang = d.meta?.lang_name; } } catch { /* retry */ }
    }
    render();
    clearInterval(state.pollTimer);
    if (items.some((x) => x.state === "processing")) state.pollTimer = setInterval(poll, 2000);
  };
  const upload = async (files) => {
    let caseId = $("#upCase").value;
    if (caseId === "__new") {
      try {
        const c = await api("/api/cases", { method: "POST", body: { title: files[0].name.replace(/\.[^.]+$/, "").slice(0, 120), is_draft: true } });
        caseId = c.id;
        const o = document.createElement("option"); o.value = c.id; o.textContent = c.title; $("#upCase").appendChild(o); $("#upCase").value = c.id;
      } catch (e) { toast(e.message, true); return; }
    }
    for (const f of files) {
      const x = { name: f.name, ext: (f.name.split(".").pop() || "").toUpperCase(), state: "uploading" }; items.unshift(x); render();
      const fd = new FormData(); fd.append("file", f); fd.append("doc_type", "auto");
      try { const d = await api(`/api/cases/${encodeURIComponent(caseId)}/documents`, { method: "POST", form: fd }); x.id = d.id; x.state = d.status === "ready" ? "ready" : d.status === "failed" ? "failed" : "processing"; x.error = d.error; }
      catch (e) { x.state = "failed"; x.error = e.message; }
      render();
    }
    poll();
  };
  const drop = $("#upDrop");
  $("#upFiles").onchange = (e) => { if (e.target.files.length) upload([...e.target.files]); e.target.value = ""; };
  ["dragenter", "dragover"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.add("drag"); }));
  ["dragleave", "drop"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.remove("drag"); }));
  drop.addEventListener("drop", (e) => { if (e.dataTransfer.files.length) upload([...e.dataTransfer.files]); });
}
