/* Module 1 — Case information search: any combination of identifiers, honest source handling, manual import. */
import { $, $$, api, crumbs, fieldError, fmtDateTime, formData, html, mount, raw, reference, spinner, toast } from "./core.js";
import { t } from "./i18n.js";

export async function viewCaseSearch(main, r) {
  crumbs([[t("nav.lookup")]]);
  const ref = await reference();
  const year = new Date().getFullYear();
  const f = (id, label, opts = {}) => html`<div class="${opts.cls || ""}"><label class="f" for="cs_${id}">${label}</label>
    <input type="text" id="cs_${id}" name="${id}" value="${r.q.get(id) || ""}" ${opts.list ? raw(`list="${opts.list}"`) : ""} ${opts.ph ? raw(`placeholder="${opts.ph}"`) : ""} autocomplete="off"></div>`;
  mount(main, html`
    <h1 class="page-title">${t("lookup.title")}</h1>
    <p class="page-lead">${t("lookup.lead")}</p>
    <div class="notice" style="margin-bottom:14px">${t("lookup.honesty")}</div>
    <form class="card" id="csForm" novalidate><div class="card-h"><h2>${t("lookup.identifiers")}</h2><span class="small muted">${t("lookup.any_combo")}</span></div>
      <div class="card-b"><div class="form-grid">
        ${f("cnr_number", t("form.cnr"), { ph: "MHPU010012342023" })}${f("case_number", t("form.case_number"))}${f("filing_number", t("form.filing_number"))}
        ${f("fir_number", t("form.fir"), { ph: "123/2024" })}
        ${f("court", t("form.court"), { list: "cs_courts", ph: t("form.search_ph") })}
        ${f("case_type", t("form.case_type"), { list: "cs_types", ph: t("form.search_ph") })}
        <div><label class="f" for="cs_state">${t("form.state")}</label><select id="cs_state" name="state"><option value="">—</option>${ref.states.map((s) => html`<option>${s}</option>`)}</select></div>
        <div id="cs_dist"></div>
        ${f("police_station", t("form.police_station"))}
        <div><label class="f" for="cs_filing_year">${t("form.filing_year")}</label><select id="cs_filing_year" name="filing_year"><option value="">—</option>${Array.from({ length: year - 1949 }, (_, i) => year - i).map((y) => html`<option>${y}</option>`)}</select></div>
        ${f("petitioner", t("lookup.petitioner"))}${f("respondent", t("lookup.respondent"))}
      </div>
      <datalist id="cs_courts">${ref.courts.map((c) => html`<option value="${c.name}">`)}</datalist>
      <datalist id="cs_types">${Object.values(ref.case_types).flat().map((c) => html`<option value="${c}">`)}</datalist>
      <div class="row" style="margin-top:12px"><button class="btn btn-saffron" type="submit">🔎 ${t("lookup.search")}</button><button class="btn btn-ghost" type="reset">${t("common.clear")}</button></div></div></form>
    <div id="csResults" style="margin-top:16px" aria-live="polite"></div>`);
  const dist = () => {
    const st = $("#cs_state").value, list = ref.districts[st];
    mount($("#cs_dist"), html`<label class="f" for="cs_district">${t("form.district")}</label>${list
      ? html`<select id="cs_district" name="district"><option value="">—</option>${list.map((d) => html`<option>${d}</option>`)}</select>`
      : html`<input type="text" id="cs_district" name="district" ${st ? "" : raw("disabled")}>`}`);
  };
  $("#cs_state").onchange = dist; dist();
  const form = $("#csForm");
  form.onsubmit = async (e) => {
    e.preventDefault();
    const body = formData(form);
    mount($("#csResults"), spinner(t("common.searching")));
    try { renderResults(await api("/api/casesearch", { method: "POST", body }), body); }
    catch (err) { mount($("#csResults"), html`<div class="notice notice-red">${err.message}</div>`); }
  };
}

const FIELD = (f) => t(`field.${f}`, {}, f.replace(/_/g, " "));

function renderResults(res, query) {
  const out = $("#csResults");
  if (!res.ok) {
    mount(out, html`<div class="notice notice-red" role="alert"><strong>${t("lookup.fix")}</strong><ul>${res.errors.map((e) => html`<li>${t(`err.${e.code}`, {}, e.message)}</li>`)}</ul></div>`);
    const first = res.errors.find((e) => e.field); if (first) $(`#cs_${first.field}`)?.focus();
    return;
  }
  const j = res.jurisdiction;
  mount(out, html`
    ${res.needs.length ? html`<div class="notice" style="margin-bottom:12px"><strong>${t("lookup.need_more")}</strong><ul>${res.needs.map((n) => html`<li>${t(`need.${n.for}`, {}, n.message)} — <em>${n.fields.map(FIELD).join(", ")}</em></li>`)}</ul></div>` : ""}
    <div class="grid g-2">
      <div class="card"><div class="card-h"><h2>${t("lookup.jurisdiction")}</h2></div><div class="card-b small"><dl class="kv">
        <dt>${t("form.state")}</dt><dd>${j.state || "—"}</dd><dt>${t("form.district")}</dt><dd>${j.district || "—"}</dd>
        <dt>${t("form.court")}</dt><dd>${j.court || "—"}${j.level ? ` (${j.level})` : ""}</dd>
        ${j.cnr ? html`<dt>CNR</dt><dd>${j.cnr.cnr} · ${t("lookup.cnr_parts", { est: j.cnr.establishment_code, serial: j.cnr.serial, year: j.cnr.year })}</dd>` : ""}</dl>
        ${j.inferred.length ? html`<div class="chip chip-warn" style="white-space:normal;margin-top:6px">${j.inferred.join(" ")}</div>` : ""}</div></div>
      <div class="card"><div class="card-h"><h2>${t("lookup.in_workspace")}</h2></div><div class="card-b">
        ${res.local_cases.length || res.imported_records.length ? html`<ul class="list-plain">
          ${res.local_cases.map((c) => html`<li><a href="#/cases/${encodeURIComponent(c.id)}">${c.title}</a> <span class="chip">${c.match === "identifier" ? t("lookup.match_id") : t("lookup.match_party")}</span><br><span class="small muted">${[c.case_number, c.cnr_number, c.court].filter(Boolean).join(" · ")}</span></li>`)}
          ${res.imported_records.map((x) => recordItem(x))}</ul>`
          : html`<div class="empty">${t("lookup.no_local")}</div>`}</div></div>
    </div>
    <div class="card" style="margin-top:16px"><div class="card-h"><h2>${t("lookup.official")}</h2></div><div class="card-b">
      <p class="small muted">${t("lookup.official_hint")}</p>
      ${res.official_sources.map((s) => html`<div class="source-box">
        <div class="row"><strong>${s.name}</strong><span class="chip ${s.access === "manual" ? "chip-warn" : "chip-wait"}">${t(`access.${s.access}`, {}, s.access)}</span><span class="small muted">${t("lookup.search_by", { mode: FIELD(s.search_mode) })}</span></div>
        ${s.why_manual ? html`<div class="small muted">${s.why_manual}</div>` : ""}
        ${Object.keys(s.enter || {}).length ? html`<table class="gov compact" style="margin:8px 0"><tbody>${Object.entries(s.enter).map(([k, v]) => html`<tr><td>${FIELD(k)}</td><td>${v ? html`<code>${v}</code> <button type="button" class="btn btn-ghost btn-sm" data-copy="${v}">${t("common.copy")}</button>` : html`<span class="chip chip-warn">${t("lookup.missing")}</span>`}</td></tr>`)}</tbody></table>` : ""}
        <div class="row">${s.url ? html`<a class="btn btn-saffron btn-sm" href="${s.url}" target="_blank" rel="noopener noreferrer">${t("lookup.open_official")} ↗</a>` : ""}
          ${Object.entries(s.extra_links || {}).map(([lbl, u]) => html`<a class="btn btn-ghost btn-sm" href="${u}" target="_blank" rel="noopener noreferrer">${lbl} ↗</a>`)}
          ${s.source ? html`<button type="button" class="btn btn-ghost btn-sm" data-import="${s.source}">⬇ ${t("lookup.import")}</button>` : ""}</div>
        ${s.checked ? html`<div class="small muted">${t("lookup.checked", { date: s.checked })}</div>` : ""}</div>`)}
    </div></div>
    <div id="importBox"></div>`);
  $$("[data-copy]", out).forEach((b) => { b.onclick = async () => { try { await navigator.clipboard.writeText(b.dataset.copy); toast(t("common.copied")); } catch { toast(t("common.copy_failed"), true); } }; });
  $$("[data-import]", out).forEach((b) => { b.onclick = () => importForm(b.dataset.import, query); });
  bindRecordActions(out);
}

const recordItem = (x) => html`<li><strong>${x.title}</strong> <span class="chip chip-warn" title="${t("lookup.user_entered_hint")}">${t("lookup.user_entered")}</span><br>
  <span class="small muted">${x.source} · ${t("lookup.retrieved", { when: fmtDateTime(x.retrieved_at) })}${x.data?.next_hearing_date ? ` · ${t("form.next_hearing")}: ${x.data.next_hearing_date}` : ""}</span>
  <div class="row" style="margin-top:4px">${x.case_id ? html`<a class="btn btn-ghost btn-sm" href="#/cases/${encodeURIComponent(x.case_id)}">${t("lookup.open_case")}</a>` : html`<button type="button" class="btn btn-sm" data-save="${x.id}">${t("lookup.save_case")}</button>`}</div></li>`;

function bindRecordActions(el) {
  $$("[data-save]", el).forEach((b) => { b.onclick = async () => {
    try { const c = await api(`/api/records/${encodeURIComponent(b.dataset.save)}/save-case`, { method: "POST" }); toast(t("lookup.saved")); location.hash = `#/cases/${encodeURIComponent(c.id)}?edit=1`; }
    catch (e) { toast(e.message, true); }
  }; });
}

async function importForm(source, query) {
  const box = $("#importBox");
  const fields = ["cnr_number", "case_number", "filing_number", "fir_number", "court", "case_type", "state", "district", "police_station", "filing_year", "petitioner", "respondent"];
  mount(box, html`<form class="card" id="impForm" style="margin-top:16px" novalidate><div class="card-h"><h2>${t("lookup.import_title")}</h2><span class="small muted">${source}</span></div>
    <div class="card-b"><p class="small">${t("lookup.import_hint")}</p><div class="form-grid">
      ${fields.map((k) => html`<div><label class="f" for="im_${k}">${FIELD(k)}</label><input type="text" id="im_${k}" name="${k}" value="${query[k] || ""}"></div>`)}
      <div><label class="f" for="im_status">${t("lookup.status")}</label><input type="text" id="im_status" name="status"></div>
      <div><label class="f" for="im_stage">${t("lookup.stage")}</label><input type="text" id="im_stage" name="stage"></div>
      <div><label class="f" for="im_next">${t("form.next_hearing")}</label><input type="date" id="im_next" name="next_hearing_date"></div>
      <div class="span-2"><label class="f" for="im_sections">${t("lookup.sections")}</label><input type="text" id="im_sections" name="sections"></div>
      <div><label class="f" for="im_url">${t("lookup.source_url")}</label><input type="text" id="im_url" name="source_url" placeholder="https://"></div>
      <div class="span-3"><label class="f" for="im_orders">${t("lookup.orders")}</label><textarea id="im_orders" name="orders_text" rows="3" placeholder="2024-03-15 | Notice issued&#10;2024-04-02 | Adjourned"></textarea><div class="hint">${t("lookup.orders_hint")}</div></div>
    </div><div class="row" style="margin-top:10px"><button class="btn btn-saffron" type="submit">${t("lookup.import_save")}</button><button class="btn btn-ghost" type="button" id="impCancel">${t("common.cancel")}</button></div></div></form>`);
  box.scrollIntoView({ behavior: "smooth" });
  $("#impCancel").onclick = () => { box.innerHTML = ""; };
  $("#impForm").onsubmit = async (e) => {
    e.preventDefault();
    const b = formData(e.target);
    const orders = (b.orders_text || "").split("\n").map((l) => l.split("|").map((x) => x.trim())).filter((p) => p[0]).map(([date, title]) => ({ date, title: title || "" }));
    delete b.orders_text;
    Object.keys(b).forEach((k) => { if (!b[k]) delete b[k]; });
    try {
      const res = await api("/api/records/import", { method: "POST", body: { ...b, source, orders } });
      const changed = res.changes.length ? t("lookup.changes", { n: res.changes.length }) : t(`lookup.result_${res.result}`);
      mount(box, html`<div class="card" style="margin-top:16px"><div class="card-h"><h2>${t("lookup.imported")}</h2></div><div class="card-b">
        <div class="notice notice-green">${changed}</div>
        ${res.changes.length ? html`<ul class="small">${res.changes.map((c) => html`<li>${FIELD(c.field)}: ${JSON.stringify(c.old ?? "—")} → ${JSON.stringify(c.new ?? "—")}</li>`)}</ul>` : ""}
        <ul class="list-plain">${recordItem(res.record)}</ul></div></div>`);
      bindRecordActions(box);
    } catch (err) { fieldError(e.target, err); }
  };
}
