/* Module 8 — potentially applicable acts / sections / articles for a case, with lawyer review (accept / reject / edit). */
import { $, $$, aiConsent, api, html, mount, pollJob, progressBar, showDrawer, spinner, toast } from "./core.js";
import { t } from "./i18n.js";

export async function provisionsPanel(el, c, reload) {
  const render = (list, extra = {}) => {
    mount(el, html`<div class="card"><div class="card-h"><h2>${t("prov.title")}</h2><span class="small muted">${t("prov.subtitle")}</span></div><div class="card-b">
      <p class="small muted">${t("prov.disclaimer")}</p>
      <details ${list.length ? "" : "open"}><summary class="small">${t("prov.run_options")}</summary>
        <label class="f" for="provExtra" style="margin-top:6px">${t("prov.extra")}</label><textarea id="provExtra" rows="3" placeholder="${t("prov.extra_ph")}"></textarea></details>
      <div class="row" style="margin:8px 0"><button class="btn btn-saffron btn-sm" id="provRun">⚖ ${list.length ? t("prov.rerun") : t("prov.run")}</button></div>
      <div id="provJob" aria-live="polite"></div>
      ${extra.conflicts?.length ? html`<div class="notice" style="margin:8px 0"><strong>${t("prov.conflicts")}</strong><ul>${extra.conflicts.map((x) => html`<li>${x}</li>`)}</ul></div>` : ""}
      ${list.length ? html`<div class="prov-list">${list.map(item)}</div>` : html`<div class="empty">${t("prov.none")}</div>`}
      ${extra.precedents?.length ? html`<h3 class="mini">${t("prov.precedents")}</h3><ul class="small">${extra.precedents.map((p) => html`<li><a href="/api/documents/${encodeURIComponent(p.doc_id)}/file#page=${p.page || 1}" target="_blank" rel="noopener">${p.title}</a> ${p.label ? `(${p.label})` : ""} — <span class="muted">${t("prov.precedent_note")}</span></li>`)}</ul>` : ""}
    </div></div>`);
    $("#provRun").onclick = run;
    $$("[data-act]", el).forEach((b) => { b.onclick = () => act(b.dataset.id, b.dataset.act); });
    $$("[data-edit]", el).forEach((b) => { b.onclick = () => edit(b.dataset.edit, b.dataset.ref); });
    $$("[data-text]", el).forEach((b) => { b.onclick = () => showText(b.dataset.actname, b.dataset.text); });
  };
  const item = (p) => html`<div class="prov prov-${p.status}">
    <div class="row"><strong>${p.ref}</strong> — ${p.act}
      <span class="chip ${p.verification === "verified_in_library" ? "chip-ok" : "chip-err"}">${p.verification === "verified_in_library" ? t("prov.verified") : t("prov.not_found")}</span>
      <span class="chip ${p.status === "accepted" ? "chip-ok" : p.status === "rejected" ? "chip-err" : "chip-wait"}">${t(`prov.status_${p.status}`)}</span></div>
    ${p.title ? html`<div class="small muted">${p.title}</div>` : ""}
    ${p.reason ? html`<div class="small">${p.reason}</div>` : ""}
    ${(p.flags || []).map((f) => html`<div class="small flag">⚠ ${f}</div>`)}
    <div class="row" style="margin-top:6px">
      <button type="button" class="btn btn-ghost btn-sm" data-text="${p.ref}" data-actname="${p.act}">${t("prov.show_text")}</button>
      ${p.doc_id ? html`<a class="btn btn-ghost btn-sm" href="/api/documents/${encodeURIComponent(p.doc_id)}/file#page=${p.page || 1}" target="_blank" rel="noopener">${t("acts.open_pdf")} ↗</a>` : ""}
      ${p.status !== "accepted" ? html`<button type="button" class="btn btn-sm" data-act="accepted" data-id="${p.id}">✓ ${t("prov.accept")}</button>` : ""}
      ${p.status !== "rejected" ? html`<button type="button" class="btn btn-danger btn-sm" data-act="rejected" data-id="${p.id}">✕ ${t("prov.reject")}</button>` : ""}
      <button type="button" class="btn btn-ghost btn-sm" data-edit="${p.id}" data-ref="${p.ref}">✎ ${t("common.edit")}</button></div>
    ${p.note ? html`<div class="small muted">${t("prov.note")}: ${p.note}</div>` : ""}</div>`;
  const load = async (extra) => { try { render((await api(`/api/cases/${encodeURIComponent(c.id)}/provisions`)).provisions, extra); } catch (e) { mount(el, html`<div class="notice notice-red">${e.message}</div>`); } };
  const run = async () => {
    if (!(await aiConsent())) return;
    try {
      const job = await api(`/api/cases/${encodeURIComponent(c.id)}/provisions/suggest`, { method: "POST", body: { consent: true, extra: $("#provExtra")?.value || "" } });
      const res = await pollJob(job, (j) => mount($("#provJob"), progressBar(j)));
      render(res.provisions, res); toast(t("prov.done", { n: res.provisions.filter((p) => p.status === "suggested").length }));
    } catch (e) { mount($("#provJob"), html`<div class="notice notice-red">${e.message}</div>`); }
  };
  const act = async (id, status) => {
    try { await api(`/api/provisions/${encodeURIComponent(id)}`, { method: "PATCH", body: { status } }); if (status === "accepted") { toast(t("prov.accepted_toast")); reload(); } else load(); }
    catch (e) { toast(e.message, true); }
  };
  const edit = async (id, ref) => {
    const v = prompt(t("prov.edit_prompt"), ref); if (v === null) return;
    const note = prompt(t("prov.note_prompt"), "") ?? "";
    try { const p = await api(`/api/provisions/${encodeURIComponent(id)}`, { method: "PATCH", body: { ref: v.trim(), note } }); if (p.verification !== "verified_in_library") toast(t("prov.edit_unverified"), true); load(); }
    catch (e) { toast(e.message, true); }
  };
  const showText = async (actName, ref) => {
    mount($("#drawerBody"), spinner()); showDrawer(`${ref} — ${actName}`);
    try {
      const p = await api(`/api/library/provision?act=${encodeURIComponent(actName)}&ref=${encodeURIComponent(ref)}`);
      mount($("#drawerBody"), p.found ? html`<p class="small muted">${p.act_title} · p. ${p.page}${p.fetched_at ? ` · ${t("prov.fetched", { date: p.fetched_at.slice(0, 10) })}` : ""}</p><pre class="passage">${p.text}${p.truncated ? "\n…" : ""}</pre>
        <div class="row"><a class="btn" href="${p.pdf_url}" target="_blank" rel="noopener">${t("acts.open_pdf")} ↗</a>${p.official_url ? html`<a class="btn btn-ghost" href="${p.official_url}" target="_blank" rel="noopener noreferrer">India Code ↗</a>` : ""}</div>`
        : html`<div class="notice notice-red">${t("prov.not_found")}</div>`);
    } catch (e) { mount($("#drawerBody"), html`<div class="notice notice-red">${e.message}</div>`); }
  };
  load();
}
