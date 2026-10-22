/* Module 10 — state-wise legal awareness (Maharashtra first). Statute text shown is the official text from the library. */
import { $, $$, api, crumbs, fmtDate, html, mount, raw, store } from "./core.js";
import { t } from "./i18n.js";

export async function viewAwareness(main, r) {
  crumbs([[t("nav.awareness")]]);
  const st = r.q.get("state") || store.get("awState", "MH");
  const subject = r.q.get("subject") || "";
  const res = await api(`/api/awareness?state=${encodeURIComponent(st)}${subject ? `&subject=${encodeURIComponent(subject)}` : ""}`);
  store.set("awState", st);
  const focus = r.q.get("item");
  mount(main, html`
    <h1 class="page-title">${t("aw.title")}</h1>
    <p class="page-lead">${t("aw.lead")}</p>
    <div class="notice" style="margin-bottom:12px">${t("aw.disclaimer")}</div>
    <div class="row" style="margin-bottom:12px">
      <label class="f" for="awState" style="margin:0">${t("form.state")}</label>
      <select id="awState" style="max-width:240px">${res.states.filter((s) => s.code !== "central").map((s) => html`<option value="${s.code}" ${s.code === st ? raw("selected") : ""}>${s.name}</option>`)}</select>
      <span class="small muted">${t("aw.more_states")}</span></div>
    <div class="tabs" role="tablist"><a role="tab" class="tab ${subject ? "" : "active"}" href="#/awareness?state=${st}">${t("common.all")}</a>${res.subjects.map((s) => html`<a role="tab" class="tab ${s.id === subject ? "active" : ""}" href="#/awareness?state=${st}&subject=${s.id}">${t(`aw.s_${s.id}`, {}, s.label)}</a>`)}</div>
    ${res.items.length ? html`<div class="stack">${res.items.map((i) => html`<article class="card aw ${i.id === focus ? "focus" : ""}" id="aw-${i.id}">
      <div class="card-h"><h2>${i.title}</h2><span class="chip ${i.state === "central" ? "chip-wait" : "chip-ok"}">${i.jurisdiction}</span></div>
      <div class="card-b">
        <div class="row small muted" style="margin-bottom:6px"><span>${t(`aw.kind_${i.kind}`, {}, i.kind)}</span> · <span>${t("aw.verified", { date: fmtDate(i.last_verified) })}</span></div>
        <p>${i.summary}</p>
        ${i.provisions.length ? html`<h3 class="mini">${t("aw.provisions")}</h3><div class="prov-list">${i.provisions.map((p) => p.found ? html`<details class="prov"><summary><strong>${p.label}</strong> — ${p.heading.split(" — ").slice(1).join(" — ")} <span class="chip chip-ok">${t("prov.verified")}</span></summary>
          <pre class="passage">${p.text}${p.truncated ? "\n…" : ""}</pre><div class="small"><a href="${p.pdf_url}" target="_blank" rel="noopener">${p.act_title} p. ${p.page} ↗</a>${p.official_url ? html` · <a href="${p.official_url}" target="_blank" rel="noopener noreferrer">India Code ↗</a>` : ""}${p.fetched_at ? html` · <span class="muted">${t("prov.fetched", { date: p.fetched_at.slice(0, 10) })}</span>` : ""}</div></details>`
          : html`<div class="prov small">${p.ref} — ${p.act} <span class="chip chip-warn">${t("prov.not_found")}</span></div>`)}</div>` : ""}
        ${i.links.length ? html`<h3 class="mini">${t("aw.official_links")}</h3><ul class="small">${i.links.map((l) => html`<li><a href="${l.url}" target="_blank" rel="noopener noreferrer">${l.title} ↗</a> <span class="muted">(${t("lookup.checked", { date: l.verified_on })})</span></li>`)}</ul>` : ""}
        ${i.kind === "state_notification" ? html`<h3 class="mini">${t("aw.updates")}</h3>${(i.updates || []).length ? html`<ul class="small">${i.updates.map((u) => html`<li>${u.date} — <a href="${u.url}" target="_blank" rel="noopener noreferrer">${u.title}</a></li>`)}</ul>` : html`<div class="small muted">${t("aw.no_updates")}</div>`}` : ""}
        ${i.faqs.length ? html`<h3 class="mini">${t("aw.faqs")}</h3>${i.faqs.map((f) => html`<details class="faq"><summary>${f.q}</summary><p class="small">${f.a}</p></details>`)}` : ""}
      </div></article>`)}</div>` : html`<div class="empty">${t("aw.none")}</div>`}`);
  $("#awState").onchange = (e) => { location.hash = `#/awareness?state=${encodeURIComponent(e.target.value)}`; };
  if (focus) document.getElementById(`aw-${focus}`)?.scrollIntoView();
}
