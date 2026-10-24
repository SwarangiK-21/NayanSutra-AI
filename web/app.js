/* NyaySutra AI — frontend entry: router, chrome, and the core views (dashboard, cases, research, library).
   Feature views live in /static/js/*.js. No external libraries (CSP: script-src 'self'). */
import { $, $$, api, bindRowLinks, COLL_LABEL, crumbs, daysTo, fieldError, fmtDate, formData, hideDrawer, html,
  initSession, markdown, mount, num, raw, reference, setUnauthenticatedHandler, showDrawer, spinner, state, store,
  toast, TYPE_BADGE } from "./js/core.js";
import { applyStatic, initialLang, lang, LANGS, setLang, t } from "./js/i18n.js";
import { viewCaseSearch } from "./js/casesearch.js";
import { openDocument, viewDocument, viewUpload } from "./js/docs.js";
import { viewDrafting, viewNotice } from "./js/drafting.js";
import { provisionsPanel } from "./js/provisions.js";
import { refreshBell, viewUpdates } from "./js/updates.js";
import { viewAwareness } from "./js/awareness.js";
import { viewSearch } from "./js/search.js";
import { voiceControls, speakButton, bindSpeak } from "./js/voice.js";
import { applyNavPermissions, can, membersCard, renderEnroll, renderForgot, renderLogin, renderMfa, renderReset,
  renderSignup, viewAccount, viewAdmin } from "./js/auth.js";

// ── Status chips ──
async function refreshStatus() {
  try {
    const s = await api("/api/status"); state.status = s;
    const llm = s.llm_reachable === false ? html`<span class="chip chip-err" title="${t("status.llm_offline_hint")}">${t("status.llm_offline")}</span>`
      : html`<span class="chip chip-ok" title="${s.llm_host}">● ${s.llm_model}</span>`;
    const idx = html`<span class="chip ${s.indexed_chunks ? "chip-ok" : "chip-warn"}">${t("status.index", { n: num(s.indexed_chunks) })}</span>`;
    const models = s.models_ready ? "" : html`<span class="chip chip-wait">${t("status.loading_models")}</span>`;
    const proc = s.processing ? html`<span class="chip chip-wait">${t("status.indexing", { n: s.processing })}</span>` : "";
    mount($("#mastStatus"), html`${llm}${idx}${models}${proc}`);
    if (!s.models_ready) setTimeout(refreshStatus, 3000);  // clear the chip promptly once warm-up ends
  } catch { mount($("#mastStatus"), html`<span class="chip chip-err">${t("status.unreachable")}</span>`); }
}

// ── Router ──
const routes = { dashboard: viewDashboard, cases: viewCases, research: viewResearch, acts: viewActs,
  drafts: viewDrafts, limitation: viewLimitation, about: viewAbout, lookup: viewCaseSearch, doc: viewDocument,
  upload: viewUpload, drafting: viewDrafting, notice: viewNotice, updates: viewUpdates, awareness: viewAwareness,
  search: viewSearch, tools: viewTools, account: viewAccount, admin: viewAdmin,
  login: (m, r) => renderLogin(route, r.q), signup: (m, r) => renderSignup(route, r.q), forgot: () => renderForgot(),
  "reset-password": () => renderReset() };
// Pages a signed-out visitor may open (public legal information); everything else asks them to sign in.
const PUBLIC = new Set(["acts", "awareness", "limitation", "login", "signup", "forgot", "reset-password", "about"]);
function parseHash() {
  const h = location.hash.replace(/^#\/?/, "") || "dashboard";
  const [path, qs] = h.split("?");
  const parts = path.split("/").map(decodeURIComponent);
  return { name: parts[0], arg: parts[1], q: new URLSearchParams(qs || "") };
}
async function route() {
  clearInterval(state.pollTimer);
  window.onbeforeunload = null;  // the draft editor sets an unsaved-changes guard while it is open
  const r = parseHash();
  const fn = r.name === "cases" && r.arg ? viewCase : routes[r.name] || viewDashboard;
  const s = state.session || {};
  if (s.state === "mfa_pending") return renderMfa(route);
  if (s.state === "mfa_enroll") return renderEnroll(true, route);
  if (!s.authenticated && !PUBLIC.has(r.name)) return renderLogin(route, r.q);
  if (s.authenticated && s.role === "guest" && !PUBLIC.has(r.name) && r.name !== "account") {
    // Signed in, but no administrator has given this account a role yet.
    crumbs([[t("auth.no_access_title")]]);
    return mount($("#main"), html`<div class="card" style="max-width:560px;margin:32px auto"><div class="card-h"><h2>${t("auth.no_access_title")}</h2></div>
      <div class="card-b stack"><p>${t("auth.no_access_body", { email: s.user?.email || "" })}</p>
      <div class="small muted">${t("auth.public")} <a href="#/acts">${t("nav.acts")}</a> · <a href="#/awareness">${t("nav.awareness")}</a> · <a href="#/limitation">${t("nav.limitation")}</a></div></div></div>`);
  }
  const navName = { doc: "cases", upload: "cases", notice: "drafting", drafts: "tools", limitation: "tools", about: "tools" }[r.name] || r.name;
  $$("[data-nav]").forEach((a) => { const on = a.dataset.nav === navName; a.classList.toggle("active", on); if (on) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current"); });
  $("#navList").classList.remove("open");
  const main = $("#main");
  try { await fn(main, r); } catch (e) { if (!e.handled) mount(main, html`<div class="notice notice-red">${e.message}</div>`); }
  // A new page starts at the top; staying on the same page (e.g. switching chats) keeps the scroll.
  const page = `${r.name}/${r.arg || ""}`;
  if (page !== state.lastPage) { state.lastPage = page; window.scrollTo(0, 0); }
  if (r.name !== "research") main.focus({ preventScroll: true });
}

// ── Signed out / half-finished sign-in (session expired, second factor pending) ──
setUnauthenticatedHandler((s) => {
  applyNavPermissions();
  if (s?.state === "mfa_pending") return renderMfa(route);
  if (s?.state === "mfa_enroll") return renderEnroll(true, route);
  return renderLogin(route);
});

// ── Dashboard ──
async function viewDashboard(main) {
  crumbs([[t("nav.dashboard")]]);
  const [d, notes] = await Promise.all([api("/api/dashboard"), api("/api/notifications?limit=5")]);
  const lib = d.library || {};
  const actions = [
    ["🔎", t("qa.search_case"), "#/lookup"], ["＋", t("qa.new_case"), "#/cases?new=1"],
    ["📄", t("qa.upload_summarise"), "#/upload"], ["💬", t("qa.research_chat"), "#/research"],
    ["✍", t("qa.draft"), "#/drafting"], ["✉", t("qa.notice"), "#/notice"],
    ["📘", t("qa.search_acts"), "#/acts"], ["🛣", t("qa.awareness"), "#/awareness"],
    ["🔔", t("qa.updates"), "#/updates"],
  ];
  mount(main, html`
    <h1 class="page-title">${t("nav.dashboard")}</h1>
    <p class="page-lead">${t("dash.lead")}</p>
    <div class="banner">
      <div><h2>${t("dash.banner_title")}</h2><p>${t("dash.banner_text")}</p></div>
      <div class="row"><a class="btn btn-saffron" href="#/research">${t("dash.start_research")}</a><a class="btn btn-ghost" href="#/cases?new=1">${t("qa.new_case")}</a></div>
    </div>
    <section aria-labelledby="qaTitle" style="margin-top:16px">
      <h2 id="qaTitle" class="section-title">${t("dash.quick_actions")}</h2>
      <div class="action-grid">${actions.map(([ico, label, href]) => html`<a class="card action" href="${href}"><span class="action-ico" aria-hidden="true">${ico}</span><span>${label}</span></a>`)}</div>
    </section>
    <div class="grid g-4 tiles" style="margin-top:16px">
      ${tile("⚖", d.total_cases, t("dash.case_files"), "#/cases")}
      ${tile("📄", lib.case || 0, t("dash.case_documents"), "#/cases")}
      ${tile("📘", lib.bare_act || 0, t("dash.acts_indexed"), "#/acts")}
      ${tile("✍", lib.draft || 0, t("dash.templates"), "#/drafts")}
    </div>
    <div class="grid g-side" style="margin-top:16px">
      <div class="card"><div class="card-h"><h2>${t("dash.upcoming")}</h2><a class="small" href="#/cases">${t("dash.all_cases")} →</a></div>
        <div class="table-wrap">${d.upcoming.length ? html`<table class="gov"><thead><tr><th>${t("col.date")}</th><th>${t("col.matter")}</th><th>${t("col.type")}</th><th>${t("col.court")}</th></tr></thead><tbody>
          ${d.upcoming.map((c) => html`<tr class="clickable" data-href="#/cases/${encodeURIComponent(c.id)}"><td><strong>${fmtDate(c.next_hearing_date)}</strong><br><span class="small muted">${t("dash.in_days", { n: daysTo(c.next_hearing_date) })}</span></td>
            <td>${c.title}</td><td><span class="badge ${TYPE_BADGE[c.case_type] || ""}">${t(`cat.${c.case_type}`, {}, c.case_type)}</span></td><td class="small">${c.court}</td></tr>`)}
          </tbody></table>` : html`<div class="empty">${t("dash.no_hearings")}</div>`}</div></div>
      <div class="stack">
        <div class="card"><div class="card-h"><h2>${t("dash.recent_updates")}</h2><a class="small" href="#/updates">${t("common.view_all")} →</a></div><div class="card-b">
          ${notes.items.length ? html`<ul class="list-plain">${notes.items.map((n) => html`<li>${n.link ? html`<a href="${n.link}">${n.title}</a>` : n.title}<br><span class="small muted">${fmtDate(n.created_at)}${n.read_at ? "" : html` · <span class="chip chip-wait">${t("updates.new")}</span>`}</span></li>`)}</ul>`
            : html`<div class="empty">${t("dash.no_updates")}</div>`}
        </div></div>
        <div class="card"><div class="card-h"><h2>${t("dash.recent_research")}</h2></div><div class="card-b">
          ${d.recent_chats.length ? html`<ul class="list-plain">${d.recent_chats.map((c) => html`<li><a href="#/research?${c.case_id ? `case=${encodeURIComponent(c.case_id)}&` : ""}chat=${encodeURIComponent(c.id)}">${c.title}</a><br>
            <span class="small muted">${c.case_title || t("research.general")} · ${fmtDate(c.updated_at)}</span></li>`)}</ul>` : html`<div class="empty">${t("dash.no_research")}</div>`}
        </div></div>
      </div>
    </div>`);
  bindRowLinks(main);
}
const tile = (ico, n, lbl, href) => html`<a class="card tile" href="${href}" style="text-decoration:none"><span class="tile-ico" aria-hidden="true">${ico}</span>
  <span><span class="tile-num">${num(n)}</span><br><span class="tile-lbl">${lbl}</span></span></a>`;

// ── Cases list ──
async function viewCases(main, r) {
  crumbs([[t("nav.cases")]]);
  const [{ cases, types, statuses }, ref] = await Promise.all([api("/api/cases"), reference()]);
  mount(main, html`
    <div class="row"><h1 class="page-title">${t("nav.cases")}</h1><span class="spacer"></span>${can("case.create") ? html`<button class="btn btn-saffron" id="newCaseBtn">＋ ${t("qa.new_case")}</button>` : ""}</div>
    <p class="page-lead">${t("cases.lead")}</p>
    <div class="card ${r.q.get("new") && can("case.create") ? "" : "hidden"}" id="newCaseCard" style="margin-bottom:16px">
      <div class="card-h"><h2>${t("cases.register")}</h2><span class="small muted">${t("form.required_hint")}</span></div>
      <form class="card-b" id="caseForm" novalidate>${caseFormFields({}, statuses, ref, true)}
        <div class="row" style="margin-top:12px"><button class="btn" type="submit" data-final="1">${t("cases.create")}</button>
          <button class="btn btn-ghost" type="submit" data-final="0">${t("cases.save_draft")}</button>
          <button class="btn btn-ghost" type="button" id="cancelNew">${t("common.cancel")}</button></div></form>
    </div>
    <div class="card"><div class="card-h"><h2>${t("cases.all", { n: cases.length })}</h2>
      <div class="row"><select id="fType" aria-label="${t("cases.filter_type")}"><option value="">${t("cases.all_types")}</option>${types.map((x) => html`<option value="${x}">${t(`cat.${x}`, {}, x)}</option>`)}</select>
      <input type="search" id="fSearch" placeholder="${t("cases.search_ph")}" aria-label="${t("cases.search_ph")}" style="min-width:240px"></div></div>
      <div class="table-wrap"><table class="gov"><thead><tr><th>${t("col.matter")}</th><th>${t("col.type")}</th><th>${t("col.case_no")}</th><th>${t("col.court")}</th><th>${t("col.client")}</th><th>${t("col.next_hearing")}</th><th>${t("col.docs")}</th><th>${t("col.status")}</th></tr></thead>
      <tbody id="caseRows"></tbody></table></div></div>`);
  const rows = () => {
    const ty = $("#fType").value, q = $("#fSearch").value.toLowerCase();
    const list = cases.filter((c) => (!ty || c.case_type === ty) && (!q || [c.title, c.case_number, c.cnr_number, c.fir_number, c.client_name, c.court, c.opposite_party].join(" ").toLowerCase().includes(q)));
    mount($("#caseRows"), list.length ? html`${list.map((c) => html`<tr class="clickable" data-href="#/cases/${encodeURIComponent(c.id)}">
      <td><strong>${c.title}</strong> ${c.is_draft ? html`<span class="chip chip-warn">${t("cases.draft_badge")}</span>` : ""}<br><span class="small muted">v. ${c.opposite_party || "—"}</span></td>
      <td><span class="badge ${TYPE_BADGE[c.case_type] || ""}">${t(`cat.${c.case_type}`, {}, c.case_type)}</span></td><td class="small">${c.case_number || c.cnr_number || "—"}</td>
      <td class="small">${c.court || "—"}</td><td class="small">${c.client_name || "—"}</td><td class="small">${fmtDate(c.next_hearing_date)}</td>
      <td>${c.n_docs ?? "—"}</td><td class="small">${c.status}${c.access ? "" : html` <span class="chip">${t("members.not_on_case")}</span>`}</td></tr>`)}` : html`<tr><td colspan="8"><div class="empty">${t("cases.none")}</div></td></tr>`);
    bindRowLinks($("#caseRows"));
  };
  rows();
  $("#fType").onchange = rows; $("#fSearch").oninput = rows;
  if ($("#newCaseBtn")) $("#newCaseBtn").onclick = () => { $("#newCaseCard").classList.remove("hidden"); $("#f_client_name").focus(); };
  $("#cancelNew").onclick = () => $("#newCaseCard").classList.add("hidden");
  const form = $("#caseForm");
  bindCaseForm(form, ref, {});
  form.onsubmit = async (e) => {
    e.preventDefault();
    const final = e.submitter?.dataset.final !== "0";
    const body = { ...caseBody(form), is_draft: !final };
    const files = [...$("#f_files").files];
    try {
      const c = await api("/api/cases", { method: "POST", body });
      for (const f of files) {
        const fd = new FormData(); fd.append("file", f); fd.append("doc_type", "auto");
        try { await api(`/api/cases/${encodeURIComponent(c.id)}/documents`, { method: "POST", form: fd }); }
        catch (err) { toast(`${f.name}: ${err.message}`, true); }
      }
      toast(final ? t("cases.created") : t("cases.draft_saved"));
      location.hash = `#/cases/${encodeURIComponent(c.id)}`;
    } catch (err) { fieldError(form, err); }
  };
}

/** Case form. Party roles, case types, districts and the police-station field depend on other answers. */
function caseFormFields(c, statuses, ref, isNew) {
  const cat = c.case_type || "Civil";
  const year = new Date().getFullYear();
  const txt = (id, label, opts = {}) => html`<div class="${opts.cls || ""}"><label class="f" for="f_${id}">${label}</label>
    <input type="text" id="f_${id}" name="${id}" value="${c[id] ?? ""}" ${opts.list ? raw(`list="${opts.list}"`) : ""} ${opts.ph ? raw(`placeholder="${opts.ph}"`) : ""} autocomplete="off">${opts.hint ? html`<div class="hint">${opts.hint}</div>` : ""}</div>`;
  return html`<div class="form-grid">
    <fieldset class="span-3 fs"><legend>${t("form.parties")}</legend><div class="form-grid">
      <div><label class="f" for="f_case_type">${t("form.category")} *</label><select id="f_case_type" name="case_type">${ref.categories.map((x) => html`<option value="${x}" ${x === cat ? raw("selected") : ""}>${t(`cat.${x}`, {}, x)}</option>`)}</select></div>
      ${txt("client_name", t("form.client_name") + " *", { ph: t("form.name_ph") })}
      <div><label class="f" for="f_client_role">${t("form.client_role")}</label><select id="f_client_role" name="client_role" data-value="${c.client_role || ""}"></select></div>
      ${txt("opposite_party", t("form.opposite_name"), { ph: t("form.name_ph") })}
      <div><label class="f" for="f_opposite_role">${t("form.opposite_role")}</label><select id="f_opposite_role" name="opposite_role" data-value="${c.opposite_role || ""}"></select></div>
      ${txt("title", t("form.title"), { hint: t("form.title_hint") })}
    </div></fieldset>
    <fieldset class="span-3 fs"><legend>${t("form.identifiers")}</legend><div class="form-grid">
      ${txt("case_subtype", t("form.case_type"), { list: "dl_case_types", ph: t("form.search_ph") })}
      ${txt("case_number", t("form.case_number"))}${txt("filing_number", t("form.filing_number"))}
      ${txt("cnr_number", t("form.cnr"), { ph: "MHPU010012342023", hint: t("form.cnr_hint") })}
      ${txt("fir_number", t("form.fir"), { ph: "123/2024" })}
      <div><label class="f" for="f_filing_year">${t("form.filing_year")}</label><select id="f_filing_year" name="filing_year"><option value="">—</option>${Array.from({ length: year - 1949 }, (_, i) => year - i).map((y) => html`<option ${String(y) === String(c.filing_year || "") ? raw("selected") : ""}>${y}</option>`)}</select></div>
    </div></fieldset>
    <fieldset class="span-3 fs"><legend>${t("form.jurisdiction")}</legend><div class="form-grid">
      ${txt("court", t("form.court"), { list: "dl_courts", ph: t("form.search_ph"), cls: "span-2" })}
      <div><label class="f" for="f_state">${t("form.state")}</label><select id="f_state" name="state"><option value="">—</option>${ref.states.map((s) => html`<option ${s === (c.state || "") ? raw("selected") : ""}>${s}</option>`)}</select></div>
      <div id="districtWrap"></div>
      <div id="psWrap">${txt("police_station", t("form.police_station"), { hint: t("form.police_station_hint") })}</div>
    </div></fieldset>
    <fieldset class="span-3 fs"><legend>${t("form.status_dates")}</legend><div class="form-grid">
      <div><label class="f" for="f_filing_date">${t("form.filing_date")}</label><input type="date" id="f_filing_date" name="filing_date" value="${c.filing_date || ""}"></div>
      <div><label class="f" for="f_next_hearing_date">${t("form.next_hearing")}</label><input type="date" id="f_next_hearing_date" name="next_hearing_date" value="${c.next_hearing_date || ""}"></div>
      <div><label class="f" for="f_status">${t("col.status")}</label><select id="f_status" name="status">${statuses.map((s) => html`<option ${s === (c.status || "Active / In Court") ? raw("selected") : ""}>${s}</option>`)}</select></div>
    </div></fieldset>
    ${txt("acts_sections", t("form.acts"), { cls: "span-3", hint: t("form.acts_hint") })}
    <div class="span-3"><label class="f" for="f_description">${t("form.description")}</label><textarea id="f_description" name="description" rows="4">${c.description || ""}</textarea></div>
    ${isNew ? html`<div class="span-3"><label class="f" for="f_files">${t("form.supporting_docs")}</label><input type="file" id="f_files" multiple accept=".pdf,.docx,.doc,.txt,.md,.png,.jpg,.jpeg,.tif,.tiff,.webp"><div class="hint">${t("form.supporting_docs_hint")}</div></div>` : ""}
    <datalist id="dl_courts">${ref.courts.map((x) => html`<option value="${x.name}">`)}</datalist>
    <datalist id="dl_case_types"></datalist>
  </div>`;
}

function bindCaseForm(form, ref, c) {
  const cat = $("#f_case_type", form), cRole = $("#f_client_role", form), oRole = $("#f_opposite_role", form);
  const roleOpt = (v, sel) => html`<option value="${v}" ${v === sel ? raw("selected") : ""}>${t(`role.${v}`, {}, v)}${lang() !== "en" && t(`role.${v}`, {}, v) !== v ? ` (${v})` : ""}</option>`;
  const fillOpposite = () => {
    const roles = ref.roles[cat.value]; const pref = roles.opposite[cRole.value] || [];
    const list = [...new Set([...pref, ...roles.all_opposite])];
    const sel = oRole.dataset.value && list.includes(oRole.dataset.value) ? oRole.dataset.value : pref[0];
    mount(oRole, html`<option value="">—</option>${list.map((v) => roleOpt(v, sel))}`); oRole.dataset.value = "";
  };
  const fillCategory = () => {
    const roles = ref.roles[cat.value];
    const sel = cRole.dataset.value && roles.client.includes(cRole.dataset.value) ? cRole.dataset.value : roles.client[0];
    mount(cRole, html`<option value="">—</option>${roles.client.map((v) => roleOpt(v, sel))}`); cRole.dataset.value = "";
    fillOpposite();
    mount($("#dl_case_types", form), html`${(ref.case_types[cat.value] || []).map((x) => html`<option value="${x}">`)}`);
    togglePS();
  };
  const togglePS = () => {
    const show = cat.value === "Criminal" || $("#f_fir_number", form).value.trim() !== "";
    $("#psWrap", form).classList.toggle("hidden", !show);
  };
  const fillDistricts = () => {
    const st = $("#f_state", form).value; const list = ref.districts[st];
    const cur = $("#f_district", form)?.value ?? c.district ?? "";
    mount($("#districtWrap", form), list ? html`<label class="f" for="f_district">${t("form.district")}</label><select id="f_district" name="district"><option value="">—</option>${list.map((d) => html`<option ${d === cur ? raw("selected") : ""}>${d}</option>`)}</select>`
      : html`<label class="f" for="f_district">${t("form.district")}</label><input type="text" id="f_district" name="district" value="${cur}" ${st ? "" : raw("disabled")} placeholder="${st ? "" : t("form.choose_state_first")}">`);
  };
  cat.onchange = fillCategory; cRole.onchange = fillOpposite;
  $("#f_fir_number", form).oninput = togglePS;
  $("#f_state", form).onchange = fillDistricts;
  fillCategory(); fillDistricts();
}
const caseBody = (form) => { const b = formData(form); if (!b.filing_year) delete b.filing_year; return b; };

// ── Single case ──
async function viewCase(main, r) {
  const id = r.arg;
  const c = await api(`/api/cases/${encodeURIComponent(id)}`);
  crumbs([[t("nav.cases"), "#/cases"], [c.title]]);
  const may = c.can || {};
  if (!c.access) {   // an administrator who is not on this case: listing data and the case team only
    mount(main, html`<div class="row"><h1 class="page-title">${c.title}</h1><span class="badge ${TYPE_BADGE[c.case_type] || ""}">${t(`cat.${c.case_type}`, {}, c.case_type)}</span></div>
      <p class="page-lead">${[c.case_number || c.cnr_number || t("case.no_number"), c.court || t("case.no_court"), c.status].filter(Boolean).join(" · ")}</p>
      <div class="notice" style="margin-bottom:16px">${t("members.admin_not_member")}</div><div id="membersPanel"></div>`);
    membersCard($("#membersPanel"), id);
    return;
  }
  const [meta, docsRes, ref] = await Promise.all([api("/api/cases"), api(`/api/documents?case_id=${encodeURIComponent(id)}`), reference()]);
  state.caseId = id; store.set("caseId", id);
  const types = docsRes.types;
  const idents = [["form.case_number", c.case_number], ["form.filing_number", c.filing_number], ["form.cnr", c.cnr_number],
    ["form.fir", c.fir_number], ["form.police_station", c.police_station], ["form.filing_year", c.filing_year]].filter(([, v]) => v);
  mount(main, html`
    <div class="row"><h1 class="page-title">${c.title}</h1><span class="badge ${TYPE_BADGE[c.case_type] || ""}">${t(`cat.${c.case_type}`, {}, c.case_type)}</span>
      ${c.is_draft ? html`<span class="chip chip-warn">${t("cases.draft_badge")}</span>` : ""}
      <span class="spacer"></span>${may.drafts ? html`<a class="btn btn-ghost" href="#/drafting?case=${encodeURIComponent(id)}">✍ ${t("qa.draft")}</a>` : ""}
      <a class="btn btn-saffron" href="#/research?case=${encodeURIComponent(id)}">💬 ${t("case.research_this")}</a></div>
    <p class="page-lead">${[c.case_subtype, c.case_number || c.cnr_number || t("case.no_number"), c.court || t("case.no_court"), [c.district, c.state].filter(Boolean).join(", "), c.status].filter(Boolean).join(" · ")}</p>
    ${c.is_draft ? html`<div class="notice" style="margin-bottom:16px">${t("case.draft_notice")}</div>` : ""}
    <div class="grid g-side">
      <div class="stack">
        <div class="card dossier"><div class="card-h"><h2>${t("case.dossier")}</h2>${may["case.update"] ? html`<button class="btn btn-ghost btn-sm" id="editBtn">${t("common.edit")}</button>` : ""}</div>
          <div class="card-b" id="dossierView"><dl>
            <dt>${t("col.client")}</dt><dd>${c.client_name || "—"} (${c.client_role || "—"})</dd>
            <dt>${t("form.opposite_name")}</dt><dd>${c.opposite_party || "—"} (${c.opposite_role || "—"})</dd>
            ${idents.map(([k, v]) => html`<dt>${t(k)}</dt><dd>${v}</dd>`)}
            <dt>${t("form.filing_date")}</dt><dd>${fmtDate(c.filing_date)}</dd>
            <dt>${t("form.next_hearing")}</dt><dd>${fmtDate(c.next_hearing_date)}</dd>
            <dt>${t("form.acts")}</dt><dd>${c.acts_sections || "—"}</dd>
            <dt>${t("case.facts")}</dt><dd>${c.description || "—"}</dd></dl></div>
          <form class="card-b hidden" id="dossierForm" novalidate>${caseFormFields(c, meta.statuses, ref, false)}
            <div class="row" style="margin-top:12px"><button class="btn" type="submit" data-final="1">${c.is_draft ? t("case.save_complete") : t("common.save")}</button>
            ${c.is_draft ? html`<button class="btn btn-ghost" type="submit" data-final="0">${t("cases.save_draft")}</button>` : ""}
            <button class="btn btn-ghost" type="button" id="cancelEdit">${t("common.cancel")}</button>
            <span class="spacer"></span>${may["case.delete"] ? html`<button class="btn btn-danger" type="button" id="deleteCase">${t("case.delete")}</button>` : ""}</div></form>
        </div>
        <div id="provPanel"></div>
        <div class="card"><div class="card-h"><h2>${t("case.documents")}</h2><span class="small muted">${t("case.doc_formats")}</span></div>
          <div class="card-b">
            ${may["documents.upload"] ? html`<div class="row" style="margin-bottom:10px"><label class="f" for="docType" style="margin:0">${t("case.doc_type")}</label>
              <select id="docType" style="max-width:260px"><option value="auto">${t("case.detect")}</option>${Object.entries(types).filter(([k]) => k !== "template").map(([k, v]) => html`<option value="${k}">${v}</option>`)}</select></div>
            <label class="dropzone" id="drop"><input type="file" id="fileInput" multiple accept=".pdf,.docx,.doc,.txt,.md,.png,.jpg,.jpeg,.tif,.tiff,.webp">
              <strong>${t("case.drop")}</strong><br><span class="small muted">${t("case.drop_hint")}</span></label>` : ""}
            <div id="docList" style="margin-top:8px"></div>
          </div></div>
      </div>
      <div class="stack">
        <div id="membersPanel"></div>
        ${may.notes ? html`<div class="card"><div class="card-h"><h2>${t("case.notes")}</h2></div><div class="card-b">
          <form id="noteForm" class="stack"><textarea id="noteText" placeholder="${t("case.note_ph")}" aria-label="${t("case.new_note")}"></textarea>
            <button class="btn btn-sm" type="submit">${t("case.add_note")}</button></form>
          <div style="margin-top:12px">${(c.notes || []).length ? c.notes.map((n) => html`<div class="note"><div class="small muted">${n.author} · ${fmtDate(n.created_at)}</div>${n.content}</div>`) : html`<div class="empty">${t("case.no_notes")}</div>`}</div>
        </div></div>` : ""}
        <div class="card"><div class="card-h"><h2>${t("case.ask")}</h2></div><div class="card-b quick">
          ${["case.q1", "case.q2", "case.q3", "case.q4"].map((k) =>
            html`<a class="btn btn-ghost" href="#/research?case=${encodeURIComponent(id)}&q=${encodeURIComponent(t(k))}">${t(k)}</a>`)}
        </div></div>
      </div>
    </div>`);
  if (may.provisions) provisionsPanel($("#provPanel"), c, () => route());
  membersCard($("#membersPanel"), id);

  const renderDocs = (docs) => {
    mount($("#docList"), docs.length ? html`${docs.map((d) => html`<div class="doc-item">
      <span class="doc-ico">${(d.filename.split(".").pop() || "").toUpperCase()}</span>
      <div class="doc-main"><div class="doc-title">${d.title}</div>
        <div class="small muted">${types[d.doc_type] || d.doc_type} · ${d.pages ? `${t("case.pages", { n: d.pages })} · ` : ""}${d.n_chunks ? `${t("case.passages", { n: d.n_chunks })} · ` : ""}${d.meta?.lang_name ? `${d.meta.lang_name} · ` : ""}${d.meta?.ocr ? `OCR · ` : ""}${fmtDate(d.created_at)}${d.meta?.instruction_like_text ? html` <span class="chip chip-warn" title="${t("guard.injection_hint")}">${t("guard.injection")}</span>` : ""}</div>
        ${d.status === "processing" ? html`<div class="status-line small"><span class="spinner"></span> ${t("case.indexing")}</div>` : ""}
        ${d.status === "failed" ? html`<div class="small" style="color:var(--red)">⚠ ${d.error || t("common.failed")}</div>` : ""}
        <div class="doc-actions">
          ${d.status === "ready" ? html`
            <a class="btn btn-saffron btn-sm" href="#/doc/${encodeURIComponent(d.id)}">${t("case.open_doc")}</a>
            <a class="btn btn-ghost btn-sm" href="#/research?case=${encodeURIComponent(id)}&doc=${encodeURIComponent(d.id)}&task=summary">${t("case.summarise")}</a>
            <a class="btn btn-ghost btn-sm" href="#/research?case=${encodeURIComponent(id)}&doc=${encodeURIComponent(d.id)}&task=timeline">${t("case.timeline")}</a>
            <a class="btn btn-ghost btn-sm" href="/api/documents/${encodeURIComponent(d.id)}/file" target="_blank" rel="noopener">${t("case.original")} ↗</a>` : ""}
          ${may["documents.delete"] ? html`<button class="btn btn-danger btn-sm" data-del="${d.id}">${t("common.delete")}</button>` : ""}</div></div></div>`)}` : html`<div class="empty">${t("case.no_docs")}</div>`);
    $$("[data-del]", $("#docList")).forEach((b) => { b.onclick = async () => {
      if (!confirm(t("case.confirm_delete_doc"))) return;
      try { await api(`/api/documents/${encodeURIComponent(b.dataset.del)}`, { method: "DELETE" }); toast(t("case.doc_deleted")); loadDocs(); } catch (e) { toast(e.message, true); }
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
        toast(d.duplicate ? t("case.duplicate", { name: f.name }) : t("case.uploaded", { name: f.name })); }
      catch (e) { toast(`${f.name}: ${e.message}`, true); }
    }
    loadDocs();
  };
  const drop = $("#drop");
  if (drop) {
    $("#fileInput").onchange = (e) => { upload([...e.target.files]); e.target.value = ""; };
    ["dragenter", "dragover"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.add("drag"); }));
    ["dragleave", "drop"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.remove("drag"); }));
    drop.addEventListener("drop", (e) => upload([...e.dataTransfer.files]));
  }

  const form = $("#dossierForm");
  bindCaseForm(form, ref, c);
  if ($("#editBtn")) $("#editBtn").onclick = () => { $("#dossierView").classList.add("hidden"); form.classList.remove("hidden"); };
  if (c.is_draft && r.q.get("edit")) $("#editBtn")?.click();
  $("#cancelEdit").onclick = () => { form.classList.add("hidden"); $("#dossierView").classList.remove("hidden"); };
  form.onsubmit = async (e) => {
    e.preventDefault();
    const body = caseBody(form);
    if (c.is_draft) body.is_draft = e.submitter?.dataset.final === "0";
    try { await api(`/api/cases/${encodeURIComponent(id)}`, { method: "PATCH", body }); toast(t("common.saved")); route(); } catch (err) { fieldError(form, err); }
  };
  if ($("#deleteCase")) $("#deleteCase").onclick = async () => {
    if (!confirm(t("case.confirm_delete"))) return;
    try { await api(`/api/cases/${encodeURIComponent(id)}`, { method: "DELETE" }); state.caseId = null; store.set("caseId", null); toast(t("case.deleted")); location.hash = "#/cases"; } catch (err) { toast(err.message, true); }
  };
  if ($("#noteForm")) $("#noteForm").onsubmit = async (e) => {
    e.preventDefault(); const x = $("#noteText").value.trim(); if (!x) return;
    try { await api(`/api/cases/${encodeURIComponent(id)}/notes`, { method: "POST", body: { content: x } }); route(); } catch (err) { toast(err.message, true); }
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
  crumbs([[t("nav.research")], ...(cur ? [[cur.title, `#/cases/${encodeURIComponent(cur.id)}`]] : [])]);
  mount(main, html`
    <h1 class="page-title">${t("nav.research")}</h1>
    <p class="page-lead">${cur ? t("research.lead_case") : t("research.lead")}</p>
    <div class="research">
      <aside class="side" aria-label="${t("research.settings")}">
        <div class="card"><div class="card-h"><h3>${t("research.matter")}</h3></div><div class="card-b">
          <select id="caseSel" aria-label="${t("research.select_case")}"><option value="">${t("research.general_option")}</option>${cases.map((c) => html`<option value="${c.id}" ${c.id === state.caseId ? raw("selected") : ""}>[${t(`cat.${c.case_type}`, {}, c.case_type)}] ${c.title}</option>`)}</select>
          <div class="scope" role="radiogroup" aria-label="${t("research.scope")}" style="margin-top:10px">
            <label><input type="radio" name="scope" value="all" ${state.scope === "all" ? raw("checked") : ""}> <span><strong>${t("research.scope_all")}</strong><br><span class="small muted">${t("research.scope_all_hint")}</span></span></label>
            <label><input type="radio" name="scope" value="case" ${state.scope === "case" ? raw("checked") : ""} ${cur ? "" : raw("disabled")}> <span><strong>${t("research.scope_case")}</strong><br><span class="small muted">${t("research.scope_case_hint")}</span></span></label>
            <label><input type="radio" name="scope" value="library" ${state.scope === "library" ? raw("checked") : ""}> <span><strong>${t("research.scope_library")}</strong><br><span class="small muted">${t("research.scope_library_hint")}</span></span></label>
          </div>
          <div id="caseDocs" class="small" style="margin-top:8px"></div></div></div>
        <div class="card"><div class="card-h"><h3>${t("research.conversations")}</h3><button class="btn btn-sm" id="newChat">＋ ${t("research.new")}</button></div>
          <ul class="chat-list" id="chatList"></ul></div>
      </aside>
      <section class="card chatbox" aria-label="${t("research.conversation")}">
        <div class="thread" id="thread" aria-live="polite"></div>
        <form class="composer" id="composer">
          <div class="suggest" id="suggest"></div>
          ${cur ? html`<div class="row small" style="margin-bottom:6px"><label for="focusDoc" class="muted">${t("research.focus_doc")}</label>
            <select id="focusDoc" style="max-width:320px"><option value="">${t("research.all_docs")}</option></select></div>` : ""}
          <div class="row" style="align-items:flex-end;flex-wrap:nowrap">
            <textarea id="q" placeholder="${t("research.placeholder")}" aria-label="${t("research.your_question")}" rows="2"></textarea>
            <span id="voiceSlot"></span>
            <button class="btn btn-saffron" type="submit" id="sendBtn" style="height:54px">${t("research.send")} ➤</button></div>
          <div class="small muted" style="margin-top:6px">${t("research.enter_hint")} · ${cur ? t("research.using_case") : t("research.select_case_hint")} · ${t("research.answer_lang", { lang: LANGS.find((l) => l.code === lang())?.label || "English" })}</div>
          <div id="voiceStatus" class="small" aria-live="polite"></div>
        </form>
      </section>
      <aside class="card sources-col" aria-label="${t("research.sources")}"><div class="card-h"><h3>${t("research.sources")}</h3><span class="small muted" id="srcCount"></span></div>
        <div class="card-b" id="sourcesPanel"><div class="empty">${t("research.sources_empty")}</div></div></aside>
    </div>`);

  $("#caseSel").onchange = (e) => { location.hash = `#/research?case=${encodeURIComponent(e.target.value)}`; };
  $$("input[name=scope]").forEach((el) => { el.onchange = () => { state.scope = el.value; store.set("scope", el.value); }; });
  $("#newChat").onclick = () => {
    const h = `#/research?case=${encodeURIComponent(state.caseId || "")}`;
    if (location.hash === h) route(); else location.hash = h;
  };
  const qEl = $("#q");
  qEl.addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); $("#composer").requestSubmit(); } });
  $("#composer").onsubmit = (e) => {
    e.preventDefault(); const x = qEl.value.trim(); if (!x || state.streaming) return;
    const mode = qEl.dataset.inputMode || "text"; qEl.value = ""; qEl.dataset.inputMode = "";
    const focus = $("#focusDoc")?.value;
    send(x, { input_mode: mode, ...(focus ? { doc_id: focus, task: "custom" } : {}) });
  };
  voiceControls($("#voiceSlot"), qEl, $("#voiceStatus"));
  $("#thread").addEventListener("click", onCiteClick);
  bindSpeak($("#thread"));
  $("#sourcesPanel").addEventListener("click", (e) => { const card = e.target.closest("[data-src]"); if (card) openSource(JSON.parse(card.dataset.src)); });

  const suggestions = cur ? [t("research.s_case1"), t("research.s_case2"), t("research.s_case3"), cur.case_type === "Criminal" ? t("research.s_case4_crim") : t("research.s_case4")]
    : [t("research.s1"), t("research.s2"), t("research.s3"), t("research.s4")];
  mount($("#suggest"), html`${suggestions.map((s) => html`<button type="button" data-s="${s}">${s}</button>`)}`);
  $$("[data-s]", $("#suggest")).forEach((b) => { b.onclick = () => { if (!state.streaming) send(b.dataset.s); }; });

  loadChatList();
  if (cur) loadCaseDocs(cur.id);
  if (state.chatId) await loadChat(state.chatId);
  else mount($("#thread"), html`<div class="empty" id="welcome"><strong>${t("research.namaste")}</strong> ${cur ? t("research.welcome_case", { title: cur.title }) : t("research.welcome")}<br>${t("research.examples")}</div>`);

  const doc = r.q.get("doc"), task = r.q.get("task"), pre = r.q.get("q");
  history.replaceState(null, "", `#/research?case=${encodeURIComponent(state.caseId || "")}${state.chatId ? `&chat=${encodeURIComponent(state.chatId)}` : ""}`);
  if (doc && task) {
    let title = t("research.this_document"); try { title = `“${(await api(`/api/documents/${encodeURIComponent(doc)}`)).title}”`; } catch { /* keep generic */ }
    send(t(`research.task_${task}`, { title }, `${t("research.task_custom", { title })}`), { doc_id: doc, task });
  } else if (pre) { qEl.value = pre; qEl.focus(); }
  else qEl.focus();
}

async function loadCaseDocs(caseId) {
  try {
    const { documents } = await api(`/api/documents?case_id=${encodeURIComponent(caseId)}`);
    mount($("#caseDocs"), documents.length ? html`<div class="muted" style="margin-bottom:4px">${t("research.docs_in_case")}</div>${documents.slice(0, 8).map((d) => html`<div>• ${d.title} ${d.status !== "ready" ? html`<span class="chip ${d.status === "failed" ? "chip-err" : "chip-wait"}">${d.status}</span>` : ""}</div>`)}`
      : html`<div class="notice">${t("research.no_docs")} <a href="#/cases/${encodeURIComponent(caseId)}">${t("research.upload_link")}</a>.</div>`);
    const sel = $("#focusDoc");
    if (sel) documents.filter((d) => d.status === "ready").forEach((d) => { const o = document.createElement("option"); o.value = d.id; o.textContent = d.title; sel.appendChild(o); });
  } catch { /* ignore */ }
}

async function loadChatList() {
  try {
    const { chats } = await api(`/api/chats${state.caseId ? `?case_id=${encodeURIComponent(state.caseId)}` : ""}`);
    mount($("#chatList"), chats.length ? html`${chats.map((c) => html`<li><a href="#/research?case=${encodeURIComponent(state.caseId || "")}&chat=${encodeURIComponent(c.id)}" class="${c.id === state.chatId ? "active" : ""}" title="${c.title}">${c.title}</a>
      <button type="button" data-delchat="${c.id}" aria-label="${t("research.delete_chat")}" title="${t("common.delete")}">✕</button></li>`)}` : html`<li class="empty small">${t("research.no_chats")}</li>`);
    $$("[data-delchat]", $("#chatList")).forEach((b) => { b.onclick = async () => {
      if (!confirm(t("research.confirm_delete_chat"))) return;
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
const INTENT = () => ({ research: t("intent.research"), drafting: t("intent.drafting"), analyze_document: t("intent.analyze_document"), calculation: t("intent.calculation"), chitchat: t("intent.chitchat") });
// Where the answer came from: everything the chat uses is local (case files + library); the only network call is the LLM.
const ORIGIN = (sources) => {
  const kinds = new Set((sources || []).map((s) => s.collection));
  if (!kinds.size) return "";
  return [kinds.has("case") && t("origin.case_files"), [...kinds].some((k) => k !== "case") && t("origin.library")].filter(Boolean).join(" + ");
};

function renderAi(el, content, sources, meta, live = null) {
  state.msgSources.set(el.dataset.mid, sources || []);
  const v = meta.verify;
  const origin = ORIGIN(sources);
  const metaRow = html`<div class="msg-meta">
    ${meta.intent ? html`<span class="chip">${INTENT()[meta.intent] || meta.intent}</span>` : ""}
    ${origin ? html`<span class="chip" title="${t("origin.hint")}">📂 ${t("origin.answered_from", { where: origin })}</span>` : ""}
    ${sources && sources.length ? html`<span class="chip">${t("research.n_sources", { n: sources.length })}</span>` : ""}
    ${meta.elapsed ? html`<span>${meta.elapsed}s</span>` : ""}
    ${!live && content ? speakButton(meta.lang) : ""}</div>`;
  const status = live ? html`<div class="status-line"><span class="spinner"></span><span>${live}</span></div>` : "";
  const verify = v ? (v.grounded
    ? html`<div class="verify verify-ok">✓ ${t("research.audit_ok")}</div>`
    : html`<div class="verify verify-warn">⚠ ${t("research.audit_warn")}${v.unsupported && v.unsupported.length ? html`<ul>${v.unsupported.map((u) => html`<li>${u}</li>`)}</ul>` : ""}</div>`) : "";
  const err = meta.error && meta.error !== "internal" && !content.includes(meta.error) ? html`<div class="notice notice-red">${meta.error}</div>` : "";
  const disclaimer = !live && meta.intent && meta.intent !== "chitchat" ? html`<div class="small muted">${t("research.disclaimer")}</div>` : "";
  mount(el, html`${metaRow}<div class="md">${markdown(content)}</div>${status}<div class="msg-foot">${err}${verify}${disclaimer}</div>`);
}

function renderSourcesPanel(sources) {
  $("#srcCount").textContent = sources.length ? t("research.n_used", { n: sources.length }) : "";
  const L = COLL_LABEL();
  mount($("#sourcesPanel"), sources.length ? html`${sources.map((s) => html`<div class="src-card" role="button" tabindex="0" data-src="${JSON.stringify(s)}">
    <div><span class="src-n">${s.n}</span><span class="badge b-${s.collection}">${L[s.collection] || s.kind}</span> ${s.exact ? html`<span class="chip chip-ok" title="${t("research.exact_hint")}">${t("research.exact")}</span>` : ""}${s.injection_suspected ? html` <span class="chip chip-warn" title="${t("guard.injection_hint")}">${t("guard.injection")}</span>` : ""}</div>
    <div class="src-title" style="margin-top:4px">${s.title}</div>
    <div class="small muted">${[s.label, s.page && !/^pp?\./.test(s.label || "") ? `p. ${s.page}` : ""].filter(Boolean).join(" · ")}</div>
    <div class="src-snip">${s.snippet}</div></div>`)}` : html`<div class="empty">${t("research.no_sources")}</div>`);
}

function onCiteClick(e) {
  const b = e.target.closest("button.cite"); if (!b) return;
  const msg = b.closest(".msg-ai"); const list = state.msgSources.get(msg?.dataset.mid) || [];
  const s = list.find((x) => String(x.n) === b.dataset.cite);
  if (s) openSource(s); else toast(t("research.bad_cite"), true);
}

export async function openSource(s) {
  const body = $("#drawerBody");
  const L = COLL_LABEL();
  const fileLink = `/api/documents/${encodeURIComponent(s.doc_id)}/file${s.page ? `#page=${s.page}` : ""}`;
  mount(body, html`<h3 style="margin-top:0;color:var(--navy)">${s.title}</h3><p class="small muted">${[s.label, s.page ? `p. ${s.page}` : ""].filter(Boolean).join(" · ")}</p>
    <div id="passage">${spinner(t("research.loading_passage"))}</div>
    <div class="row" style="margin-top:12px"><a class="btn" href="${fileLink}" target="_blank" rel="noopener">${t("research.open_original")}${s.page ? ` (p. ${s.page})` : ""} ↗</a></div>`);
  showDrawer(`${t("research.source")} [${s.n ?? "•"}] — ${L[s.collection] || s.kind || ""}`);
  if (s.chunk_id) {
    try { const c = await api(`/api/chunks/${encodeURIComponent(s.chunk_id)}`);
      mount($("#passage"), html`<div class="small muted" style="margin-bottom:6px">${c.header}</div><pre class="passage">${c.text}</pre>`); }
    catch (e) { mount($("#passage"), html`<div class="notice notice-red">${e.message}</div>`); }
  } else mount($("#passage"), html`<pre class="passage">${s.snippet}</pre>`);
}

async function send(text, extra = {}) {
  if (state.streaming) return;
  state.streaming = true; $("#sendBtn").disabled = true;
  addUserMsg(text);
  const el = addAiMsg(); const thread = $("#thread");
  let content = "", sources = [], meta = { lang: lang() }, live = t("research.starting"), pending = false;
  const paint = () => { pending = false; renderAi(el, content, sources, meta, live); thread.scrollTop = thread.scrollHeight; };
  const schedule = () => { if (!pending) { pending = true; requestAnimationFrame(paint); } };
  paint();
  try {
    const r = await fetch("/api/chat", { method: "POST", credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-Nyaya-CSRF": state.csrf || "" },
      body: JSON.stringify({ message: text, chat_id: state.chatId, case_id: state.caseId, scope: state.scope, lang: lang(), ...extra }) });
    if (!r.ok) { let m = ""; try { m = (await r.json()).detail; } catch { /* */ } throw new Error(m || t("err.request_failed", { status: r.status })); }
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
          case "status": live = t(`step.${evt.step}`, {}, evt.text); break;
          case "meta": meta.intent = evt.intent; break;
          case "sources": sources = evt.items; renderSourcesPanel(sources); break;
          case "token": content += evt.text; if (live && !/verify/.test(live)) live = t("research.writing"); break;
          case "verify": meta.verify = evt; break;
          case "final": content = evt.text; break;   // the server removed something from the reply (output filter)
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

// ── Bare acts + precedents ──
async function viewActs(main, r) {
  crumbs([[t("nav.acts")]]);
  const lib = await api("/api/library");
  const acts = lib.bare_act;
  const tab = r.q.get("tab") === "judgments" ? "judgments" : "acts";
  mount(main, html`
    <h1 class="page-title">${t("acts.title")}</h1>
    <p class="page-lead">${t("acts.lead")}</p>
    <div class="tabs" role="tablist"><a role="tab" class="tab ${tab === "acts" ? "active" : ""}" href="#/acts">${t("acts.tab_acts")}</a><a role="tab" class="tab ${tab === "judgments" ? "active" : ""}" href="#/acts?tab=judgments">${t("acts.tab_judgments")}</a></div>
    ${tab === "acts" ? html`
    <div class="card"><div class="card-h"><h2>${t("acts.lookup")}</h2></div>
      <form class="card-b row" id="lookupForm" style="align-items:flex-end">
        <div style="flex:2;min-width:240px"><label class="f" for="lq">${t("acts.lookup_label")}</label><input type="search" id="lq" placeholder="${t("acts.lookup_ph")}" value="${r.q.get("q") || ""}"></div>
        <div style="flex:1;min-width:200px"><label class="f" for="lact">${t("acts.act")}</label><select id="lact"><option value="">${t("acts.all_acts")}</option>${acts.map((a) => html`<option value="${a.title}" ${a.title === r.q.get("act") ? raw("selected") : ""}>${a.title}</option>`)}</select></div>
        <button class="btn" type="submit">${t("common.search")}</button></form>
      <div id="lookupResults"></div></div>
    <h2 class="section-title" style="margin-top:22px">${t("acts.in_library", { n: acts.length })}</h2>
    <div class="grid g-3">${acts.map((a) => html`<div class="card act-card">
      <h3>${a.title}</h3>
      <div class="row">${a.meta.short ? html`<span class="chip">${a.meta.short}</span>` : ""}${(a.meta.areas || []).map((x) => html`<span class="badge ${TYPE_BADGE[x] || ""}">${t(`cat.${x}`, {}, x)}</span>`)}
        ${a.meta.repealed ? html`<span class="chip chip-warn" title="${a.meta.note || ""}">${t("acts.repealed")}</span>` : ""}
        ${a.status !== "ready" ? html`<span class="chip chip-wait">${a.status}</span>` : ""}</div>
      <div class="small muted">${t("acts.meta", { pages: a.pages || "?", n: a.n_chunks || 0 })}${a.meta.act_number ? ` · Act ${a.meta.act_number} of ${a.meta.year}` : ""}${a.meta.fetched_at ? ` · ${t("acts.fetched", { date: fmtDate(a.meta.fetched_at) })}` : ""}</div>
      <div class="row"><a class="btn btn-ghost btn-sm" href="/api/documents/${encodeURIComponent(a.id)}/file" target="_blank" rel="noopener">${t("acts.open_pdf")} ↗</a>
        ${a.meta.item_url ? html`<a class="btn btn-ghost btn-sm" href="${a.meta.item_url}" target="_blank" rel="noopener noreferrer">India Code ↗</a>` : ""}
        <a class="btn btn-ghost btn-sm" href="#/research?case=&q=${encodeURIComponent(`Under the ${a.title}, `)}">${t("acts.ask")}</a></div></div>`)}</div>
    ${acts.length ? "" : html`<div class="notice">${t("acts.empty")}</div>`}` : html`
    <div class="card"><div class="card-h"><h2>${t("acts.precedents")}</h2></div>
      <form class="card-b row" id="jForm" style="align-items:flex-end">
        <div style="flex:2;min-width:240px"><label class="f" for="jq">${t("acts.precedent_label")}</label><input type="search" id="jq" placeholder="${t("acts.precedent_ph")}" value="${r.q.get("q") || ""}"></div>
        <button class="btn" type="submit">${t("common.search")}</button></form>
      <div id="jResults"></div>
      <div class="card-b small muted">${t("acts.precedent_note", { n: lib.judgment.length })}</div></div>
    <div class="grid g-3" style="margin-top:16px">${lib.judgment.map((j) => html`<div class="card act-card"><h3>${j.title}</h3>
      <div class="small muted">${t("acts.meta", { pages: j.pages || "?", n: j.n_chunks || 0 })}${j.meta.source ? ` · ${j.meta.source}` : ""}</div>
      <div class="row"><a class="btn btn-ghost btn-sm" href="/api/documents/${encodeURIComponent(j.id)}/file" target="_blank" rel="noopener">${t("acts.open_pdf")} ↗</a></div></div>`)}</div>`}`);
  if (tab === "judgments") {
    const runJ = async () => {
      const q = $("#jq").value.trim(); if (!q) return;
      mount($("#jResults"), html`<div class="card-b">${spinner(t("common.searching"))}</div>`);
      try {
        const res = await api(`/api/search?q=${encodeURIComponent(q)}&types=judgment`);
        mount($("#jResults"), res.results.length ? html`${res.results.map((x) => html`<div class="result"><h3>${x.title}</h3>
          <div class="small muted">${x.label || ""}${x.page ? ` · p. ${x.page}` : ""} · <a href="${x.url}" target="_blank" rel="noopener">${t("acts.open_pdf")} ↗</a></div><pre>${x.snippet}</pre></div>`)}`
          : html`<div class="card-b empty">${t("acts.no_precedents")}</div>`);
      } catch (e) { mount($("#jResults"), html`<div class="card-b notice notice-red">${e.message}</div>`); }
    };
    $("#jForm").onsubmit = (e) => { e.preventDefault(); runJ(); };
    if (r.q.get("q")) runJ();
    return;
  }
  const run = async () => {
    const q = $("#lq").value.trim(); if (!q) return;
    mount($("#lookupResults"), html`<div class="card-b">${spinner(t("common.searching"))}</div>`);
    try {
      const res = await api(`/api/library/lookup?q=${encodeURIComponent(q)}${$("#lact").value ? `&act=${encodeURIComponent($("#lact").value)}` : ""}`);
      mount($("#lookupResults"), res.results.length ? html`${res.results.map((x) => html`<div class="result"><h3>${x.label ? html`<span class="chip ${x.exact ? "chip-ok" : ""}">${x.label}</span> ` : ""}${x.header.split(" — ").slice(1).join(" — ") || x.header}</h3>
        <div class="small muted" style="margin-bottom:6px">${x.title}${x.page ? ` · p. ${x.page}` : ""} · <a href="/api/documents/${encodeURIComponent(x.doc_id)}/file#page=${x.page || 1}" target="_blank" rel="noopener">${t("acts.open_pdf")} ↗</a></div><pre>${x.text}</pre></div>`)}`
        : html`<div class="card-b empty">${t("acts.no_match")}</div>`);
    } catch (e) { mount($("#lookupResults"), html`<div class="card-b notice notice-red">${e.message}</div>`); }
  };
  $("#lookupForm").onsubmit = (e) => { e.preventDefault(); run(); };
  if (r.q.get("q")) run();
}

// ── Draft templates ──
async function viewDrafts(main) {
  crumbs([[t("nav.tools"), "#/tools"], [t("nav.templates")]]);
  const [lib, { cases }] = await Promise.all([api("/api/library"), api("/api/cases")]);
  const drafts = lib.draft; const areas = ["All", ...new Set(drafts.map((d) => d.meta.area || "General"))];
  let area = "All";
  mount(main, html`
    <h1 class="page-title">${t("nav.templates")}</h1>
    <p class="page-lead">${t("templates.lead")}</p>
    <div class="tabs" role="tablist">${areas.map((a) => html`<button type="button" role="tab" data-area="${a}" class="${a === "All" ? "active" : ""}">${a === "All" ? t("common.all") : t(`cat.${a}`, {}, a)}</button>`)}</div>
    <div class="grid g-3" id="draftGrid"></div>`);
  const grid = () => {
    const list = drafts.filter((d) => area === "All" || (d.meta.area || "General") === area);
    mount($("#draftGrid"), list.length ? html`${list.map((d) => html`<div class="card act-card"><h3>${d.title}</h3>
      <div class="row"><span class="badge ${TYPE_BADGE[d.meta.area] || "b-draft"}">${t(`cat.${d.meta.area || "General"}`, {}, d.meta.area || "General")}</span></div>
      <div class="row"><button class="btn btn-ghost btn-sm" data-open="${d.id}">${t("templates.read")}</button>
        <a class="btn btn-ghost btn-sm" href="#/drafting?template=${encodeURIComponent(d.id)}">${t("templates.use")}</a></div></div>`)}` : html`<div class="empty">${t("templates.none")}</div>`);
    $$("[data-open]", $("#draftGrid")).forEach((b) => { b.onclick = () => openDraft(drafts.find((d) => d.id === b.dataset.open), cases); });
  };
  $$("[data-area]").forEach((b) => { b.onclick = () => { area = b.dataset.area; $$("[data-area]").forEach((x) => x.classList.toggle("active", x === b)); grid(); }; });
  grid();
}
async function openDraft(d, cases) {
  mount($("#drawerBody"), spinner()); showDrawer(t("templates.drawer"));
  try {
    const x = await api(`/api/documents/${encodeURIComponent(d.id)}/text`);
    mount($("#drawerBody"), html`<div class="card" style="margin-bottom:12px"><div class="card-b row">
        <select id="useCase" aria-label="${t("templates.case_for")}"><option value="">${t("templates.choose_case")}</option>${cases.map((c) => html`<option value="${c.id}">[${c.case_type}] ${c.title}</option>`)}</select>
        <button class="btn btn-saffron btn-sm" id="useBtn">${t("templates.draft_for_case")}</button></div></div>
      <div class="md">${markdown(x.text)}</div>`);
    $("#useBtn").onclick = () => {
      const c = $("#useCase").value; if (!c) { toast(t("templates.choose_case_first"), true); return; }
      hideDrawer(); location.hash = `#/drafting?case=${encodeURIComponent(c)}&template=${encodeURIComponent(d.id)}`;
    };
  } catch (e) { mount($("#drawerBody"), html`<div class="notice notice-red">${e.message}</div>`); }
}

// ── Limitation ──
async function viewLimitation(main) {
  crumbs([[t("nav.tools"), "#/tools"], [t("nav.limitation")]]);
  const { categories, caveat } = await api("/api/limitation");
  const today = new Date().toISOString().slice(0, 10);
  mount(main, html`
    <h1 class="page-title">${t("limitation.title")}</h1>
    <p class="page-lead">${t("limitation.lead")}</p>
    <div class="grid g-side">
      <div class="card"><div class="card-h"><h2>${t("limitation.compute")}</h2></div>
        <form class="card-b stack" id="limForm">
          <div><label class="f" for="lc">${t("limitation.proceeding")}</label><select id="lc">${categories.map((c) => html`<option value="${c.key}">[${c.area}] ${c.label} — ${c.period}</option>`)}</select></div>
          <div><label class="f" for="ld">${t("limitation.start")}</label><input type="date" id="ld" value="${today}" required><div class="small muted" id="lstarts"></div></div>
          <button class="btn" type="submit">${t("limitation.calculate")}</button></form>
        <div id="limResult"></div></div>
      <div class="card"><div class="card-h"><h2>${t("limitation.important")}</h2></div><div class="card-b small">${caveat}</div></div>
    </div>
    <div class="card" style="margin-top:16px"><div class="card-h"><h2>${t("limitation.supported")}</h2></div><div class="table-wrap"><table class="gov">
      <thead><tr><th>${t("limitation.proceeding")}</th><th>${t("limitation.period")}</th><th>${t("limitation.starts")}</th><th>${t("limitation.basis")}</th></tr></thead>
      <tbody>${categories.map((c) => html`<tr><td>${c.label} <span class="badge ${TYPE_BADGE[c.area] || ""}">${c.area}</span></td><td>${c.period}</td><td class="small">${c.starts}</td><td class="small">${c.basis}</td></tr>`)}</tbody></table></div></div>`);
  const starts = () => { const c = categories.find((x) => x.key === $("#lc").value); $("#lstarts").textContent = c ? `${t("limitation.starts")}: ${c.starts}` : ""; };
  $("#lc").onchange = starts; starts();
  $("#limForm").onsubmit = async (e) => {
    e.preventDefault();
    try {
      const x = await api("/api/limitation", { method: "POST", body: { cause: $("#lc").value, start_date: $("#ld").value } });
      mount($("#limResult"), html`<div class="card-b"><div class="notice ${x.days_left >= 0 ? "notice-green" : "notice-red"}">
        <div class="small">${x.label} · ${x.period} · ${x.basis}</div>
        <div style="font-size:1.4rem;font-weight:800;color:var(--navy);margin:4px 0">${t("limitation.last_date")}: ${fmtDate(x.deadline)}</div>
        <div><strong>${x.status}</strong> (${t("limitation.start").toLowerCase()} ${fmtDate(x.start_date)})</div></div></div>`);
    } catch (err) { toast(err.message, true); }
  };
}

// ── Tools hub ──
async function viewTools(main) {
  crumbs([[t("nav.tools")]]);
  const items = [["⏱", t("nav.limitation"), t("tools.limitation"), "#/limitation"], ["📑", t("nav.templates"), t("tools.templates"), "#/drafts"],
    ...(can("integrations.read") ? [["🔌", t("tools.integrations"), t("tools.integrations_hint"), "#/updates?tab=sources"]] : []),
    ...(can("audit.read") ? [["🗒", t("tools.activity"), t("tools.activity_hint"), "#/updates?tab=activity"]] : []),
    ...(can("account.self") ? [["⚙", t("tools.privacy"), t("tools.privacy_hint"), "#/tools?privacy=1"]] : []), ["ℹ", t("nav.about"), t("tools.about"), "#/about"]];
  mount(main, html`<h1 class="page-title">${t("nav.tools")}</h1><p class="page-lead">${t("tools.lead")}</p>
    <div class="grid g-3">${items.map(([ico, title, hint, href]) => html`<a class="card act-card tool" href="${href}"><h3><span aria-hidden="true">${ico}</span> ${title}</h3><div class="small muted">${hint}</div></a>`)}</div>
    ${can("account.self") ? html`<div class="card" style="margin-top:16px" id="privacyCard"><div class="card-h"><h2>${t("tools.privacy")}</h2></div><form class="card-b stack" id="privForm">
      ${can("settings.manage") ? html`<div><label class="f" for="ret">${t("privacy.retention")}</label><select id="ret" name="chat_retention_days">
        ${[["", t("privacy.keep")], ["30", "30"], ["90", "90"], ["180", "180"], ["365", "365"]].map(([v, l]) => html`<option value="${v}" ${String(state.prefs.chat_retention_days || "") === v ? raw("selected") : ""}>${l}${v ? ` ${t("privacy.days")}` : ""}</option>`)}</select>
        <div class="hint">${t("privacy.retention_hint")}</div></div>` : ""}
      <div class="small">${t("privacy.voice_status")}: <strong>${state.prefs.voice_consent === "granted" ? t("privacy.granted") : t("privacy.not_granted")}</strong>
        ${state.prefs.voice_consent === "granted" ? html` <button class="btn btn-ghost btn-sm" type="button" id="revokeVoice">${t("privacy.revoke")}</button>` : ""}</div>
      <div class="small muted">${t("privacy.facts")}</div>
      ${can("settings.manage") ? html`<div><button class="btn btn-sm" type="submit">${t("common.save")}</button></div>` : ""}</form></div>` : ""}`);
  if ($("#privForm")) $("#privForm").onsubmit = async (e) => {
    e.preventDefault();
    try { state.prefs = await api("/api/prefs", { method: "PUT", body: { values: { chat_retention_days: $("#ret").value } } }); toast(t("common.saved")); } catch (err) { toast(err.message, true); }
  };
  $("#revokeVoice")?.addEventListener("click", async () => { state.prefs = await api("/api/prefs", { method: "PUT", body: { values: { voice_consent: "denied" } } }); toast(t("privacy.revoked")); route(); });
  if (new URLSearchParams(location.hash.split("?")[1] || "").get("privacy")) $("#privacyCard")?.scrollIntoView();
}

// ── About ──
async function viewAbout(main) {
  crumbs([[t("nav.tools"), "#/tools"], [t("nav.about")]]);
  const s = state.status || await api("/api/status");
  mount(main, html`
    <h1 class="page-title">${t("about.title")}</h1>
    <div class="grid g-2">
      <div class="card"><div class="card-h"><h2>${t("about.how")}</h2></div><div class="card-b md">${markdown(t("about.how_md"))}</div></div>
      <div class="card"><div class="card-h"><h2>${t("about.config")}</h2></div><div class="card-b"><table class="gov"><tbody>
        <tr><td>${t("about.answer_model")}</td><td>${s.llm_model} (${s.llm_host})</td></tr><tr><td>${t("about.fast_model")}</td><td>${s.fast_model}</td></tr>
        <tr><td>${t("about.embeddings")}</td><td>${s.embed_model} (local)</td></tr><tr><td>${t("about.reranker")}</td><td>${s.reranker || "—"} (local)</td></tr>
        <tr><td>${t("about.ocr")}</td><td>${s.ocr ? `${s.ocr.engine} (${(s.ocr.langs || []).join(", ")})` : t("about.ocr_missing")}</td></tr>
        <tr><td>${t("about.passages")}</td><td>${num(s.indexed_chunks)}</td></tr>
        <tr><td>${t("about.storage")}</td><td>${s.database || "—"} · ${s.file_storage || "—"}</td></tr></tbody></table></div></div>
      <div class="card"><div class="card-h"><h2>${t("about.chunking")}</h2></div><div class="card-b md">${markdown(t("about.chunking_md"))}</div></div>
      <div class="card"><div class="card-h"><h2>${t("about.security")}</h2></div><div class="card-b md">${markdown(t("about.security_md"))}</div></div>
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
function renderLangToggle() {
  mount($("#langToggle"), html`${LANGS.filter((l) => l.toggle).map((l, i) => html`${i ? html`<span aria-hidden="true">|</span>` : ""}<button type="button" data-lang="${l.code}" lang="${l.code}" aria-pressed="${String(lang() === l.code)}" class="${lang() === l.code ? "on" : ""}">${l.label}</button>`)}`);
  $$("[data-lang]", $("#langToggle")).forEach((b) => { b.onclick = () => switchLang(b.dataset.lang); });
}
async function switchLang(code) {
  if (code === lang()) return;
  await setLang(code); renderLangToggle(); refreshStatus(); route(); refreshBell();
  try { state.prefs = await api("/api/prefs", { method: "PUT", body: { values: { lang: code } } }); } catch { /* kept locally */ }
  toast(t("lang.switched"));
}
document.addEventListener("DOMContentLoaded", async () => {
  await setLang(initialLang());
  renderLangToggle();
  $$("[data-font]").forEach((b) => { b.onclick = () => applyFont(Number(b.dataset.font)); });
  document.documentElement.style.setProperty("--fs", [13, 14, 15, 16, 17, 18][store.get("font", 2)] + "px");
  applyContrast(store.get("contrast", false));
  $("#contrastBtn").onclick = () => applyContrast(document.documentElement.dataset.contrast !== "on");
  $("#navToggle").onclick = () => { const ul = $("#navList"); ul.classList.toggle("open"); $("#navToggle").setAttribute("aria-expanded", String(ul.classList.contains("open"))); };
  $("#drawerClose").onclick = hideDrawer;
  $("#drawer").addEventListener("click", (e) => { if (e.target.id === "drawer") hideDrawer(); });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !$("#drawer").hidden) hideDrawer(); });
  $("#logoutBtn").onclick = async () => { try { await api("/api/auth/logout", { method: "POST", retry: false }); } catch { /* already gone */ }
    state.caseId = null; store.set("caseId", null); location.hash = "#/login"; location.reload(); };
  $("#globalSearch").onsubmit = (e) => { e.preventDefault(); const q = $("#gq").value.trim(); if (q) location.hash = `#/search?q=${encodeURIComponent(q)}`; };
  window.addEventListener("hashchange", route);
  let s;
  try {
    s = await initSession();
    applyNavPermissions();
  } catch { mount($("#main"), html`<div class="notice notice-red">${t("err.server_down")}</div>`); return; }
  if (s.state === "recovery") location.hash = "#/reset-password";
  try {
    state.prefs = await api("/api/prefs", { retry: false });
    if (state.prefs.lang && state.prefs.lang !== lang() && !store.get("lang", null)) { await setLang(state.prefs.lang); renderLangToggle(); }
  } catch { /* defaults */ }
  refreshStatus(); setInterval(refreshStatus, 30000);
  const bell = () => { if (can("notifications.read")) refreshBell(); };
  bell(); setInterval(bell, 60000);
  route();
});
