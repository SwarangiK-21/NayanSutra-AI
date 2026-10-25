/* All-India law: the official India Code catalogue for Central Acts and every state / Union Territory.
   Results come live from indiacode.gov.in (title, year, act number, repeal flag and the official link, as India Code
   records them). "Add to research library" downloads that act's official English text so Legal Research can search
   and cite it; nothing is summarised or invented here. */
import { $, $$, api, html, mount, raw, spinner, store, toast } from "./core.js";
import { t } from "./i18n.js";
import { can } from "./auth.js";

export async function catalogueView(box, r) {
  let j;
  try { j = await api("/api/jurisdictions"); }
  catch (e) { mount(box, html`<div class="notice notice-red">${e.message}</div>`); return; }
  const states = j.states.filter((s) => !s.union_territory), uts = j.states.filter((s) => s.union_territory);
  let state = r.q.get("state") || store.get("catState", "Central");
  if (state !== "Central" && !j.states.some((s) => s.name === state)) state = "Central";
  let page = 0;
  const opt = (s) => html`<option value="${s.name}" ${s.name === state ? raw("selected") : ""}>${s.name}${s.in_library ? ` (${t("acts.n_in_library", { n: s.in_library })})` : ""}</option>`;
  mount(box, html`<div class="card"><div class="card-h"><h2>${t("acts.catalogue_title")}</h2>
      <a class="small" href="${j.source_url}" target="_blank" rel="noopener noreferrer">indiacode.gov.in ↗</a></div>
    <form class="card-b row" id="catForm" style="align-items:flex-end">
      <div style="flex:1;min-width:220px"><label class="f" for="catState">${t("acts.jurisdiction")}</label>
        <select id="catState"><option value="Central" ${state === "Central" ? raw("selected") : ""}>${t("acts.central_acts")}${j.central.in_library ? ` (${t("acts.n_in_library", { n: j.central.in_library })})` : ""}</option>
          <optgroup label="${t("acts.states")}">${states.map(opt)}</optgroup><optgroup label="${t("acts.uts")}">${uts.map(opt)}</optgroup></select></div>
      <div style="flex:2;min-width:220px"><label class="f" for="catQ">${t("acts.catalogue_q")}</label>
        <input type="search" id="catQ" placeholder="${t("acts.catalogue_ph")}" value="${r.q.get("q") || ""}"></div>
      <button class="btn" type="submit">${t("common.search")}</button></form>
    <div class="card-b small muted" style="padding-top:0">${t("acts.catalogue_note")}</div>
    <div id="catResults" aria-live="polite"></div></div>`);

  const load = async () => {
    state = $("#catState").value; store.set("catState", state);
    const q = $("#catQ").value.trim();
    mount($("#catResults"), html`<div class="card-b">${spinner(t("acts.catalogue_loading"))}</div>`);
    let res;
    try { res = await api(`/api/library/indiacode?state=${encodeURIComponent(state)}&q=${encodeURIComponent(q)}&page=${page}`); }
    catch (e) { mount($("#catResults"), html`<div class="card-b notice notice-red">${e.message}</div>`); return; }
    const mayImport = can("library.import");
    mount($("#catResults"), res.items.length ? html`
      <div class="card-b small muted" style="padding-bottom:0">${t("acts.catalogue_count", { n: res.total, where: state === "Central" ? t("acts.central_acts") : state })}</div>
      <ul class="rows">${res.items.map((a) => html`<li><div class="r-main">
        <div class="r-title">${a.title}</div>
        <div class="r-meta">${a.act_number ? `${t("acts.act_no", { n: a.act_number })} · ` : ""}${a.year || "—"} · ${a.jurisdiction === "Central" ? t("research.central_law") : a.jurisdiction}
          ${a.repealed ? html` <span class="chip chip-warn">${t("acts.repealed_ic")}</span>` : ""}</div></div>
        <div class="r-act"><a class="btn btn-ghost btn-sm" href="${a.item_url}" target="_blank" rel="noopener noreferrer">India Code ↗</a>
          ${a.in_library ? html`<span class="chip ${a.in_library.status === "ready" ? "chip-ok" : "chip-wait"}">${a.in_library.status === "ready" ? `✓ ${t("acts.in_library_short")}` : t("acts.indexing")}</span>`
            : mayImport ? html`<button type="button" class="btn btn-sm" data-import="${a.uuid}">＋ ${t("acts.add_to_library")}</button>` : ""}</div></li>`)}</ul>
      ${res.pages > 1 ? html`<div class="card-b row"><button type="button" class="btn btn-ghost btn-sm" id="catPrev" ${res.page <= 0 ? raw("disabled") : ""}>‹ ${t("cal.prev")}</button>
        <span class="small muted">${t("acts.page_of", { page: res.page + 1, pages: res.pages })}</span>
        <button type="button" class="btn btn-ghost btn-sm" id="catNext" ${res.page + 1 >= res.pages ? raw("disabled") : ""}>${t("cal.next")} ›</button></div>` : ""}`
      : html`<div class="card-b empty">${t("acts.catalogue_none")}</div>`);
    $("#catPrev")?.addEventListener("click", () => { page -= 1; load(); });
    $("#catNext")?.addEventListener("click", () => { page += 1; load(); });
    $$("[data-import]", $("#catResults")).forEach((b) => { b.onclick = async () => {
      b.disabled = true; b.textContent = t("acts.importing");
      try {
        const d = await api("/api/library/indiacode/import", { method: "POST", body: { uuid: b.dataset.import } });
        toast(d.duplicate ? t("acts.already_in_library") : t("acts.imported", { title: d.title }));
        b.replaceWith(Object.assign(document.createElement("span"), { className: "chip chip-wait", textContent: t("acts.indexing") }));
      } catch (e) { toast(e.message, true); b.disabled = false; b.textContent = `＋ ${t("acts.add_to_library")}`; }
    }; });
  };
  $("#catForm").onsubmit = (e) => { e.preventDefault(); page = 0; load(); };
  $("#catState").onchange = () => { page = 0; load(); };
  load();
}
