/* Module 13 — unified search. Retrieval only: verified source material, with no AI text mixed in. */
import { $, api, crumbs, fmtDate, html, mount, raw, reference, spinner } from "./core.js";
import { t } from "./i18n.js";

const TYPES = ["case", "document", "bare_act", "judgment", "draft", "news", "awareness"];

export async function viewSearch(main, r) {
  crumbs([[t("nav.search")]]);
  const ref = await reference();
  const q = r.q.get("q") || "";
  const sel = new Set((r.q.get("types") || "").split(",").filter(Boolean));
  const get = (k) => r.q.get(k) || "";
  mount(main, html`<h1 class="page-title">${t("search.title")}</h1><p class="page-lead">${t("search.lead")}</p>
    <form class="card" id="sForm"><div class="card-b stack">
      <div class="row"><input type="search" id="sq" name="q" value="${q}" placeholder="${t("search.ph")}" aria-label="${t("search.ph")}" style="flex:1;min-width:240px"><button class="btn btn-saffron">${t("common.search")}</button></div>
      <fieldset class="fs"><legend>${t("search.types")}</legend><div class="row">${TYPES.map((x) => html`<label class="check"><input type="checkbox" name="types" value="${x}" ${!sel.size || sel.has(x) ? raw("checked") : ""}> ${t(`stype.${x}`)}</label>`)}</div></fieldset>
      <details ${get("court") || get("state") || get("year") || get("case_type") ? raw("open") : ""}><summary class="small">${t("search.filters")}</summary><div class="form-grid" style="margin-top:8px">
        <div><label class="f" for="s_court">${t("form.court")}</label><input type="text" id="s_court" name="court" list="s_courts" value="${get("court")}"></div>
        <div><label class="f" for="s_state">${t("form.state")}</label><select id="s_state" name="state"><option value="">—</option>${ref.states.map((s) => html`<option ${s === get("state") ? raw("selected") : ""}>${s}</option>`)}</select></div>
        <div><label class="f" for="s_year">${t("search.year")}</label><input type="text" id="s_year" name="year" inputmode="numeric" maxlength="4" value="${get("year")}"></div>
        <div><label class="f" for="s_ct">${t("form.category")}</label><select id="s_ct" name="case_type"><option value="">—</option>${ref.categories.map((c) => html`<option value="${c}" ${c === get("case_type") ? raw("selected") : ""}>${t(`cat.${c}`, {}, c)}</option>`)}</select></div>
        <div><label class="f" for="s_dt">${t("search.doc_type")}</label><select id="s_dt" name="doc_type"><option value="">—</option>${["judgment", "pleading", "contract", "evidence", "notice", "statute", "other"].map((d) => html`<option value="${d}" ${d === get("doc_type") ? raw("selected") : ""}>${t(`dtype.${d}`, {}, d)}</option>`)}</select></div>
        <div><label class="f" for="s_src">${t("search.source")}</label><select id="s_src" name="source"><option value="">—</option><option value="livelaw" ${get("source") === "livelaw" ? raw("selected") : ""}>LiveLaw</option></select></div>
      </div><datalist id="s_courts">${ref.courts.map((c) => html`<option value="${c.name}">`)}</datalist></details>
    </div></form>
    <div id="sResults" style="margin-top:16px" aria-live="polite"></div>`);
  $("#sForm").onsubmit = (e) => {
    e.preventDefault();
    const fd = new FormData(e.target); const p = new URLSearchParams();
    for (const [k, v] of fd.entries()) if (k !== "types" && String(v).trim()) p.set(k, String(v).trim());
    const types = fd.getAll("types"); if (types.length && types.length < TYPES.length) p.set("types", types.join(","));
    location.hash = `#/search?${p}`;
  };
  if (!q) return;
  mount($("#sResults"), spinner(t("common.searching")));
  const qs = new URLSearchParams({ q, types: [...sel].join(","), court: get("court"), state: get("state"), year: get("year"), case_type: get("case_type"), doc_type: get("doc_type"), source: get("source") });
  const res = await api(`/api/search?${qs}`);
  mount($("#sResults"), html`<div class="row small" style="margin-bottom:8px">${Object.entries(res.counts).map(([k, n]) => html`<span class="chip">${t(`stype.${k}`)}: ${n}</span>`)}</div>
    ${res.results.length ? html`<div class="card">${res.results.map((x) => html`<div class="result">
      <div class="row"><span class="badge b-${x.type}">${t(`stype.${x.type}`)}</span><h3 style="margin:0"><a href="${x.open || x.url}" ${x.external || /^\/api\//.test(x.url) && !x.open ? raw('target="_blank" rel="noopener noreferrer"') : ""}>${x.title}${x.label ? ` — ${x.label}` : ""}</a></h3></div>
      <div class="small muted">${t("search.source_lbl")}: ${x.source}${x.date ? ` · ${fmtDate(x.date)}` : ""}${x.jurisdiction ? ` · ${x.jurisdiction}` : ""}${x.page ? ` · p. ${x.page}` : ""}</div>
      ${x.snippet ? html`<p class="small" style="margin:4px 0 0">${x.snippet}</p>` : ""}
      ${x.open && x.url ? html`<a class="small" href="${x.url}" target="_blank" rel="noopener">${t("case.original")} ↗</a>` : ""}</div>`)}</div>
      <p class="small muted">${t("search.note")}</p>` : html`<div class="empty">${t("search.none")}</div>`}`);
}
