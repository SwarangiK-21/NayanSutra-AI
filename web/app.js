/* NyaySutra AI — frontend entry: router, chrome, and the core views (dashboard, cases, research, library).
   Feature views live in /static/js/*.js. No external libraries (CSP: script-src 'self'). */
import { $, $$, ACCEPT_UPLOADS, api, bindRowLinks, bindTabs, COLL_LABEL, crumbs, fieldError, fmtDate, formData, hideDrawer, html,
  initSession, markdown, mount, num, raw, reference, setUnauthenticatedHandler, showDrawer, spinner, state, store,
  tabBar, tabPanel, toast, TYPE_BADGE } from "./js/core.js";
import { applyStatic, initialLang, lang, LANGS, setLang, t } from "./js/i18n.js";
import { viewCaseSearch } from "./js/casesearch.js";
import { openDocument, viewDocument, viewUpload } from "./js/docs.js";
import { viewDrafting, viewNotice } from "./js/drafting.js";
import { provisionsPanel } from "./js/provisions.js";
import { refreshBell, viewUpdates } from "./js/updates.js";
import { viewAwareness } from "./js/awareness.js";
import { viewSearch } from "./js/search.js";
import { viewJobs } from "./js/jobs.js";
import { viewCalendar } from "./js/calendar.js";
import { catalogueView } from "./js/statelaw.js";
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
    // One chip with the state that matters most; the full status opens on demand.
    const head = s.llm_reachable === false ? llm : s.processing ? proc : !s.models_ready ? models
      : html`<span class="chip chip-ok"><span class="dot" aria-hidden="true"></span> ${t("status.ready")}</span>`;
    mount($("#mastStatus"), html`<div class="small muted">${t("status.system")}</div><div class="row">${head}${idx}${s.llm_reachable === false ? "" : llm}</div>`);
    if (!s.models_ready) setTimeout(refreshStatus, 3000);  // clear the chip promptly once warm-up ends
  } catch { mount($("#mastStatus"), html`<span class="chip chip-err">${t("status.unreachable")}</span>`); }
}

// ── Router ──
const routes = { ask: viewResearch, research: viewResearch, dashboard: viewResearch, library: viewActs, cases: viewCases, acts: viewActs,
  drafts: viewDrafts, limitation: viewLimitation, about: viewAbout, privacy: viewPrivacy, lookup: viewCaseSearch, doc: viewDocument,
  upload: viewUpload, drafting: viewDrafting, notice: viewNotice, updates: viewUpdates, awareness: viewAwareness,
  search: viewSearch, tools: viewTools, account: viewAccount, admin: viewAdmin, jobs: viewJobs, calendar: viewCalendar,
  login: (m, r) => renderLogin(route, r.q), signup: (m, r) => renderSignup(route, r.q), forgot: () => renderForgot(),
  "reset-password": () => renderReset() };
// Pages a signed-out visitor may open (public legal information); everything else asks them to sign in.
const PUBLIC = new Set(["ask", "research", "dashboard", "library", "acts", "awareness", "limitation", "login", "signup", "forgot", "reset-password", "about", "privacy"]);
const ASK_ROUTES = new Set(["ask", "research", "dashboard"]);
const CASE_ROUTES = new Set(["cases", "calendar", "lookup", "upload", "doc"]);
// Library sections and the pages inside each one.
const LIB_ROUTES = { library: "law", acts: "law", search: "law", drafting: "drafting", notice: "drafting", drafts: "drafting",
  tools: "tools", limitation: "tools", jobs: "tools", updates: "updates", awareness: "updates" };
const icon = (id, cls = "") => raw(`<svg class="ic ${cls}"><use href="#i-${id}"/></svg>`);

/** The section header: Cases (All Cases, Calendar, actions) or Library (four cards, one search, sub-pages). */
function renderSecnav(r) {
  const el = $("#secnav");
  if (CASE_ROUTES.has(r.name) && r.name !== "doc" && !(r.name === "cases" && r.arg)) {
    mount(el, html`<div class="sec-head"><h1>${t("nav.cases_short")}</h1>
      <div class="seg">${[["cases", t("cases.tab_all")], ...(can("calendar.use") ? [["calendar", t("cal.title")]] : [])].map(([k, x]) => html`<a href="#/${k}" class="${r.name === k ? "active" : ""}">${x}</a>`)}</div>
      <span class="spacer"></span>
      ${can("casesearch.use") ? html`<a class="btn btn-ghost" href="#/lookup">${t("cases.find")}</a>` : ""}
      <a class="btn btn-ghost" href="#/upload">${t("qa.upload_summarise")}</a>
      ${can("case.create") ? html`<a class="btn btn-saffron" href="#/cases?new=1">${icon("plus", "ic-s")} ${t("qa.new_case")}</a>` : ""}</div>`);
    return;
  }
  const sec = LIB_ROUTES[r.name];
  if (!sec) return;
  const cards = [["law", "book", t("lib.law"), t("lib.law_hint"), "#/acts"],
    ...(can("drafting.use") ? [["drafting", "pen", t("nav.drafting"), t("lib.drafting_hint"), "#/drafting"]] : []),
    ["tools", "tool", t("nav.tools"), t("lib.tools_hint"), "#/tools"],
    ["updates", "news", t("nav.updates"), t("lib.updates_hint"), can("notifications.read") ? "#/updates" : "#/awareness"]];
  const pills = { law: [], drafting: [["drafting", t("nav.drafting")], ["notice", t("lib.notice")], ["drafts", t("nav.templates")]],
    tools: [["tools", t("nav.tools")], ["limitation", t("nav.limitation")], ...(can("account.self") ? [["jobs", t("jobs.title")]] : [])],
    updates: [...(can("notifications.read") ? [["updates", t("nav.updates")]] : []), ["awareness", t("nav.awareness")]] }[sec];
  mount(el, html`<div class="sec-head"><h1>${t("nav.library")}</h1>
      <form class="sec-search" id="libSearch" role="search">${icon("search", "ic-s")}<input type="search" id="gq" placeholder="${t("search.global_ph")}" aria-label="${t("search.global_ph")}"></form></div>
    <div class="lib-cards">${cards.map(([k, ic, title, hint, href]) => html`<a class="lib-card lib-${k} ${k === sec ? "active" : ""}" href="${href}">${icon(ic)}<span><strong>${title}</strong><span>${hint}</span></span></a>`)}</div>
    ${pills.length ? html`<div class="pills">${pills.map(([k, x]) => html`<a href="#/${k}" class="${r.name === k ? "active" : ""}">${x}</a>`)}</div>` : ""}`);
  $("#libSearch").onsubmit = (e) => { e.preventDefault(); const q = $("#gq").value.trim(); if (q) location.hash = `#/search?q=${encodeURIComponent(q)}`; };
}

function parseHash() {
  const h = location.hash.replace(/^#\/?/, "") || "ask";
  const [path, qs] = h.split("?");
  const parts = path.split("/").map(decodeURIComponent);
  return { name: parts[0], arg: parts[1], q: new URLSearchParams(qs || "") };
}
let routeSeq = 0;
async function route() {
  const seq = ++routeSeq;
  clearInterval(state.pollTimer);
  window.onbeforeunload = null;  // the draft editor sets an unsaved-changes guard while it is open
  hideDrawer();
  const r = parseHash();
  const fn = r.name === "cases" && r.arg ? viewCase : routes[r.name] || viewResearch;
  const s = state.session || {};
  document.body.classList.remove("authpage");
  document.body.classList.toggle("full", ASK_ROUTES.has(r.name) || !routes[r.name]);
  mount($("#secnav"), raw(""));
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
  const navName = ASK_ROUTES.has(r.name) ? "ask" : CASE_ROUTES.has(r.name) ? "cases" : LIB_ROUTES[r.name] ? "library" : "";
  renderSecnav(r);
  $$("[data-nav]").forEach((a) => { const on = a.dataset.nav === navName; a.classList.toggle("active", on); if (on) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current"); });
  const main = $("#main");
  // A page that fails after the user has already moved on must not overwrite the page now on screen.
  try { await fn(main, r); } catch (e) { if (!e.handled && seq === routeSeq) mount(main, html`<div class="notice notice-red">${e.message}</div>`); }
  // A new page starts at the top; staying on the same page (e.g. switching chats) keeps the scroll.
  const page = `${r.name}/${r.arg || ""}`;
  if (page !== state.lastPage) { state.lastPage = page; $("#stage").scrollTop = 0; }
  if (!ASK_ROUTES.has(r.name)) main.focus({ preventScroll: true });
}

// ── Signed out / half-finished sign-in (session expired, second factor pending) ──
setUnauthenticatedHandler((s) => {
  applyNavPermissions();
  if (s?.state === "mfa_pending") return renderMfa(route);
  if (s?.state === "mfa_enroll") return renderEnroll(true, route);
  return renderLogin(route);
});

// ── Cases list ──
async function viewCases(main, r) {
  crumbs([[t("nav.cases")]]);
  const [{ cases, types, statuses }, ref] = await Promise.all([api("/api/cases"), reference()]);
  const adding = r.q.get("new") && can("case.create");
  const group = (c) => /Disposed/.test(c.status) ? "closed" : c.is_draft || /Draft/.test(c.status) ? "prefiling" : "active";
  const count = (k) => cases.filter((c) => group(c) === k).length;
  let status = "";
  mount(main, html`
    <div class="card ${adding ? "" : "hidden"}" id="newCaseCard" style="margin:10px 0 16px">
      <div class="card-h"><h2>${t("cases.register")}</h2><span class="small muted">${t("form.required_hint")}</span></div>
      <form class="card-b" id="caseForm" novalidate>${caseFormFields({}, statuses, ref, true)}
        <div class="row" style="margin-top:12px"><button class="btn btn-saffron" type="submit" data-final="1">${t("cases.create")}</button>
          <button class="btn btn-ghost" type="submit" data-final="0">${t("cases.save_draft")}</button>
          <button class="btn btn-ghost" type="button" id="cancelNew">${t("common.cancel")}</button></div></form>
    </div>
    <div class="row" style="margin:6px 0 14px">
      <div class="st-filter" id="stFilter">${[["", t("cases.st_all", { n: cases.length })], ["active", t("cases.st_active", { n: count("active") })], ["prefiling", t("cases.st_prefiling", { n: count("prefiling") })], ["closed", t("cases.st_closed", { n: count("closed") })]].map(([k, x]) =>
        html`<button type="button" data-st="${k}" class="${k ? "" : "active"}">${x}</button>`)}</div>
      <span class="spacer"></span>
      <select id="fType" aria-label="${t("cases.filter_type")}" style="width:auto"><option value="">${t("cases.all_types")}</option>${types.map((x) => html`<option value="${x}">${t(`cat.${x}`, {}, x)}</option>`)}</select>
      <input type="search" id="fSearch" placeholder="${t("cases.search_ph")}" aria-label="${t("cases.search_ph")}" style="width:280px;max-width:100%"></div>
    <div class="card"><div class="table-wrap"><table class="gov"><thead><tr><th>${t("col.matter")}</th><th>${t("col.type")}</th><th>${t("col.court")}</th><th>${t("col.next_hearing")}</th><th>${t("col.docs")}</th><th>${t("col.status")}</th></tr></thead>
      <tbody id="caseRows"></tbody></table></div></div>`);
  const stClass = (s) => /Disposed/.test(s) ? "" : /Evidence/.test(s) ? "st-evidence" : /Draft/.test(s) ? "st-draft" : /Final|Reserved/.test(s) ? "st-late" : "st-active";
  const rows = () => {
    const ty = $("#fType").value, q = $("#fSearch").value.toLowerCase();
    const list = cases.filter((c) => (!status || group(c) === status) && (!ty || c.case_type === ty) && (!q || [c.title, c.case_number, c.cnr_number, c.fir_number, c.client_name, c.court, c.opposite_party].join(" ").toLowerCase().includes(q)));
    mount($("#caseRows"), list.length ? html`${list.map((c) => html`<tr class="clickable" data-href="#/cases/${encodeURIComponent(c.id)}">
      <td><strong>${c.title}</strong> ${c.is_draft ? html`<span class="chip chip-warn">${t("cases.draft_badge")}</span>` : ""}<br><span class="small muted">${[c.client_name, c.opposite_party ? `v. ${c.opposite_party}` : "", c.case_number || c.cnr_number].filter(Boolean).join(" · ")}</span></td>
      <td><span class="badge ${TYPE_BADGE[c.case_type] || ""}">${t(`cat.${c.case_type}`, {}, c.case_type)}</span></td>
      <td class="small">${c.court || "—"}</td><td class="small">${fmtDate(c.next_hearing_date)}</td>
      <td>${c.n_docs ?? "—"}</td><td><span class="st-tag ${stClass(c.status)}">${c.status}</span>${c.access ? "" : html` <span class="chip">${t("members.not_on_case")}</span>`}</td></tr>`)}` : html`<tr><td colspan="6"><div class="empty">${t("cases.none")}</div></td></tr>`);
    bindRowLinks($("#caseRows"));
  };
  rows();
  $("#fType").onchange = rows; $("#fSearch").oninput = rows;
  $$("[data-st]", $("#stFilter")).forEach((b) => { b.onclick = () => { status = b.dataset.st; $$("[data-st]", $("#stFilter")).forEach((x) => x.classList.toggle("active", x === b)); rows(); }; });
  if (adding) $("#f_client_name").focus();
  $("#cancelNew").onclick = () => { location.hash = "#/cases"; };
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
    ${isNew ? html`<div class="span-3"><label class="f" for="f_files">${t("form.supporting_docs")}</label><input type="file" id="f_files" multiple accept="${ACCEPT_UPLOADS}"><div class="hint">${t("form.supporting_docs_hint")}</div></div>` : ""}
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
  const nNotes = (c.notes || []).length;
  const tabs = [["overview", t("case.tab_overview")], ["docs", html`${t("case.tab_documents")}<span class="tab-n" id="docN">${docsRes.documents.length}</span>`],
    ...(may.provisions ? [["provisions", t("case.tab_provisions")]] : []),
    ...(may.notes ? [["notes", html`${t("case.notes")}${nNotes ? html`<span class="tab-n">${nNotes}</span>` : ""}`]] : []),
    ["research", t("case.tab_research")], ...(may.drafts ? [["drafts", t("case.tab_drafts")]] : []), ["records", t("case.tab_records")],
    ["team", t("case.tab_team")]];
  mount(main, html`
    <div class="page-head"><h1 class="page-title">${c.title}</h1><span class="badge ${TYPE_BADGE[c.case_type] || ""}">${t(`cat.${c.case_type}`, {}, c.case_type)}</span>
      ${c.is_draft ? html`<span class="chip chip-warn">${t("cases.draft_badge")}</span>` : ""}
      <span class="spacer"></span>${may.drafts ? html`<a class="btn btn-ghost" href="#/drafting?case=${encodeURIComponent(id)}">${icon("pen", "ic-s")} ${t("qa.draft")}</a>` : ""}
      </div>
    <p class="case-meta">${[c.case_subtype, c.case_number || c.cnr_number || t("case.no_number"), c.court || t("case.no_court"), [c.district, c.state].filter(Boolean).join(", "), c.status].filter(Boolean).map((x) => html`<span>${x}</span>`)}
      ${c.next_hearing_date ? html`<span>${t("form.next_hearing")}: <strong>${fmtDate(c.next_hearing_date)}</strong></span>` : ""}</p>
    ${c.is_draft ? html`<div class="notice" style="margin-bottom:16px">${t("case.draft_notice")}</div>` : ""}
    <form class="case-ask" id="caseAsk">${icon("spark")}<input id="caseAskQ" placeholder="${t("case.ask_ph")}" aria-label="${t("case.ask_ph")}" autocomplete="off">
      <button class="send-btn" type="submit" aria-label="${t("research.send")}">${icon("send")}</button></form>
    <div id="caseTabs">
    ${tabBar("case", tabs, t("case.sections"))}
    ${tabPanel("case", "overview", html`<div class="grid g-side">
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
        <div class="card"><div class="card-h"><h2>${t("case.ask")}</h2></div><div class="card-b quick">
          ${["case.q1", "case.q2", "case.q3", "case.q4"].map((k) =>
            html`<a class="btn btn-ghost" style="justify-content:flex-start;text-align:left" href="#/research?case=${encodeURIComponent(id)}&q=${encodeURIComponent(t(k))}">${t(k)}</a>`)}
        </div></div>
      </div>`)}
    ${tabPanel("case", "docs", html`<div class="card"><div class="card-h"><h2>${t("case.documents")}</h2><span class="small muted">${t("case.doc_formats")}</span></div>
          <div class="card-b">
            ${may["documents.upload"] ? html`<div class="row" style="margin-bottom:10px"><label class="f" for="docType" style="margin:0">${t("case.doc_type")}</label>
              <select id="docType" style="max-width:260px"><option value="auto">${t("case.detect")}</option>${Object.entries(types).filter(([k]) => k !== "template").map(([k, v]) => html`<option value="${k}">${v}</option>`)}</select></div>
            <label class="dropzone" id="drop"><input type="file" id="fileInput" multiple accept="${ACCEPT_UPLOADS}">
              <strong>${t("case.drop")}</strong><br><span class="small muted">${t("case.drop_hint")}</span></label>` : ""}
            <div id="docList" style="margin-top:8px"></div>
          </div></div>`)}
    ${may.provisions ? tabPanel("case", "provisions", html`<div id="provPanel"></div>`) : ""}
    ${may.notes ? tabPanel("case", "notes", html`<div class="card" style="max-width:860px"><div class="card-h"><h2>${t("case.notes")}</h2></div><div class="card-b">
          <form id="noteForm" class="stack"><textarea id="noteText" placeholder="${t("case.note_ph")}" aria-label="${t("case.new_note")}"></textarea>
            <div><button class="btn btn-sm" type="submit">${t("case.add_note")}</button></div></form>
          <div style="margin-top:16px">${nNotes ? c.notes.map((n) => html`<div class="note"><div class="small muted">${n.author} · ${fmtDate(n.created_at)}</div>${n.content}</div>`) : html`<div class="empty">${t("case.no_notes")}</div>`}</div>
        </div></div>`) : ""}
    ${tabPanel("case", "research", html`<div class="card" id="caseResearch">${spinner()}</div>`)}
    ${may.drafts ? tabPanel("case", "drafts", html`<div class="card" id="caseDrafts">${spinner()}</div>`) : ""}
    ${tabPanel("case", "records", html`<div class="card" id="caseRecords">${spinner()}</div>`)}
    ${tabPanel("case", "team", html`<div id="membersPanel" style="max-width:860px"></div>`)}
    </div>`);
  $("#caseAsk").onsubmit = (e) => { e.preventDefault(); const q = $("#caseAskQ").value.trim();
    location.hash = `#/ask?case=${encodeURIComponent(id)}${q ? `&send=${encodeURIComponent(q)}` : ""}`; };
  caseLists(id, may);
  bindTabs($("#caseTabs"), "case", "caseTab", c.is_draft && r.q.get("edit") ? "overview" : r.q.get("tab"));
  if (may.provisions) provisionsPanel($("#provPanel"), c, () => route());
  membersCard($("#membersPanel"), id);

  const renderDocs = (docs) => {
    mount($("#docList"), docs.length ? html`${docs.map((d) => html`<div class="doc-item">
      <span class="doc-ico">${(d.filename.split(".").pop() || "").toUpperCase()}</span>
      <div class="doc-main"><div class="doc-title">${d.title}</div>
        <div class="small muted">${types[d.doc_type] || d.doc_type} · ${d.pages ? `${t("case.pages", { n: d.pages })} · ` : ""}${d.n_chunks ? `${t("case.passages", { n: d.n_chunks })} · ` : ""}${d.meta?.lang_name ? `${d.meta.lang_name} · ` : ""}${d.meta?.ocr ? `OCR · ` : ""}${fmtDate(d.created_at)}${d.meta?.instruction_like_text ? html` <span class="chip chip-warn" title="${t("guard.injection_hint")}">${t("guard.injection")}</span>` : ""}</div>
        ${d.status === "processing" ? html`<div class="status-line small"><span class="spinner"></span> ${t("case.indexing")}</div>` : ""}
        ${["failed", "quarantined", "cancelled"].includes(d.status) ? html`<div class="small" style="color:var(--red)">⚠ <strong>${t(`doc.st_${d.status}`)}</strong> — ${d.error || t("common.failed")}
          ${may["documents.upload"] ? html` <button class="btn btn-ghost btn-sm" data-reprocess="${d.id}">${t("doc.reprocess")}</button>` : ""}</div>` : ""}
        ${d.meta?.quality?.warnings?.length ? html`<div class="small"><span class="chip chip-warn" title="${d.meta.quality.warnings.join(" · ")}">⚠ ${t("doc.quality_issues", { n: d.meta.quality.warnings.length })}</span></div>` : ""}
        <div class="doc-actions">
          ${d.status === "ready" ? html`
            <a class="btn btn-saffron btn-sm" href="#/doc/${encodeURIComponent(d.id)}">${t("case.open_doc")}</a>
            <a class="btn btn-ghost btn-sm" href="#/research?case=${encodeURIComponent(id)}&doc=${encodeURIComponent(d.id)}&task=summary">${t("case.summarise")}</a>
            <a class="btn btn-ghost btn-sm" href="#/research?case=${encodeURIComponent(id)}&doc=${encodeURIComponent(d.id)}&task=timeline">${t("case.timeline")}</a>
            <a class="btn btn-ghost btn-sm" href="/api/documents/${encodeURIComponent(d.id)}/file" target="_blank" rel="noopener">${t("case.original")} ↗</a>` : ""}
          ${may["documents.delete"] ? html`<button class="btn btn-danger btn-sm" data-del="${d.id}">${t("common.delete")}</button>` : ""}</div></div></div>`)}` : html`<div class="empty">${t("case.no_docs")}</div>`);
    $$("[data-reprocess]", $("#docList")).forEach((b) => { b.onclick = async () => {
      try { await api(`/api/documents/${encodeURIComponent(b.dataset.reprocess)}/reprocess`, { method: "POST" }); toast(t("doc.requeued")); loadDocs(); }
      catch (e) { toast(e.message, true); }
    }; });
    $$("[data-del]", $("#docList")).forEach((b) => { b.onclick = async () => {
      if (!confirm(t("case.confirm_delete_doc"))) return;
      try { await api(`/api/documents/${encodeURIComponent(b.dataset.del)}`, { method: "DELETE" }); toast(t("case.doc_deleted")); loadDocs(); } catch (e) { toast(e.message, true); }
    }; });
    clearInterval(state.pollTimer);
    if (docs.some((d) => d.status === "processing")) state.pollTimer = setInterval(loadDocs, 2500);
  };
  const loadDocs = async () => { try { const { documents } = await api(`/api/documents?case_id=${encodeURIComponent(id)}`); renderDocs(documents); $("#docN").textContent = String(documents.length); refreshStatus(); } catch { /* transient */ } };
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

/** The Research, Drafts and Records tabs of a case: what already exists for it, one click from the case. */
async function caseLists(id, may) {
  const list = (el, items, empty, row) => mount(el, items.length ? html`<ul class="rows">${items.map(row)}</ul>` : html`<div class="card-b empty">${empty}</div>`);
  try { const { chats } = await api(`/api/chats?case_id=${encodeURIComponent(id)}`);
    list($("#caseResearch"), chats, t("research.no_chats"), (c) => html`<li><div class="r-main"><a class="r-title" href="#/ask?case=${encodeURIComponent(id)}&chat=${encodeURIComponent(c.id)}">${c.title}</a><div class="r-meta">${fmtDate(c.updated_at)}</div></div></li>`);
  } catch { mount($("#caseResearch"), html`<div class="card-b empty">${t("research.no_chats")}</div>`); }
  if (may.drafts) try { const { drafts } = await api(`/api/drafts?case_id=${encodeURIComponent(id)}`);
    list($("#caseDrafts"), drafts, t("case.no_drafts"), (d) => html`<li><div class="r-main"><a class="r-title" href="#/drafting?draft=${encodeURIComponent(d.id)}">${d.title}</a><div class="r-meta">${fmtDate(d.updated_at)}</div></div></li>`);
  } catch { mount($("#caseDrafts"), html`<div class="card-b empty">${t("case.no_drafts")}</div>`); }
  try { const { records } = await api(`/api/cases/${encodeURIComponent(id)}/records`);
    list($("#caseRecords"), records, t("case.no_records"), (x) => html`<li><div class="r-main"><div class="r-title">${x.title || x.data?.title || x.source}</div><div class="r-meta">${[x.source, fmtDate(x.retrieved_at)].filter(Boolean).join(" · ")}</div></div></li>`);
  } catch { mount($("#caseRecords"), html`<div class="card-b empty">${t("case.no_records")}</div>`); }
}

// ── Ask (AI legal search) ──
function askHero(name) {
  const h = new Date().getHours();
  const hi = t(h < 12 ? "ask.morning" : h < 17 ? "ask.afternoon" : "ask.evening");
  return html`<div class="ask-hero">${icon("scale")}<h1>${name ? `${hi}, ${name}` : hi}</h1><p>${t("ask.lead")}</p></div>`;
}
/** Signed out (or no role yet): the same first screen; the question waits while the person signs in. */
function viewAskGuest(main) {
  crumbs([[t("nav.ask")]]);
  const signedIn = state.session?.authenticated;
  mount(main, html`<div class="ask ask-empty" id="askRoot"><div class="ask-top"><span class="spacer"></span>${signedIn ? "" : html`<a class="btn btn-ghost btn-sm" href="#/login">${t("login.title")}</a>`}</div>
    <div class="ask-main"><div class="ask-col">${askHero()}
      <form class="composer" id="composer"><div class="composer-box"><textarea id="q" rows="1" placeholder="${t("ask.placeholder")}" aria-label="${t("research.your_question")}"></textarea>
        <div class="composer-bar"><button class="send-btn" type="submit" aria-label="${t("research.send")}">${icon("send")}</button></div></div></form>
      <div class="ask-after"><div class="ask-chips">
        <a class="ask-chip" href="#/acts">${icon("book")}${t("nav.acts")}</a><a class="ask-chip" href="#/limitation">${icon("cal")}${t("nav.limitation")}</a><a class="ask-chip" href="#/awareness">${icon("news")}${t("nav.awareness")}</a></div>
        <div class="small muted">${t("ask.note")}</div></div></div></div></div>`);
  const qEl = $("#q");
  qEl.addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); $("#composer").requestSubmit(); } });
  $("#composer").onsubmit = (e) => {
    e.preventDefault(); const x = qEl.value.trim(); if (!x) return;
    if (signedIn) { toast(t("auth.no_access_title"), true); return; }
    store.set("pendingQ", x); location.hash = "#/login";
  };
  qEl.focus();
}

async function viewResearch(main, r) {
  if (!can("research.use")) return viewAskGuest(main);
  const qCase = r.q.get("case");
  if (qCase !== null) { state.caseId = qCase || null; store.set("caseId", state.caseId); }
  state.chatId = r.q.get("chat") || null;
  const [{ cases }, ref] = await Promise.all([api("/api/cases"), reference()]);
  if (state.caseId && !cases.some((c) => c.id === state.caseId)) state.caseId = null;
  const cur = cases.find((c) => c.id === state.caseId);
  // Jurisdiction: "" = the matter's state (or all of India without one); "all" = every state; or one state / UT.
  state.jur = store.get("rs.jur", "");
  if (state.jur && state.jur !== "all" && !ref.states.includes(state.jur)) state.jur = "";
  const jurLabel = () => state.jur === "all" ? t("research.jur_all_short") : state.jur ? t("research.jur_state_short", { state: state.jur })
    : cur?.state ? t("research.jur_state_short", { state: cur.state }) : t("research.jur_all_short");
  if (!cur && state.scope === "case") state.scope = "all";
  crumbs([[t("nav.ask")], ...(cur ? [[cur.title, `#/cases/${encodeURIComponent(cur.id)}`]] : [])]);
  const scopeLabel = () => t({ all: "research.scope_all", case: "research.scope_case", library: "research.scope_library" }[state.scope] || "research.scope_all");
  const ctx = () => html`${cur ? html`<strong>${cur.title}</strong>` : t("research.general")} · ${scopeLabel()} · ${jurLabel()}`;
  const name = (state.session?.user?.display_name || "").trim().split(/\s+/)[0];
  const suggestions = cur ? [t("research.s_case1"), t("research.s_case2"), t("research.s_case3"), cur.case_type === "Criminal" ? t("research.s_case4_crim") : t("research.s_case4")] : [];
  mount(main, html`
    <div class="ask ask-empty" id="askRoot">
      <div class="ask-top">
        <button type="button" class="btn btn-ghost btn-sm" id="tgHist" aria-controls="rsHist" aria-pressed="false">${icon("hist", "ic-s")} ${t("research.history")}</button>
        <span class="rs-ctx" id="rsCtx" title="${cur ? cur.title : t("research.general")}">${ctx()}</span>
        <span class="spacer"></span>
        <button type="button" class="btn btn-ghost btn-sm" id="newChat">${icon("plus", "ic-s")} ${t("research.new_chat")}</button>
        <button type="button" class="btn btn-ghost btn-sm" id="tgRight" aria-controls="rsRight" aria-pressed="false">${t("research.sources")} <span class="tab-n" id="srcN">0</span></button>
      </div>
      <aside class="rs-hist-panel" id="rsHist" aria-label="${t("research.history")}">
        <div class="rs-panel-h"><h2>${t("research.history")}</h2><button type="button" class="btn btn-ghost btn-sm" data-close="hist" aria-label="${t("research.close_panel")}">✕</button></div>
        <div class="rs-panel-b" id="histBody">
          ${tabBar("hist", [["chats", t("research.conversations")], ["searches", t("research.searches")]], t("research.history"))}
          ${tabPanel("hist", "chats", html`<ul class="chat-list hist-list" id="histChats"></ul>`)}
          ${tabPanel("hist", "searches", html`<ul class="chat-list hist-list" id="histSearches"></ul><div id="histClear" style="margin-top:10px"></div>`)}
        </div>
      </aside>
      <div class="ask-main">
        <section class="ask-col" aria-label="${t("research.conversation")}">
          ${askHero(name)}
          <div class="thread" id="thread" aria-live="polite"></div>
          <form class="composer" id="composer"><div class="composer-box">
            <textarea id="q" rows="1" placeholder="${t("ask.placeholder")}" aria-label="${t("research.your_question")}"></textarea>
            <div class="composer-bar">
              <a class="icon-btn" href="#/upload${cur ? `?case=${encodeURIComponent(cur.id)}` : ""}" aria-label="${t("qa.upload_summarise")}" title="${t("qa.upload_summarise")}">${icon("clip", "ic-s")}</a>
              <select class="pick" id="caseSel" aria-label="${t("research.matter")}"><option value="">${t("ask.no_case")}</option>${cases.map((c) => html`<option value="${c.id}" ${c.id === state.caseId ? raw("selected") : ""}>${c.title}</option>`)}</select>
              <select class="pick" id="scopeSel" aria-label="${t("research.scope")}">${[["all", "research.scope_all"], ["case", "research.scope_case"], ["library", "research.scope_library"]].map(([v, k]) =>
                html`<option value="${v}" ${state.scope === v ? raw("selected") : ""} ${v === "case" && !cur ? raw("disabled") : ""}>${t(k)}</option>`)}</select>
              <select class="pick" id="jurSel" aria-label="${t("research.jurisdiction")}" title="${t("research.jur_hint")}"><option value="">${cur?.state ? t("research.jur_auto_case", { state: cur.state }) : t("research.jur_auto_none")}</option>
                <option value="all" ${state.jur === "all" ? raw("selected") : ""}>${t("research.jur_all")}</option>
                <optgroup label="${t("research.jur_states")}">${ref.states.map((s) => html`<option value="${s}" ${s === state.jur ? raw("selected") : ""}>${t("research.jur_state_opt", { state: s })}</option>`)}</optgroup></select>
              ${cur ? html`<select class="pick" id="focusDoc" aria-label="${t("research.focus_doc")}"><option value="">${t("research.all_docs")}</option></select>` : ""}
              <select class="pick" id="askLang" aria-label="${t("profile.language")}">${LANGS.filter((l) => l.toggle).map((l) => html`<option value="${l.code}" ${l.code === lang() ? raw("selected") : ""}>${l.label}</option>`)}</select>
              <span id="voiceSlot"></span>
              <button class="send-btn" type="submit" id="sendBtn" aria-label="${t("research.send")}" title="${t("research.enter_hint")}">${icon("send")}</button>
            </div>
            <div id="voiceStatus" class="small" aria-live="polite"></div>
          </div></form>
          <div class="ask-after">
            <div class="ask-chips">${cur ? suggestions.map((x) => html`<button type="button" class="ask-chip" data-s="${x}">${x}</button>`) : html`
              <button type="button" class="ask-chip" data-focus="1">${icon("scale")}${t("ask.chip_law")}</button>
              <a class="ask-chip" href="#/limitation">${icon("cal")}${t("ask.chip_deadline")}</a>
              <a class="ask-chip" href="#/upload">${icon("doc")}${t("ask.chip_analyse")}</a>
              ${can("drafting.use") ? html`<a class="ask-chip" href="#/notice">${icon("pen")}${t("ask.chip_notice")}</a>` : ""}`}</div>
            <div class="ask-next" id="askNext"></div>
            <div class="small muted">${t("ask.note")}</div>
          </div>
        </section>
        <aside id="rsRight" aria-label="${t("research.sources")}">
          <div class="rs-panel-h"><h2>${t("research.sources")}</h2><span class="row"><span class="small muted" id="srcCount"></span>
            <button type="button" class="btn btn-ghost btn-sm" data-close="right" aria-label="${t("research.close_panel")}">✕</button></span></div>
          <div class="rs-panel-b" id="sourcesPanel"><div class="empty">${t("research.sources_empty")}</div></div></aside>
      </div>
    </div>`);

  const grid = $("#askRoot");
  let showRight = !matchMedia("(max-width: 1100px)").matches && store.get("rs.right", false), showHist = false;   // History always starts hidden
  const apply = () => {
    grid.classList.toggle("show-right", showRight); grid.classList.toggle("show-hist", showHist);
    $("#tgRight").setAttribute("aria-pressed", String(showRight)); $("#tgHist").setAttribute("aria-pressed", String(showHist));
  };
  const toggle = (side, on) => {
    if (side === "hist") { showHist = on ?? !showHist; apply(); if (showHist) loadHistory(); return; }
    showRight = on ?? !showRight; store.set("rs.right", showRight); apply();
  };
  $("#tgRight").onclick = () => toggle("right");
  $("#tgHist").onclick = () => toggle("hist");
  $$("[data-close]", grid).forEach((b) => { b.onclick = () => { toggle(b.dataset.close, false); $(b.dataset.close === "hist" ? "#tgHist" : "#tgRight").focus(); }; });
  grid.addEventListener("keydown", (e) => { if (e.key === "Escape" && showHist) { showHist = false; apply(); $("#tgHist").focus(); } });
  bindTabs($("#histBody"), "hist", "histTab");
  apply();

  $("#caseSel").onchange = (e) => { location.hash = `#/ask?case=${encodeURIComponent(e.target.value)}`; };
  $("#scopeSel").onchange = (e) => { state.scope = e.target.value; store.set("scope", state.scope); mount($("#rsCtx"), ctx()); };
  $("#askLang").onchange = (e) => switchLang(e.target.value);
  $("#jurSel").onchange = (e) => { state.jur = e.target.value; store.set("rs.jur", state.jur); mount($("#rsCtx"), ctx()); };
  $("#newChat").onclick = () => {
    const h = `#/ask?case=${encodeURIComponent(state.caseId || "")}`;
    if (location.hash === h) route(); else location.hash = h;
  };
  const qEl = $("#q");
  const grow = () => { qEl.style.height = "auto"; qEl.style.height = `${qEl.scrollHeight}px`; };
  qEl.addEventListener("input", grow);
  qEl.addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); $("#composer").requestSubmit(); } });
  $("#composer").onsubmit = (e) => {
    e.preventDefault(); const x = qEl.value.trim(); if (!x || state.streaming) return;
    const mode = qEl.dataset.inputMode || "text"; qEl.value = ""; qEl.dataset.inputMode = ""; grow();
    const focus = $("#focusDoc")?.value;
    send(x, { input_mode: mode, ...(focus ? { doc_id: focus, task: "custom" } : {}) });
  };
  voiceControls($("#voiceSlot"), qEl, $("#voiceStatus"));
  $("#thread").addEventListener("click", onCiteClick);
  $("#thread").addEventListener("click", onAnswerAction);
  bindSpeak($("#thread"));
  $("#sourcesPanel").addEventListener("click", (e) => { const card = e.target.closest("[data-src]"); if (card) openSource(JSON.parse(card.dataset.src)); });

  if (cur) loadCaseDocs(cur.id);
  if (state.chatId) await loadChat(state.chatId);
  else {
    $$("[data-s]", grid).forEach((b) => { b.onclick = () => { if (!state.streaming) send(b.dataset.s); }; });
    $("[data-focus]", grid)?.addEventListener("click", () => qEl.focus());
    nextHearing();
  }

  const doc = r.q.get("doc"), task = r.q.get("task"), pending = store.get("pendingQ", null), pre = r.q.get("q") || pending, go = r.q.get("send");
  if (pending) store.set("pendingQ", null);
  history.replaceState(null, "", `#/ask?case=${encodeURIComponent(state.caseId || "")}${state.chatId ? `&chat=${encodeURIComponent(state.chatId)}` : ""}`);
  if (doc && task) {
    let title = t("research.this_document"); try { title = `“${(await api(`/api/documents/${encodeURIComponent(doc)}`)).title}”`; } catch { /* keep generic */ }
    send(t(`research.task_${task}`, { title }, `${t("research.task_custom", { title })}`), { doc_id: doc, task });
  } else if (go) send(go);
  else if (pre) { qEl.value = pre; grow(); qEl.focus(); }
  else qEl.focus();
}

/** One quiet line under the chatbox; nothing is shown when no hearing is coming up. */
async function nextHearing() {
  if (!can("calendar.use")) return;
  try {
    const tz = Intl.DateTimeFormat().resolvedOptions().timeZone;
    const { items } = await api(`/api/calendar/upcoming?tz=${encodeURIComponent(tz)}&days=45&limit=1`);
    const e = items[0]; const el = $("#askNext");
    if (e && el) mount(el, html`${icon("cal")}<span>${t("ask.next")} <a href="${e.source === "case" ? `#/cases/${encodeURIComponent(e.case_id)}` : `#/calendar?event=${encodeURIComponent(e.id)}`}">${e.case_title || e.title}</a>, ${fmtDate(e.event_date)}</span>`);
  } catch { /* the line is optional */ }
}

async function loadCaseDocs(caseId) {
  try {
    const { documents } = await api(`/api/documents?case_id=${encodeURIComponent(caseId)}`);
    const sel = $("#focusDoc");
    if (!documents.length) { const n = $("#askNext"); if (n) mount(n, html`<span>${t("research.no_docs")} <a href="#/upload?case=${encodeURIComponent(caseId)}">${t("research.upload_link")}</a>.</span>`); }
    if (sel) documents.filter((d) => d.status === "ready").forEach((d) => { const o = document.createElement("option"); o.value = d.id; o.textContent = d.title; sel.appendChild(o); });
  } catch { /* ignore */ }
}

/** Recent History: the person's own conversations (every matter) and searches; reopened with one click. */
async function loadHistory() {
  if (!$("#histChats")) return;
  let data;
  try { data = await api("/api/history?limit=40"); }
  catch (e) { mount($("#histChats"), html`<li class="notice notice-red">${e.message}</li>`); return; }
  const { chats, searches } = data;
  mount($("#histChats"), chats.length ? html`${chats.map((c) => html`<li><a href="#/ask?case=${encodeURIComponent(c.case_id || "")}&chat=${encodeURIComponent(c.id)}" class="${c.id === state.chatId ? "active" : ""}" title="${c.title}">
      <span class="hist-t">${c.title}</span><span class="hist-sub">${c.case_title || t("research.general")} · ${fmtDate(c.updated_at)}</span></a>
    <button type="button" data-delchat="${c.id}" aria-label="${t("research.delete_chat")}" title="${t("common.delete")}">✕</button></li>`)}` : html`<li class="empty small">${t("research.no_chats")}</li>`);
  const searchHref = (s) => { const p = new URLSearchParams({ q: s.query, ...(s.filters || {}) }); return `#/search?${p}`; };
  mount($("#histSearches"), searches.length ? html`${searches.map((s) => html`<li><a href="${searchHref(s)}" title="${s.query}">
      <span class="hist-t">🔎 ${s.query}</span><span class="hist-sub">${fmtDate(s.created_at)}${s.results !== null && s.results !== undefined ? ` · ${t("research.n_results", { n: s.results })}` : ""}${Object.keys(s.filters || {}).length ? ` · ${t("research.filtered")}` : ""}</span></a>
    <button type="button" data-delsearch="${s.id}" aria-label="${t("research.delete_search")}" title="${t("common.delete")}">✕</button></li>`)}` : html`<li class="empty small">${t("research.no_searches")}</li>`);
  mount($("#histClear"), searches.length ? html`<button type="button" class="btn btn-ghost btn-sm" id="clearSearches">${t("research.clear_searches")}</button>` : "");
  $$("[data-delchat]", $("#histChats")).forEach((b) => { b.onclick = async () => {
    if (!confirm(t("research.confirm_delete_chat"))) return;
    try { await api(`/api/chats/${encodeURIComponent(b.dataset.delchat)}`, { method: "DELETE" }); } catch (e) { toast(e.message, true); return; }
    if (b.dataset.delchat === state.chatId) location.hash = `#/ask?case=${encodeURIComponent(state.caseId || "")}`; else loadHistory();
  }; });
  $$("[data-delsearch]", $("#histSearches")).forEach((b) => { b.onclick = async () => {
    try { await api(`/api/history/searches/${encodeURIComponent(b.dataset.delsearch)}`, { method: "DELETE" }); loadHistory(); } catch (e) { toast(e.message, true); }
  }; });
  $("#clearSearches")?.addEventListener("click", async () => {
    if (!confirm(t("research.confirm_clear_searches"))) return;
    try { await api("/api/history/searches", { method: "DELETE" }); toast(t("research.searches_cleared")); loadHistory(); } catch (e) { toast(e.message, true); }
  });
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
  $("#askRoot")?.classList.remove("ask-empty");
  const el = document.createElement("div"); el.className = "msg msg-user"; el.textContent = text;
  $("#thread").appendChild(el); return el;
}
function addAiMsg() {
  const el = document.createElement("div"); el.className = "msg msg-ai"; el.dataset.mid = String(++state.msgSeq);
  $("#thread").appendChild(el); return el;
}
const INTENT = () => ({ research: t("intent.research"), drafting: t("intent.drafting"), analyze_document: t("intent.analyze_document"), calculation: t("intent.calculation"), chitchat: t("intent.chitchat"),
  news: t("intent.news"), time: t("intent.time"), out_of_scope: t("intent.out_of_scope"), clarify: t("intent.clarify") });
// Intents whose reply is not legal research (no jurisdiction chip, no "not legal advice" line).
const NON_RESEARCH = ["chitchat", "blocked", "calculation", "time", "out_of_scope", "clarify"];
// Where the answer came from: case files and the library are local; web/news sources come from a live search.
const LIVE = ["web", "news"];
const ORIGIN = (sources) => {
  const kinds = new Set((sources || []).map((s) => s.collection));
  if (!kinds.size) return "";
  return [kinds.has("case") && t("origin.case_files"), [...kinds].some((k) => k !== "case" && !LIVE.includes(k)) && t("origin.library"),
    LIVE.some((k) => kinds.has(k)) && t("origin.web")].filter(Boolean).join(" + ");
};

function renderAi(el, content, sources, meta, live = null) {
  state.msgSources.set(el.dataset.mid, sources || []);
  const v = meta.verify;
  const origin = ORIGIN(sources);
  const metaRow = html`<div class="msg-meta">
    ${meta.intent ? html`<span class="chip">${INTENT()[meta.intent] || meta.intent}</span>` : ""}
    ${meta.jurisdiction && meta.jurisdiction !== "all" && meta.intent && ![...NON_RESEARCH, "news"].includes(meta.intent) ? html`<span class="chip" title="${t("research.jur_hint")}">${t("research.jur_state_short", { state: meta.jurisdiction })}</span>` : ""}
    ${origin ? html`<span class="chip" title="${t((sources || []).some((s) => LIVE.includes(s.collection)) ? "origin.hint_web" : "origin.hint")}">${t("origin.answered_from", { where: origin })}</span>` : ""}
    ${meta.clock ? html`<span class="chip ${meta.clock.verified ? "chip-ok" : "chip-warn"}" title="${meta.clock.source}">${t(meta.clock.verified ? "clock.verified" : "clock.unverified")}</span>` : ""}
    ${sources && sources.length ? html`<span class="chip">${t("research.n_sources", { n: sources.length })}</span>` : ""}
    ${meta.fallback_used ? html`<span class="chip chip-warn" title="${t("trace.fallback_hint")}">${t("trace.fallback")}</span>` : ""}
    ${meta.partial ? html`<span class="chip chip-err">${t("trace.partial")}</span>` : ""}
    ${v && v.checked === false ? html`<span class="chip chip-warn" title="${t("trace.audit_code_only_hint")}">${t("trace.audit_code_only")}</span>` : ""}
    ${meta.elapsed ? html`<span>${meta.elapsed}s</span>` : ""}
    ${!live && content ? speakButton(meta.lang) : ""}</div>`;
  const trace = !live && (meta.trace || []).length ? html`<details class="small trace"><summary>${t("trace.title")}</summary><ol>
    ${meta.trace.map((s) => html`<li><strong>${t(`trace.step_${s.step}`, {}, s.step)}</strong> — ${t(`trace.st_${s.status}`, {}, s.status)}${s.model ? ` · ${s.model}` : ""}${s.fallback ? ` (${t("trace.fallback")})` : ""}${s.passages !== undefined ? ` · ${t("trace.passages", { n: s.passages })}` : ""}${s.jurisdiction && s.jurisdiction !== "all" ? ` · ${s.jurisdiction}` : ""}${s.issues ? ` · ${t("trace.issues", { n: s.issues })}` : ""}${s.requested ? ` · ${s.requested} → ${s.used}` : ""}</li>`)}</ol></details>` : "";
  const status = live ? html`<div class="status-line"><span class="spinner"></span><span>${live}</span></div>` : "";
  const verify = v ? (v.grounded
    ? html`<div class="verify verify-ok">✓ ${t("research.audit_ok")}</div>`
    : html`<div class="verify verify-warn">⚠ ${t("research.audit_warn")}${v.unsupported && v.unsupported.length ? html`<ul>${v.unsupported.map((u) => html`<li>${u}</li>`)}</ul>` : ""}</div>`) : "";
  const err = meta.error && meta.error !== "internal" && !content.includes(meta.error) ? html`<div class="notice notice-red">${meta.error}</div>` : "";
  const disclaimer = !live && meta.intent && !NON_RESEARCH.includes(meta.intent) ? html`<div class="small muted">${t("research.disclaimer")}</div>` : "";
  const research = !live && content && meta.intent && !NON_RESEARCH.includes(meta.intent);
  const actions = research ? html`<div class="msg-actions">
      <button type="button" class="btn btn-ghost btn-sm" data-act="save">${icon("folder", "ic-s")} ${t("ask.save_case")}</button>
      <button type="button" class="btn btn-ghost btn-sm" data-act="draft">${icon("pen", "ic-s")} ${t("ask.draft_from")}</button>
      <button type="button" class="btn btn-ghost btn-sm" data-act="copy">${icon("copy", "ic-s")} ${t("ask.copy")}</button></div>
    <div class="msg-follow">${["ask.f1", "ask.f2", "ask.f3"].map((k) => html`<button type="button" data-follow="${t(k)}">${t(k)}</button>`)}</div>` : "";
  el.dataset.text = content;
  mount(el, html`${metaRow}<div class="md">${markdown(content)}</div>${status}<div class="msg-foot">${err}${verify}${disclaimer}${trace}</div>${actions}`);
}

/** Save to case, Draft from this, Copy, and the follow-up suggestions under an answer. */
async function onAnswerAction(e) {
  const follow = e.target.closest("[data-follow]");
  if (follow) { if (!state.streaming) send(follow.dataset.follow); return; }
  const b = e.target.closest("[data-act]"); if (!b) return;
  const text = b.closest(".msg-ai").dataset.text || "";
  if (b.dataset.act === "copy") {
    try { await navigator.clipboard.writeText(text); toast(t("ask.copied")); } catch { toast(t("ask.copy_failed"), true); }
  } else if (b.dataset.act === "draft") {
    store.set("draftSeed", text.slice(0, 4000));
    location.hash = `#/drafting?case=${encodeURIComponent(state.caseId || "")}`;
  } else if (!state.caseId) { toast(t("ask.pick_case"), true); $("#caseSel").focus(); }
  else {
    try { await api(`/api/cases/${encodeURIComponent(state.caseId)}/notes`, { method: "POST", body: { content: text.slice(0, 8000) } }); toast(t("ask.saved_case")); }
    catch (err) { toast(err.message, true); }
  }
}

function renderSourcesPanel(sources) {
  $("#srcCount").textContent = sources.length ? t("research.n_used", { n: sources.length }) : "";
  $("#srcN").textContent = String(sources.length);
  const L = COLL_LABEL();
  mount($("#sourcesPanel"), sources.length ? html`${sources.map((s) => html`<div class="src-card" role="button" tabindex="0" data-src="${JSON.stringify(s)}">
    <div><span class="src-n">${s.n}</span><span class="badge b-${s.collection}">${L[s.collection] || s.kind}</span>
      ${s.jurisdiction ? html` <span class="chip ${s.jurisdiction === "Central" ? "" : "chip-wait"}" title="${t("research.jur_chip_hint")}">${s.jurisdiction === "Central" ? t("research.central_law") : s.jurisdiction}</span>` : ""} ${s.exact ? html`<span class="chip chip-ok" title="${t("research.exact_hint")}">${t("research.exact")}</span>` : ""}${s.injection_suspected ? html` <span class="chip chip-warn" title="${t("guard.injection_hint")}">${t("guard.injection")}</span>` : ""}${s.catalogue ? html` <span class="chip ${s.full_text === "ready" ? "chip-ok" : "chip-wait"}" title="${t("sc.hint")}">${t(s.full_text === "ready" ? "sc.full_text" : "sc.headnote_only")}</span>` : ""}</div>
    <div class="src-title" style="margin-top:4px">${s.title}</div>
    <div class="small muted">${LIVE.includes(s.collection)
      ? [s.domain, s.reliability, s.published ? t("research.published", { date: s.published }) : t("research.date_unknown")].filter(Boolean).join(" · ")
      : [s.label, s.page && !/^pp?\./.test(s.label || "") ? `p. ${s.page}` : ""].filter(Boolean).join(" · ")}</div>
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
  if (s.catalogue) {   // a Supreme Court catalogue entry: headnote here, full text on request
    const safe = /^https:\/\//.test(s.url || "") ? s.url : "";
    mount(body, html`<h3 style="margin-top:0;color:var(--navy)">${s.title}</h3>
      <p class="small muted">${[s.label, t("sc.court")].filter(Boolean).join(" · ")}</p>
      <div id="passage">${spinner(t("research.loading_passage"))}</div>
      <p class="small muted">${t("sc.attribution")}</p>
      <div class="row" style="margin-top:12px">
        ${safe ? html`<a class="btn" href="${safe}" target="_blank" rel="noopener noreferrer">${t("sc.open_pdf")} ↗</a>` : ""}
        ${s.full_text === "ready" ? html`<span class="chip chip-ok">${t("sc.full_text")}</span>` : html`<button type="button" class="btn btn-primary" id="scFetch">${t("sc.load_full")}</button>`}
      </div>`);
    showDrawer(`${t("research.source")} [${s.n ?? "•"}] — ${t("sc.summary")}`);
    const btn = $("#scFetch");
    if (btn) btn.onclick = async () => {
      btn.disabled = true;
      try { const r = await api("/api/judgments/fetch", { method: "POST", body: { path: s.dataset_path } });
        toast(t(r.status === "ready" ? "sc.ready" : "sc.queued")); }
      catch (e) { btn.disabled = false; toast(e.message, true); }
    };
    try { const c = await api(`/api/chunks/${encodeURIComponent(s.chunk_id)}`);
      mount($("#passage"), html`<pre class="passage">${c.text}</pre>`); }
    catch (e) { mount($("#passage"), html`<div class="notice notice-red">${e.message}</div>`); }
    return;
  }
  if (LIVE.includes(s.collection)) {   // a live web/news source: show the extract and link to the publisher
    const safe = /^https?:\/\//.test(s.url || "") ? s.url : "";
    mount(body, html`<h3 style="margin-top:0;color:var(--navy)">${s.title}</h3>
      <p class="small muted">${[s.domain, s.reliability, s.published ? t("research.published", { date: s.published }) : t("research.date_unknown")].filter(Boolean).join(" · ")}</p>
      <pre class="passage">${s.snippet}</pre>
      ${safe ? html`<div class="row" style="margin-top:12px"><a class="btn" href="${safe}" target="_blank" rel="noopener noreferrer">${t("research.open_web")} ↗</a></div>` : ""}`);
    showDrawer(`${t("research.source")} [${s.n ?? "•"}] — ${L[s.collection] || s.kind || ""}`);
    return;
  }
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
      body: JSON.stringify({ message: text, chat_id: state.chatId, case_id: state.caseId, scope: state.scope, lang: lang(),
        ...(state.jur ? { jurisdiction: state.jur } : {}), ...extra }) });
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
          case "chat": if (state.chatId !== evt.chat_id) { state.chatId = evt.chat_id; history.replaceState(null, "", `#/ask?case=${encodeURIComponent(state.caseId || "")}&chat=${encodeURIComponent(evt.chat_id)}`); } break;
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
    if ($("#askRoot")?.classList.contains("show-hist")) loadHistory();
    refreshStatus();
  }
}

// ── Bare acts + precedents ──
async function viewActs(main, r) {
  crumbs([[t("nav.acts")]]);
  if (r.q.get("read")) return viewActReader(main, r.q.get("read"), r.q.get("section"));
  const lib = await api("/api/library");
  const acts = lib.bare_act;
  const tab = ["judgments", "catalogue"].includes(r.q.get("tab")) ? r.q.get("tab") : "acts";
  const tabs = html`<div class="tabs" role="tablist">${[["acts", t("acts.tab_acts"), "#/acts"], ["catalogue", t("acts.tab_catalogue"), "#/acts?tab=catalogue"],
    ["judgments", t("acts.tab_judgments"), "#/acts?tab=judgments"]].map(([k, label, href]) =>
    html`<a role="tab" class="tab ${tab === k ? "active" : ""}" aria-selected="${String(tab === k)}" href="${href}">${label}</a>`)}</div>`;
  if (tab === "catalogue") {   // the official India Code record for Central law and every state / UT
    mount(main, html`<h1 class="page-title">${t("acts.title")}</h1><p class="page-lead">${t("acts.lead")}</p>${tabs}<div id="catBox"></div>`);
    return catalogueView($("#catBox"), r);
  }
  const jurs = [...new Set(acts.map((a) => a.jurisdiction || "Central"))].sort((a, b) => (a === "Central" ? -1 : b === "Central" ? 1 : a.localeCompare(b)));
  mount(main, html`
    <h1 class="page-title">${t("acts.title")}</h1>
    <p class="page-lead">${t("acts.lead")}</p>
    ${tabs}
    ${tab === "acts" ? html`
    <div class="card"><div class="card-h"><h2>${t("acts.lookup")}</h2></div>
      <form class="card-b row" id="lookupForm" style="align-items:flex-end">
        <div style="flex:2;min-width:240px"><label class="f" for="lq">${t("acts.lookup_label")}</label><input type="search" id="lq" placeholder="${t("acts.lookup_ph")}" value="${r.q.get("q") || ""}"></div>
        <div style="flex:1;min-width:200px"><label class="f" for="lact">${t("acts.act")}</label><select id="lact"><option value="">${t("acts.all_acts")}</option>${acts.map((a) => html`<option value="${a.title}" ${a.title === r.q.get("act") ? raw("selected") : ""}>${a.title}</option>`)}</select></div>
        <button class="btn" type="submit">${t("common.search")}</button></form>
      <div id="lookupResults"></div></div>
    <div class="card" style="margin-top:20px"><div class="card-h"><h2>${t("acts.in_library", { n: acts.length })}</h2>
      <span class="row">${jurs.length > 1 ? html`<select id="actJur" aria-label="${t("acts.jurisdiction")}" style="width:auto"><option value="">${t("acts.all_jurisdictions")}</option>
        ${jurs.map((j) => html`<option value="${j}">${j === "Central" ? t("acts.central_acts") : j}</option>`)}</select>` : ""}
      ${acts.length > 6 ? html`<input type="search" id="actFilter" placeholder="${t("acts.filter_ph")}" aria-label="${t("acts.filter_ph")}" style="max-width:240px">` : ""}</span></div>
    ${acts.length ? html`<ul class="rows" id="actRows">${acts.map((a) => html`<li data-name="${`${a.title} ${a.meta.short || ""}`.toLowerCase()}" data-jur="${a.jurisdiction || "Central"}"><div class="r-main">
      <div class="r-title">${a.title} ${a.meta.short ? html`<span class="chip">${a.meta.short}</span>` : ""}
        <span class="chip ${(a.jurisdiction || "Central") === "Central" ? "" : "chip-wait"}">${(a.jurisdiction || "Central") === "Central" ? t("research.central_law") : a.jurisdiction}</span></div>
      <div class="r-meta">${(a.meta.areas || []).map((x) => html`<span class="badge ${TYPE_BADGE[x] || ""}">${t(`cat.${x}`, {}, x)}</span> `)}
        ${t("acts.meta", { pages: a.pages || "?", n: a.n_chunks || 0 })}${a.meta.act_number ? ` · Act ${a.meta.act_number} of ${a.meta.year}` : ""}${a.meta.fetched_at ? ` · ${t("acts.fetched", { date: fmtDate(a.meta.fetched_at) })}` : ""}
        ${a.meta.repealed ? html` <span class="chip chip-warn" title="${a.meta.note || ""}">${t("acts.repealed")}</span>` : ""}
        ${(a.meta.version || 1) > 1 ? html` <button type="button" class="chip chip-ok" data-versions="${a.id}" title="${t("acts.version_hint")}">${t("acts.version", { v: a.meta.version, date: fmtDate(a.meta.version_date) })}</button>` : ""}
        ${a.status !== "ready" ? html` <span class="chip chip-wait">${a.status}</span>` : ""}</div></div>
      <div class="r-act"><a class="btn btn-ghost btn-sm" href="#/acts?read=${encodeURIComponent(a.id)}">${t("acts.read")}</a>
        ${can("research.use") ? html`<a class="btn btn-ghost btn-sm" href="#/ask?case=&q=${encodeURIComponent(`Under the ${a.title}, `)}">${t("acts.ask")}</a>` : ""}
        <details class="menu"><summary class="btn btn-ghost btn-sm">${t("common.open")}</summary><div class="menu-list">
          <a href="/api/documents/${encodeURIComponent(a.id)}/file" target="_blank" rel="noopener">${t("acts.open_pdf")} ↗</a>
          ${a.meta.item_url ? html`<a href="${a.meta.item_url}" target="_blank" rel="noopener noreferrer">India Code ↗</a>` : ""}</div></details></div></li>`)}</ul>`
      : html`<div class="card-b"><div class="notice">${t("acts.empty")}</div></div>`}
    <div class="card-b small muted">${t("acts.more_states")} <a href="#/acts?tab=catalogue">${t("acts.tab_catalogue")} →</a></div></div>` : html`
    <div class="card"><div class="card-h"><h2>${t("acts.precedents")}</h2></div>
      <form class="card-b row" id="jForm" style="align-items:flex-end">
        <div style="flex:2;min-width:240px"><label class="f" for="jq">${t("acts.precedent_label")}</label><input type="search" id="jq" placeholder="${t("acts.precedent_ph")}" value="${r.q.get("q") || ""}"></div>
        <button class="btn" type="submit">${t("common.search")}</button></form>
      <div id="jResults"></div>
      <div class="card-b small muted">${t("acts.precedent_note", { n: lib.judgment.length })}</div></div>
    ${lib.judgment.length ? html`<div class="card" style="margin-top:20px"><ul class="rows">${lib.judgment.map((j) => html`<li><div class="r-main"><div class="r-title">${j.title}</div>
      <div class="r-meta">${t("acts.meta", { pages: j.pages || "?", n: j.n_chunks || 0 })}${j.meta.source ? ` · ${j.meta.source}` : ""}</div></div>
      <div class="r-act"><a class="btn btn-ghost btn-sm" href="/api/documents/${encodeURIComponent(j.id)}/file" target="_blank" rel="noopener">${t("acts.open_pdf")} ↗</a></div></li>`)}</ul></div>` : ""}`}`);
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
  const filterActs = () => {
    const q = ($("#actFilter")?.value || "").trim().toLowerCase(), j = $("#actJur")?.value || "";
    $$("#actRows > li").forEach((li) => { li.hidden = (!!q && !li.dataset.name.includes(q)) || (!!j && li.dataset.jur !== j); });
  };
  $("#actFilter")?.addEventListener("input", filterActs);
  $("#actJur")?.addEventListener("change", filterActs);
  $$("[data-versions]", main).forEach((b) => { b.onclick = async () => {
    const { versions } = await api(`/api/documents/${encodeURIComponent(b.dataset.versions)}/versions`);
    showDrawer(t("acts.versions"));
    mount($("#drawerBody"), html`<p class="small muted">${t("acts.versions_hint")}</p><ul class="list-plain">${versions.map((v) => html`<li>
      <strong>v${v.version}</strong> · ${fmtDate(v.created_at)} · <span class="chip ${v.status === "ready" ? "chip-ok" : ""}">${t(`acts.ver_${v.status}`, {}, v.status)}</span>
      <a href="/api/documents/${encodeURIComponent(v.id)}/text" target="_blank" rel="noopener">${t("acts.ver_text")} ↗</a></li>`)}</ul>`);
  }; });
}

/** An act as a reader: contents on the left, one provision at a time on the right. */
async function viewActReader(main, docId, section) {
  const act = await api(`/api/library/act/${encodeURIComponent(docId)}`);
  crumbs([[t("nav.acts"), "#/acts"], [act.title]]);
  const chapters = [...new Set(act.sections.map((x) => x.chapter || ""))];
  mount(main, html`<div class="row" style="margin:8px 0"><a class="btn btn-ghost btn-sm" href="#/acts">${t("common.back")}</a><h2 style="margin:0;font-size:1.35rem">${act.title}</h2></div>
    <div class="reader"><nav class="card reader-toc" aria-label="${t("acts.contents")}">${chapters.map((c) => html`${c ? html`<h3>${c}</h3>` : ""}
      ${act.sections.filter((x) => (x.chapter || "") === c).map((x) => html`<button type="button" data-label="${x.label}">${x.label}${x.title ? `. ${x.title}` : ""}</button>`)}`)}</nav>
    <article class="card reader-body" id="readerBody">${spinner()}</article></div>`);
  const show = async (label) => {
    $$("[data-label]", main).forEach((b) => { const on = b.dataset.label === label; b.classList.toggle("active", on); if (on) b.setAttribute("aria-current", "true"); else b.removeAttribute("aria-current"); });
    mount($("#readerBody"), spinner());
    try {
      const x = await api(`/api/library/act/${encodeURIComponent(docId)}?label=${encodeURIComponent(label)}`);
      mount($("#readerBody"), html`<div class="row"><span class="small muted">${x.label}${x.page ? ` · p. ${x.page}` : ""}</span><span class="spacer"></span>
          <a class="btn btn-ghost btn-sm" href="/api/documents/${encodeURIComponent(docId)}/file#page=${x.page || 1}" target="_blank" rel="noopener">${t("acts.open_pdf")} ↗</a>
          ${can("research.use") ? html`<a class="btn btn-saffron btn-sm" href="#/ask?case=&q=${encodeURIComponent(`${x.label} of the ${act.title}: `)}">${t("acts.ask_section")}</a>` : ""}</div>
        <h2>${x.title || x.label}</h2><div class="reader-text">${x.text}</div>`);
    } catch (e) { mount($("#readerBody"), html`<div class="notice notice-red">${e.message}</div>`); }
  };
  $$("[data-label]", main).forEach((b) => { b.onclick = () => show(b.dataset.label); });
  if (act.sections.length) { const first = act.sections.find((x) => x.label === section) || act.sections[0]; show(first.label); }
  else mount($("#readerBody"), html`<div class="empty">${t("acts.no_sections")}</div>`);
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
    <div class="card" id="draftGrid"></div>`);
  const grid = () => {
    const list = drafts.filter((d) => area === "All" || (d.meta.area || "General") === area);
    mount($("#draftGrid"), list.length ? html`<ul class="rows">${list.map((d) => html`<li><div class="r-main"><div class="r-title">${d.title}</div>
      <div class="r-meta"><span class="badge ${TYPE_BADGE[d.meta.area] || "b-draft"}">${t(`cat.${d.meta.area || "General"}`, {}, d.meta.area || "General")}</span></div></div>
      <div class="r-act"><button class="btn btn-ghost btn-sm" data-open="${d.id}">${t("templates.read")}</button>
        <a class="btn btn-sm" href="#/drafting?template=${encodeURIComponent(d.id)}">${t("templates.use")}</a></div></li>`)}</ul>` : html`<div class="empty">${t("templates.none")}</div>`);
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
    <div class="card" style="max-width:860px"><div class="card-h"><h2>${t("limitation.compute")}</h2></div>
        <form class="card-b stack" id="limForm">
          <div><label class="f" for="lc">${t("limitation.proceeding")}</label><select id="lc">${categories.map((c) => html`<option value="${c.key}">[${c.area}] ${c.label} — ${c.period}</option>`)}</select></div>
          <div><label class="f" for="ld">${t("limitation.start")}</label><input type="date" id="ld" value="${today}" required><div class="small muted" id="lstarts"></div></div>
          <button class="btn" type="submit">${t("limitation.calculate")}</button></form>
        <div id="limResult"></div>
        <div class="card-b" style="padding-top:0"><div class="notice small"><strong>${t("limitation.important")}:</strong> ${caveat}</div></div></div>
    <details class="panel" style="margin-top:20px"><summary>${t("limitation.supported")} <span class="s-aside">${categories.length}</span></summary><div class="table-wrap"><table class="gov">
      <thead><tr><th>${t("limitation.proceeding")}</th><th>${t("limitation.period")}</th><th>${t("limitation.starts")}</th><th>${t("limitation.basis")}</th></tr></thead>
      <tbody>${categories.map((c) => html`<tr><td>${c.label} <span class="badge ${TYPE_BADGE[c.area] || ""}">${c.area}</span></td><td>${c.period}</td><td class="small">${c.starts}</td><td class="small">${c.basis}</td></tr>`)}</tbody></table></div></details>`);
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
    ...(can("account.self") ? [["⏳", t("jobs.title"), t("jobs.hint"), "#/jobs"]] : []),
    ["ℹ", t("nav.about"), t("tools.about"), "#/about"]];
  mount(main, html`<h1 class="page-title">${t("nav.tools")}</h1><p class="page-lead">${t("tools.lead")}</p>
    <div class="card"><ul class="rows">${items.map(([ico, title, hint, href]) => html`<li><span class="tile-ico" aria-hidden="true">${ico}</span>
      <div class="r-main"><a class="r-title" href="${href}">${title}</a><div class="r-meta">${hint}</div></div></li>`)}</ul></div>
    ${can("account.self") ? html`<details class="panel" style="margin-top:20px" id="privacyCard" ${new URLSearchParams(location.hash.split("?")[1] || "").get("privacy") ? raw("open") : ""}><summary>⚙ ${t("tools.privacy")} <span class="s-aside">${t("tools.privacy_hint")}</span></summary><form class="panel-b stack" id="privForm">
      ${can("settings.manage") ? html`<div><label class="f" for="ret">${t("privacy.retention")}</label><select id="ret" name="chat_retention_days">
        ${[["", t("privacy.keep")], ["30", "30"], ["90", "90"], ["180", "180"], ["365", "365"]].map(([v, l]) => html`<option value="${v}" ${String(state.prefs.chat_retention_days || "") === v ? raw("selected") : ""}>${l}${v ? ` ${t("privacy.days")}` : ""}</option>`)}</select>
        <div class="hint">${t("privacy.retention_hint")}</div></div>` : ""}
      <div class="small">${t("privacy.voice_status")}: <strong>${state.prefs.voice_consent === "granted" ? t("privacy.granted") : t("privacy.not_granted")}</strong>
        ${state.prefs.voice_consent === "granted" ? html` <button class="btn btn-ghost btn-sm" type="button" id="revokeVoice">${t("privacy.revoke")}</button>` : ""}</div>
      <div class="small muted">${t("privacy.facts")}</div>
      ${can("settings.manage") ? html`<div><button class="btn btn-sm" type="submit">${t("common.save")}</button></div>` : ""}</form></details>` : ""}`);
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
    <div style="max-width:960px">
      <details class="panel" open><summary>${t("about.how")}</summary><div class="panel-b md">${markdown(t("about.how_md"))}</div></details>
      <details class="panel"><summary>${t("about.config")}</summary><div class="panel-b"><table class="gov"><tbody>
        <tr><td>${t("about.answer_model")}</td><td>${s.llm_model} (${s.llm_host})</td></tr><tr><td>${t("about.fast_model")}</td><td>${s.fast_model}</td></tr>
        <tr><td>${t("about.embeddings")}</td><td>${s.embed_model} (local)</td></tr><tr><td>${t("about.reranker")}</td><td>${s.reranker || "—"} (local)</td></tr>
        <tr><td>${t("about.ocr")}</td><td>${s.ocr ? `${s.ocr.engine} (${(s.ocr.langs || []).join(", ")})` : t("about.ocr_missing")}</td></tr>
        <tr><td>${t("about.passages")}</td><td>${num(s.indexed_chunks)}</td></tr>
        <tr><td>${t("about.storage")}</td><td>${s.database || "—"} · ${s.file_storage || "—"}</td></tr></tbody></table></div></details>
      <details class="panel"><summary>${t("footer.sources")}</summary><div class="panel-b"><ul>${["footer.src_acts", "footer.src_ipc", "footer.src_judgments", "footer.src_news", "footer.src_cases"].map((k) => html`<li>${t(k)}</li>`)}</ul><p class="small muted">${t("footer.bottom")}</p></div></details>
      <details class="panel"><summary>${t("about.chunking")}</summary><div class="panel-b md">${markdown(t("about.chunking_md"))}</div></details>
      <details class="panel"><summary>${t("about.security")}</summary><div class="panel-b md">${markdown(t("about.security_md"))}</div></details>
    </div>`);
}

async function viewPrivacy(main) {
  crumbs([[t("nav.privacy")]]);
  mount(main, html`<h1 class="page-title">${t("privacy.title")}</h1><div style="max-width:960px">${spinner()}</div>`);
  let p;
  try { p = await api("/api/privacy"); } catch (e) { mount(main, html`<div class="notice notice-red">${e.message}</div>`); return; }
  mount(main, html`
    <h1 class="page-title">${t("privacy.title")}</h1>
    <div style="max-width:960px">
      <div class="md">${markdown(t("privacy.intro_md"))}</div>
      <details class="panel" open><summary>${t("privacy.periods")}</summary><div class="panel-b"><table class="gov">
        <thead><tr><th>${t("privacy.col_data")}</th><th>${t("privacy.col_kept")}</th><th>${t("privacy.col_from")}</th></tr></thead>
        <tbody>${p.periods.map((r) => html`<tr><td>${r.data}</td><td>${r.kept}</td><td>${r.from}</td></tr>`)}</tbody></table>
        <p class="small muted">${t(p.retention_job ? "privacy.job_on" : "privacy.job_off")}</p></div></details>
      <details class="panel"><summary>${t("privacy.where")}</summary><div class="panel-b md">${markdown(t("privacy.where_md"))}</div></details>
      <details class="panel"><summary>${t("privacy.rights")}</summary><div class="panel-b md">${markdown(t("privacy.rights_md"))}</div></details>
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
function applyTheme(theme) {
  document.documentElement.dataset.theme = theme; store.set("theme", theme);
  $$(".theme-ico").forEach((u) => u.setAttribute("href", theme === "dark" ? "#i-sun" : "#i-moon"));
}
const PANELS = { account: [viewAccount, "auth.account"], admin: [viewAdmin, "admin.title"], privacy: [viewPrivacy, "nav.privacy"], about: [viewAbout, "nav.about"], updates: [viewUpdates, "nav.updates"] };
/** Account, Administration, Privacy, About and Notifications open beside the current page instead of replacing it. */
async function openPanel(name) {
  const [view, title] = PANELS[name]; const body = $("#drawerBody");
  $("#profileMenu").open = false;
  $("#drawer").classList.add("wide"); mount(body, spinner()); showDrawer(t(title));
  try { await view(body, { name, q: new URLSearchParams() }); } catch (e) { if (!e.handled) mount(body, html`<div class="notice notice-red">${e.message}</div>`); }
}

// ── Quick jump (Ctrl+K) ──
const JUMPS = () => [["#/ask", t("nav.ask")], ["#/cases", t("nav.cases_short"), "cases.list"], ["#/cases?new=1", t("qa.new_case"), "case.create"], ["#/calendar", t("cal.title"), "calendar.use"],
  ["#/lookup", t("nav.lookup"), "casesearch.use"], ["#/upload", t("qa.upload_summarise"), "cases.list"], ["#/acts", t("nav.acts")], ["#/drafting", t("nav.drafting"), "drafting.use"], ["#/notice", t("lib.notice"), "drafting.use"],
  ["#/drafts", t("nav.templates")], ["#/limitation", t("nav.limitation")], ["#/tools", t("nav.tools")], ["#/updates", t("nav.updates"), "notifications.read"], ["#/awareness", t("nav.awareness")]].filter(([, , p]) => !p || can(p));
async function openPalette() {
  const dlg = $("#palette"), input = $("#paletteInput"), list = $("#paletteList");
  let cases = [];
  try { if (can("cases.list")) cases = (await api("/api/cases")).cases; } catch { /* links still work */ }
  const items = [...JUMPS(), ...cases.map((c) => [`#/cases/${encodeURIComponent(c.id)}`, c.title])];
  let sel = 0, shown = [];
  const draw = () => {
    const q = input.value.trim().toLowerCase();
    shown = items.filter(([, x]) => !q || x.toLowerCase().includes(q)).slice(0, 9);
    if (q) shown.push([`#/search?q=${encodeURIComponent(q)}`, t("palette.search", { q: input.value.trim() })]);
    sel = Math.min(sel, Math.max(shown.length - 1, 0));
    mount(list, html`${shown.map(([href, x], i) => html`<a href="${href}" role="option" aria-selected="${String(i === sel)}" class="${i === sel ? "sel" : ""}">${x}</a>`)}`);
  };
  input.value = ""; draw(); dlg.showModal(); input.focus();
  input.oninput = () => { sel = 0; draw(); };
  input.onkeydown = (e) => { const d = { ArrowDown: 1, ArrowUp: -1 }[e.key]; if (d && shown.length) { e.preventDefault(); sel = (sel + d + shown.length) % shown.length; draw(); } };
  $("#paletteForm").onsubmit = (e) => { e.preventDefault(); if (shown[sel]) location.hash = shown[sel][0]; dlg.close(); };
  list.onclick = (e) => { if (e.target.closest("a")) dlg.close(); };
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
  applyTheme(store.get("theme", "dark"));
  renderLangToggle();
  $$("[data-font]").forEach((b) => { b.onclick = () => applyFont(Number(b.dataset.font)); });
  document.documentElement.style.setProperty("--fs", [13, 14, 15, 16, 17, 18][store.get("font", 2)] + "px");
  applyContrast(store.get("contrast", false));
  $("#contrastBtn").onclick = () => applyContrast(document.documentElement.dataset.contrast !== "on");
  document.addEventListener("click", (e) => {
    if (e.target.closest("[data-theme-toggle]")) applyTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark");
    const p = e.target.closest("[data-panel-open]"); if (p) openPanel(p.dataset.panelOpen);
  });
  $("#bellLink").onclick = () => openPanel("updates");
  document.addEventListener("keydown", (e) => { if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") { e.preventDefault(); if (!$("#palette").open) openPalette(); } });
  $("#drawerClose").onclick = hideDrawer;
  $("#drawer").addEventListener("click", (e) => { if (e.target.id === "drawer") hideDrawer(); });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !$("#drawer").hidden) hideDrawer(); });
  // Drop-down menus (<details class="menu">) close on a click elsewhere, after choosing an item, or on Escape.
  const closeMenus = (except) => $$("details.menu[open], details.status-pop[open]").forEach((d) => { if (d !== except) d.open = false; });
  document.addEventListener("click", (e) => {
    const d = e.target.closest("details.menu, details.status-pop");
    closeMenus(d);
    if (d && e.target.closest(".menu-list a, .menu-list button")) d.open = false;
  });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeMenus(); });
  $("#logoutBtn").onclick = async () => { try { await api("/api/auth/logout", { method: "POST", retry: false }); } catch { /* already gone */ }
    state.caseId = null; store.set("caseId", null); location.hash = "#/login"; location.reload(); };
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
