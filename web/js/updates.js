/* Modules 2 & 12 — notifications, integration status (periodic vs manual — never a fabricated "live"), news, activity log. */
import { $, $$, api, crumbs, fmtDateTime, html, mount, num, spinner, toast } from "./core.js";
import { t } from "./i18n.js";
import { can } from "./auth.js";
import { desktopAlerts } from "./calendar.js";

export async function refreshBell() {
  try {
    const { unread, items } = await api("/api/notifications?unread=1&limit=5");
    desktopAlerts(items);   // calendar reminders as desktop alerts, if the person turned them on
    const b = $("#bellCount");
    b.textContent = unread ? (unread > 99 ? "99+" : String(unread)) : "";
    b.hidden = !unread;
    $("#bellLink").setAttribute("aria-label", unread ? t("updates.bell_n", { n: unread }) : t("updates.bell"));
  } catch { /* offline */ }
}

export async function viewUpdates(main, r) {
  const tab = r.q.get("tab") || "notifications";
  crumbs([[t("nav.updates")]]);
  mount(main, html`<h1 class="page-title">${t("updates.title")}</h1><p class="page-lead">${t("updates.lead")}</p>
    <div class="tabs" role="tablist">${["notifications", "sources", "news", ...(can("audit.read") ? ["activity"] : [])].map((k) => html`<a role="tab" class="tab ${k === tab ? "active" : ""}" aria-selected="${String(k === tab)}" href="#/updates?tab=${k}">${t(`updates.tab_${k}`)}</a>`)}</div>
    <div id="upBody">${spinner()}</div>`);
  const body = $("#upBody");
  if (tab === "notifications") return notifications(body);
  if (tab === "sources") return sources(body);
  if (tab === "news") return news(body, r.q.get("q") || "");
  return activity(body);
}

async function notifications(el) {
  const { items } = await api("/api/notifications?limit=100");
  mount(el, html`<div class="card"><div class="card-h"><h2>${t("updates.tab_notifications")}</h2><button class="btn btn-ghost btn-sm" id="readAll">${t("updates.mark_read")}</button></div>
    ${items.length ? html`<ul class="list-plain card-b">${items.map((n) => html`<li class="${n.read_at ? "" : "unread"}"><span class="chip">${t(`kind.${n.kind}`, {}, n.kind)}</span> ${n.link ? html`<a href="${n.link}">${n.title}</a>` : html`<strong>${n.title}</strong>`}
      ${n.body ? html`<div class="small">${n.body}</div>` : ""}<div class="small muted">${fmtDateTime(n.created_at)}</div></li>`)}</ul>` : html`<div class="empty">${t("updates.none")}</div>`}</div>`);
  $("#readAll").onclick = async () => { await api("/api/notifications/read", { method: "POST" }); refreshBell(); notifications(el); };
}

async function sources(el) {
  const { integrations, scheduler, can_manage: manage } = await api("/api/integrations");
  mount(el, html`<details class="panel" style="margin-bottom:16px"><summary>${t("sync.modes")} <span class="s-aside">${scheduler ? t("sync.scheduler_on") : t("sync.scheduler_off")}</span></summary>
      <div class="panel-b small"><ul style="margin:0;padding-left:18px">
      <li><strong>${t("sync.live")}</strong> — ${t("sync.live_desc")}</li>
      <li><strong>${t("sync.periodic")}</strong> — ${t("sync.periodic_desc")}</li>
      <li><strong>${t("sync.manual")}</strong> — ${t("sync.manual_desc")}</li></ul></div></details>
    <div class="grid g-2">${integrations.map((s) => html`<div class="card"><div class="card-h"><h2>${s.name}</h2>
        <span class="chip ${s.mode === "periodic" ? "chip-ok" : "chip-warn"}">${s.mode === "periodic" ? t("sync.periodic_every", { n: s.interval_minutes >= 1440 ? `${s.interval_minutes / 1440} ${t("sync.days")}` : `${s.interval_minutes} ${t("sync.minutes")}` }) : t("sync.manual")}</span></div>
      <div class="card-b small">
        ${s.mode === "periodic" ? html`<dl class="kv">
          <dt>${t("sync.status")}</dt><dd><span class="chip ${s.status === "ok" ? "chip-ok" : s.status === "error" ? "chip-err" : "chip-wait"}">${t(`sync.st_${s.status}`, {}, s.status)}</span>${s.enabled ? "" : html` <span class="chip">${t("sync.disabled")}</span>`}</dd>
          <dt>${t("sync.last_success")}</dt><dd>${fmtDateTime(s.last_success_at)}</dd>
          <dt>${t("sync.last_attempt")}</dt><dd>${fmtDateTime(s.last_attempt_at)}</dd>
          <dt>${t("sync.next")}</dt><dd>${s.enabled ? fmtDateTime(s.next_due_at) : "—"}</dd>
          <dt>${t("sync.records")}</dt><dd>${num(s.records)}</dd>
          ${s.last_error ? html`<dt>${t("sync.error")}</dt><dd class="err-text">${s.last_error}</dd>` : ""}</dl>
          ${s.log.length ? html`<details><summary>${t("sync.log")}</summary><table class="gov compact"><thead><tr><th>${t("sync.when")}</th><th>${t("sync.trigger")}</th><th>${t("sync.outcome")}</th><th>${t("sync.new")}</th></tr></thead><tbody>
            ${s.log.map((l) => html`<tr><td>${fmtDateTime(l.started_at)}</td><td>${t(`sync.trigger_${l.trigger}`, {}, l.trigger)}</td><td>${t(`sync.out_${l.outcome}`, {}, l.outcome || "…")}${l.error ? ` — ${l.error}` : ""}</td><td>${l.items_new ?? 0}</td></tr>`)}</tbody></table></details>` : ""}`
          : html`<p>${s.why_manual || ""}</p>`}
        ${s.note ? html`<p class="muted">${s.note}</p>` : ""}
        <div class="row" style="margin-top:8px">
          ${s.mode === "periodic" && manage ? html`<button class="btn btn-sm" data-sync="${s.id}">↻ ${t("sync.now")}</button><button class="btn btn-ghost btn-sm" data-toggle="${s.id}" data-on="${s.enabled ? "0" : "1"}">${s.enabled ? t("sync.disable") : t("sync.enable")}</button>` : ""}
          <a class="btn btn-ghost btn-sm" href="${s.home || s.url}" target="_blank" rel="noopener noreferrer">${t("lookup.open_official")} ↗</a>
          ${s.kind === "case_records" || s.kind === "fir" ? html`<a class="btn btn-ghost btn-sm" href="#/lookup">${t("lookup.import")}</a>` : ""}</div>
        <div class="muted" style="margin-top:6px">${t("lookup.checked", { date: s.checked })}</div></div></div>`)}</div>`);
  $$("[data-sync]", el).forEach((b) => { b.onclick = async () => {
    b.disabled = true; b.textContent = t("sync.running");
    try { const x = await api(`/api/integrations/${encodeURIComponent(b.dataset.sync)}/sync`, { method: "POST" });
      toast(x.ok ? t("sync.done", { n: x.new || 0 }) : t("sync.failed", { err: x.error }), !x.ok); refreshBell(); }
    catch (e) { toast(e.message, true); }
    sources(el);
  }; });
  $$("[data-toggle]", el).forEach((b) => { b.onclick = async () => { await api(`/api/integrations/${encodeURIComponent(b.dataset.toggle)}/enabled`, { method: "POST", body: { enabled: b.dataset.on === "1" } }); sources(el); }; });
}

async function news(el, q) {
  const { items } = await api(`/api/news?limit=100&q=${encodeURIComponent(q)}`);
  mount(el, html`<form class="row" id="newsForm" style="margin-bottom:10px"><input type="search" id="nq" value="${q}" placeholder="${t("news.filter")}" aria-label="${t("news.filter")}" style="max-width:360px"><button class="btn btn-sm">${t("common.search")}</button></form>
    <div class="card">${items.length ? html`<ul class="list-plain card-b">${items.map((n) => html`<li><a href="${n.url}" target="_blank" rel="noopener noreferrer">${n.title} ↗</a>
      <div class="small muted">${n.source} · ${fmtDateTime(n.published_at)} · ${t("news.retrieved", { when: fmtDateTime(n.retrieved_at) })}${(n.data?.categories || []).length ? ` · ${n.data.categories.join(", ")}` : ""}</div></li>`)}</ul>`
      : html`<div class="empty">${t("news.none")}</div>`}</div><p class="small muted">${t("news.note")}</p>`);
  $("#newsForm").onsubmit = (e) => { e.preventDefault(); location.hash = `#/updates?tab=news&q=${encodeURIComponent($("#nq").value.trim())}`; };
}

async function activity(el) {
  const { items } = await api("/api/audit?limit=200");
  mount(el, html`<div class="card"><div class="card-h"><h2>${t("updates.tab_activity")}</h2><span class="small muted">${t("activity.note")}</span></div><div class="table-wrap"><table class="gov compact">
    <thead><tr><th>${t("sync.when")}</th><th>${t("activity.action")}</th><th>${t("activity.item")}</th><th>${t("activity.details")}</th></tr></thead>
    <tbody>${items.map((a) => html`<tr class="${a.outcome && a.outcome !== "ok" ? "unread" : ""}"><td class="small">${fmtDateTime(a.at)}</td><td><code>${a.action}</code>${a.outcome && a.outcome !== "ok" ? html` <span class="chip chip-warn">${a.outcome}</span>` : ""}<br><span class="small muted">${a.actor_role || ""} ${a.actor_id ? a.actor_id.slice(0, 8) : ""} ${a.ip || ""}</span></td><td class="small">${a.entity} ${a.entity_id || ""}</td><td class="small">${Object.entries(a.detail || {}).map(([k, v]) => `${k}: ${typeof v === "object" ? JSON.stringify(v) : v}`).join(" · ")}</td></tr>`)}</tbody></table>
    ${items.length ? "" : html`<div class="empty">${t("activity.none")}</div>`}</div></div>`);
}
