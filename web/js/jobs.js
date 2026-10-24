/* Background jobs: the signed-in user's own tasks (document processing, translations, summaries, drafts,
   provision suggestions) with their state, progress, attempts and errors; cancel and retry. */
import { $, $$, api, crumbs, fmtDateTime, html, mount, progressBar, spinner, state, toast } from "./core.js";
import { t } from "./i18n.js";

const CHIP = { queued: "chip-wait", processing: "chip-wait", completed: "chip-ok", partial: "chip-warn", failed: "chip-err",
  dead: "chip-err", cancelled: "" };
const ACTIVE = new Set(["queued", "processing"]);

export async function viewJobs(main) {
  crumbs([[t("nav.tools"), "#/tools"], [t("jobs.title")]]);
  mount(main, html`<h1 class="page-title">${t("jobs.title")}</h1><p class="page-lead">${t("jobs.lead")}</p><div id="jobList">${spinner()}</div>`);
  const load = async () => {
    clearInterval(state.pollTimer);
    let jobs;
    try { ({ jobs } = await api("/api/jobs?limit=100")); } catch (e) { mount($("#jobList"), html`<div class="notice notice-red">${e.message}</div>`); return; }
    mount($("#jobList"), jobs.length ? html`<div class="card"><div class="table-wrap"><table class="gov"><thead><tr>
        <th>${t("jobs.task")}</th><th>${t("jobs.state")}</th><th>${t("jobs.progress")}</th><th>${t("jobs.started")}</th><th></th></tr></thead><tbody>
      ${jobs.map((j) => html`<tr><td><strong>${t(`jobs.kind_${j.kind}`, {}, j.kind)}</strong>${j.case_id ? html`<br><a class="small" href="#/cases/${encodeURIComponent(j.case_id)}">${t("jobs.open_case")}</a>` : ""}
          ${j.result?.fallback_model_used ? html`<br><span class="chip chip-warn">${t("trace.fallback")}</span>` : ""}</td>
        <td><span class="chip ${CHIP[j.status] || ""}">${t(`jobs.st_${j.status}`, {}, j.status)}</span>
          ${j.attempts > 1 ? html`<div class="small muted">${t("jobs.attempts", { n: j.attempts, max: j.max_attempts })}</div>` : ""}</td>
        <td style="min-width:180px">${ACTIVE.has(j.status) ? progressBar(j) : html`<span class="small">${j.error || j.message || ""}</span>`}</td>
        <td class="small">${fmtDateTime(j.created_at)}${j.finished_at ? html`<br><span class="muted">${t("jobs.finished")} ${fmtDateTime(j.finished_at)}</span>` : ""}</td>
        <td>${ACTIVE.has(j.status) && !j.cancel_requested ? html`<button class="btn btn-ghost btn-sm" data-cancel="${j.id}">${t("jobs.cancel")}</button>` : ""}
          ${j.cancel_requested && ACTIVE.has(j.status) ? html`<span class="small muted">${t("jobs.cancelling")}</span>` : ""}
          ${["failed", "dead", "cancelled"].includes(j.status) ? html`<button class="btn btn-ghost btn-sm" data-retry="${j.id}">${t("jobs.retry")}</button>` : ""}</td></tr>`)}
      </tbody></table></div></div>` : html`<div class="empty">${t("jobs.none")}</div>`);
    $$("[data-cancel]", main).forEach((b) => { b.onclick = async () => {
      try { await api(`/api/jobs/${encodeURIComponent(b.dataset.cancel)}/cancel`, { method: "POST" }); toast(t("jobs.cancel_sent")); load(); } catch (e) { toast(e.message, true); } }; });
    $$("[data-retry]", main).forEach((b) => { b.onclick = async () => {
      try { await api(`/api/jobs/${encodeURIComponent(b.dataset.retry)}/retry`, { method: "POST" }); toast(t("jobs.requeued")); load(); } catch (e) { toast(e.message, true); } }; });
    if (jobs.some((j) => ACTIVE.has(j.status))) state.pollTimer = setInterval(load, 2500);
  };
  load();
}
