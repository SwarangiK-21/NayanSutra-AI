/* Calendar — a lawyer's hearings, deadlines, follow-ups, submissions, meetings and tasks, with reminders.
   Month / week / day views; entries are private to their owner. Reminders are scheduled and delivered by the server
   (bell notifications, optional e-mail), so they arrive even when this page is closed; desktop alerts are an extra,
   asked for only when the person turns them on. */
import { $, $$, api, crumbs, html, mount, raw, spinner, store, toast } from "./core.js";
import { lang, t } from "./i18n.js";

const TZ = (() => { try { return Intl.DateTimeFormat().resolvedOptions().timeZone || "Asia/Kolkata"; } catch { return "Asia/Kolkata"; } })();
const KIND_ICON = { hearing: "⚖", deadline: "⏰", follow_up: "↻", submission: "📄", meeting: "👥", task: "✓" };
let cfgCache = null;
const config = async () => (cfgCache ||= await api("/api/calendar/config"));

// ── Dates (ISO strings, local calendar days) ──
const iso = (d) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
const parse = (s) => { const [y, m, d] = s.split("-").map(Number); return new Date(y, m - 1, d); };
const addDays = (s, n) => { const d = parse(s); d.setDate(d.getDate() + n); return iso(d); };
const mondayOf = (s) => { const d = parse(s); const wd = (d.getDay() + 6) % 7; d.setDate(d.getDate() - wd); return iso(d); };
const todayIso = () => iso(new Date());
const loc = () => (lang() === "hi" ? "hi-IN" : "en-IN");
const fmt = (s, opts) => parse(s).toLocaleDateString(loc(), opts);
const fmtLong = (s) => fmt(s, { weekday: "short", day: "numeric", month: "short", year: "numeric" });

function range(view, cursor) {
  if (view === "day") return [cursor, cursor];
  if (view === "week") { const s = mondayOf(cursor); return [s, addDays(s, 6)]; }
  const first = cursor.slice(0, 8) + "01";
  const s = mondayOf(first);
  return [s, addDays(s, 41)];   // six weeks
}
function title(view, cursor) {
  if (view === "day") return fmtLong(cursor);
  if (view === "week") { const [s, e] = range("week", cursor); return `${fmt(s, { day: "numeric", month: "short" })} – ${fmt(e, { day: "numeric", month: "short", year: "numeric" })}`; }
  return fmt(cursor, { month: "long", year: "numeric" });
}

export const kindLabel = (k) => t(`cal.kind_${k}`, {}, k);
const when = (e) => `${e.event_time || t("cal.all_day")}${e.event_time && e.timezone && e.timezone !== TZ ? ` (${e.timezone})` : ""}`;
const chipCls = (e) => `ev ev-${e.kind} ${e.priority === "high" ? "ev-high" : ""} ${e.status !== "scheduled" ? `ev-${e.status}` : ""} ${e.source === "case" ? "ev-case" : ""}`;

// ── Page ──
export async function viewCalendar(main, r) {
  crumbs([[t("cal.title")]]);
  let view = r.q.get("view") || store.get("calView", "month");
  if (!["month", "week", "day"].includes(view)) view = "month";
  let cursor = /^\d{4}-\d{2}-\d{2}$/.test(r.q.get("date") || "") ? r.q.get("date") : todayIso();
  const [cfg, { cases }] = await Promise.all([config(), api("/api/cases")]);
  const myCases = cases.filter((c) => c.access);
  mount(main, html`
    <div class="page-head"><h1 class="page-title">${t("cal.title")}</h1><span class="head-actions">
      <button class="btn btn-saffron" id="calNew">＋ ${t("cal.new")}</button></span></div>
    <p class="page-lead">${t("cal.lead")}</p>
    <div class="cal-bar">
      <div class="row"><button class="btn btn-ghost btn-sm" id="calPrev" aria-label="${t("cal.prev")}">‹</button>
        <button class="btn btn-ghost btn-sm" id="calToday">${t("cal.today")}</button>
        <button class="btn btn-ghost btn-sm" id="calNext" aria-label="${t("cal.next")}">›</button>
        <h2 class="cal-title" id="calTitle" aria-live="polite"></h2></div>
      <div class="seg" role="group" aria-label="${t("cal.view")}">${["month", "week", "day"].map((v) => html`<button type="button" data-view="${v}" aria-pressed="false">${t(`cal.${v}`)}</button>`)}</div>
    </div>
    <div class="grid g-side cal-layout">
      <section id="calBody" aria-label="${t("cal.title")}">${spinner()}</section>
      <aside>
        <div class="card"><div class="card-h"><h2>${t("cal.upcoming")}</h2><span class="small muted" id="calOverdue"></span></div><div id="calUpcoming">${spinner()}</div></div>
        <div class="small muted" style="margin-top:12px" id="calAlerts"></div>
      </aside>
    </div>`);

  const setView = (v) => { view = v; store.set("calView", v); $$("[data-view]", main).forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.view === v))); load(); };
  $$("[data-view]", main).forEach((b) => { b.onclick = () => setView(b.dataset.view); });
  const step = (dir) => {
    if (view === "day") cursor = addDays(cursor, dir);
    else if (view === "week") cursor = addDays(cursor, 7 * dir);
    else { const d = parse(cursor); d.setDate(1); d.setMonth(d.getMonth() + dir); cursor = iso(d); }
    load();
  };
  $("#calPrev").onclick = () => step(-1);
  $("#calNext").onclick = () => step(1);
  $("#calToday").onclick = () => { cursor = todayIso(); load(); };
  $("#calNew").onclick = () => editEvent(null, { event_date: view === "month" && cursor.slice(0, 7) !== todayIso().slice(0, 7) ? cursor : todayIso() });

  let items = [];
  const byDay = () => { const m = {}; for (const e of items) (m[e.event_date] ||= []).push(e); return m; };

  async function load() {
    $$("[data-view]", main).forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.view === view)));
    $("#calTitle").textContent = title(view, cursor);
    const [s, e] = range(view, cursor);
    try { items = (await api(`/api/calendar/events?start=${s}&end=${e}`)).events; }
    catch (err) { mount($("#calBody"), html`<div class="notice notice-red">${err.message}</div>`); return; }
    if (view === "month") renderMonth(s); else if (view === "week") renderWeek(s); else renderDay();
    loadUpcoming();
  }

  const chip = (e) => html`<button type="button" class="${chipCls(e)}" data-ev="${e.id}" title="${kindLabel(e.kind)} · ${e.title}">
    <span aria-hidden="true">${KIND_ICON[e.kind] || "•"}</span> ${e.event_time ? html`<span class="ev-t">${e.event_time}</span> ` : ""}<span class="ev-n">${e.title}</span></button>`;

  function renderMonth(start) {
    const days = byDay(), month = cursor.slice(0, 7), today = todayIso();
    const heads = Array.from({ length: 7 }, (_, i) => fmt(addDays(start, i), { weekday: "short" }));
    mount($("#calBody"), html`<div class="card cal-month" role="grid" aria-label="${title("month", cursor)}">
      <div class="cal-row cal-head" role="row">${heads.map((h) => html`<div role="columnheader">${h}</div>`)}</div>
      ${Array.from({ length: 6 }, (_, w) => html`<div class="cal-row" role="row">${Array.from({ length: 7 }, (_, i) => {
        const d = addDays(start, w * 7 + i), list = days[d] || [];
        return html`<div role="gridcell" class="cal-cell ${d.slice(0, 7) !== month ? "out" : ""} ${d === today ? "today" : ""}" data-day="${d}">
          <button type="button" class="cal-date" data-goday="${d}" aria-label="${fmtLong(d)}${list.length ? ` — ${t("cal.n_items", { n: list.length })}` : ""}">${Number(d.slice(8))}</button>
          ${list.slice(0, 3).map(chip)}${list.length > 3 ? html`<button type="button" class="ev-more" data-goday="${d}">+${list.length - 3} ${t("cal.more")}</button>` : ""}
          ${list.length ? html`<span class="cal-dots" aria-hidden="true">${list.length}</span>` : ""}</div>`;
      })}</div>`)}</div>`);
    bindBody();
  }

  function renderWeek(start) {
    const days = byDay(), today = todayIso();
    mount($("#calBody"), html`<div class="cal-week">${Array.from({ length: 7 }, (_, i) => {
      const d = addDays(start, i), list = days[d] || [];
      return html`<div class="card cal-wday ${d === today ? "today" : ""}"><div class="card-h"><button type="button" class="cal-date-lg" data-goday="${d}">${fmt(d, { weekday: "short", day: "numeric", month: "short" })}</button>
        <button type="button" class="rs-close" data-newon="${d}" aria-label="${t("cal.new_on", { date: fmtLong(d) })}" title="${t("cal.new")}">＋</button></div>
        <div class="cal-wlist">${list.length ? list.map(chip) : html`<div class="small muted">—</div>`}</div></div>`;
    })}</div>`);
    bindBody();
  }

  function renderDay() {
    const list = byDay()[cursor] || [];
    mount($("#calBody"), html`<div class="card">${list.length ? html`<ul class="rows">${list.map((e) => html`<li>
        <span class="ev-ico ${chipCls(e)}" aria-hidden="true">${KIND_ICON[e.kind] || "•"}</span>
        <div class="r-main"><button type="button" class="linklike r-title" data-ev="${e.id}">${e.title}</button>
          <div class="r-meta">${when(e)} · ${kindLabel(e.kind)}${e.case_title && e.source !== "case" ? ` · ${e.case_title}` : ""}${e.source === "case" ? ` · ${t("cal.from_case")}` : ""}
            ${e.priority === "high" ? html` <span class="chip chip-warn">${t("cal.p_high")}</span>` : ""}${e.status !== "scheduled" ? html` <span class="chip">${t(`cal.st_${e.status}`)}</span>` : ""}</div></div>
        ${e.source === "case" ? "" : e.status === "scheduled" ? html`<button class="btn btn-ghost btn-sm" data-done="${e.id}">✓ ${t("cal.complete")}</button>` : ""}</li>`)}</ul>`
      : html`<div class="empty">${t("cal.none_day")} <button type="button" class="linklike" data-newon="${cursor}">${t("cal.add_one")}</button></div>`}</div>`);
    bindBody();
  }

  function bindBody() {
    const body = $("#calBody");
    $$("[data-ev]", body).forEach((b) => { b.onclick = (ev) => { ev.stopPropagation(); showEvent(items.find((x) => x.id === b.dataset.ev)); }; });
    $$("[data-goday]", body).forEach((b) => { b.onclick = (ev) => { ev.stopPropagation(); cursor = b.dataset.goday; setView("day"); }; });
    $$("[data-newon]", body).forEach((b) => { b.onclick = () => editEvent(null, { event_date: b.dataset.newon }); });
    $$("[data-done]", body).forEach((b) => { b.onclick = () => setStatus(b.dataset.done, "completed"); });
    $$(".cal-cell", body).forEach((c) => { c.ondblclick = () => editEvent(null, { event_date: c.dataset.day }); });
  }

  async function loadUpcoming() {
    try {
      const u = await api(`/api/calendar/upcoming?tz=${encodeURIComponent(TZ)}&days=60&limit=10`);
      $("#calOverdue").textContent = u.overdue ? t("cal.overdue", { n: u.overdue }) : "";
      mount($("#calUpcoming"), u.items.length ? html`<ul class="rows">${u.items.map((e) => html`<li>
        <div class="r-main"><button type="button" class="linklike r-title" data-up="${e.id}">${KIND_ICON[e.kind] || "•"} ${e.title}</button>
        <div class="r-meta">${fmtLong(e.event_date)} · ${when(e)}${e.case_title && e.source !== "case" ? ` · ${e.case_title}` : ""}</div></div></li>`)}</ul>`
        : html`<div class="empty">${t("cal.none_upcoming")}</div>`);
      $$("[data-up]", $("#calUpcoming")).forEach((b) => { b.onclick = () => showEvent(u.items.find((x) => x.id === b.dataset.up)); });
    } catch (err) { mount($("#calUpcoming"), html`<div class="card-b notice notice-red">${err.message}</div>`); }
  }

  // Desktop alerts: asked for only here, only when the person turns them on.
  function renderAlerts() {
    const box = $("#calAlerts");
    if (!("Notification" in window)) { box.textContent = t("cal.alerts_unsupported"); return; }
    const on = store.get("desktopAlerts", false) && Notification.permission === "granted";
    if (Notification.permission === "denied") { box.textContent = t("cal.alerts_blocked"); return; }
    mount(box, html`${on ? t("cal.alerts_on") : t("cal.alerts_hint")} <button type="button" class="linklike" id="alertsBtn">${on ? t("cal.alerts_turn_off") : t("cal.alerts_turn_on")}</button>`);
    $("#alertsBtn").onclick = async () => {
      if (on) { store.set("desktopAlerts", false); renderAlerts(); return; }
      const res = await Notification.requestPermission();
      store.set("desktopAlerts", res === "granted");
      toast(res === "granted" ? t("cal.alerts_on") : t("cal.alerts_blocked"), res !== "granted");
      renderAlerts();
    };
  }

  async function setStatus(id, status) {
    try { await api(`/api/calendar/events/${encodeURIComponent(id)}`, { method: "PATCH", body: { status } }); toast(t(`cal.done_${status}`)); load(); }
    catch (err) { toast(err.message, true); }
  }

  // ── Detail / edit dialogs ──
  function dialog(content) {
    $("#calDlg")?.remove();
    const d = document.createElement("dialog"); d.className = "dlg cal-dlg"; d.id = "calDlg";
    document.body.appendChild(d); mount(d, content); d.showModal();
    d.addEventListener("close", () => d.remove());
    $$("[data-close]", d).forEach((b) => { b.onclick = () => d.close(); });
    return d;
  }

  function showEvent(e) {
    if (!e) return;
    if (e.source === "case") {
      const d = dialog(html`<h2>⚖ ${e.title}</h2><p class="small muted">${t("cal.case_hearing_note")}</p>
        <dl class="kv small"><dt>${t("cal.date")}</dt><dd>${fmtLong(e.event_date)}</dd>${e.court ? html`<dt>${t("col.court")}</dt><dd>${e.court}</dd>` : ""}</dl>
        <div class="row" style="margin-top:14px"><a class="btn btn-ghost btn-sm" href="#/cases/${encodeURIComponent(e.case_id)}">${t("cal.open_case")}</a>
          <button class="btn btn-sm" id="addRem">🔔 ${t("cal.add_reminder")}</button><span class="spacer"></span><button class="btn btn-ghost btn-sm" data-close>${t("common.close")}</button></div>`);
      $("#addRem", d).onclick = () => { d.close(); editEvent(null, { kind: "hearing", case_id: e.case_id, event_date: e.event_date, title: `${t("cal.kind_hearing")} — ${e.title}`, priority: "high" }); };
      return;
    }
    const rs = (e.reminder_status || []).filter((x) => x.channel === "in_app" || e.email_reminders);
    const d = dialog(html`<h2>${KIND_ICON[e.kind] || ""} ${e.title}</h2>
      <div class="row small" style="margin-bottom:8px"><span class="chip">${kindLabel(e.kind)}</span>${e.priority === "high" ? html`<span class="chip chip-warn">${t("cal.p_high")}</span>` : ""}
        <span class="chip ${e.status === "completed" ? "chip-ok" : e.status === "cancelled" ? "" : "chip-wait"}">${t(`cal.st_${e.status}`)}</span></div>
      <dl class="kv small"><dt>${t("cal.date")}</dt><dd>${fmtLong(e.event_date)} · ${when(e)}</dd>
        ${e.case_id ? html`<dt>${t("cal.case")}</dt><dd><a href="#/cases/${encodeURIComponent(e.case_id)}">${e.case_title || e.case_id}</a></dd>` : ""}
        ${e.description ? html`<dt>${t("cal.description")}</dt><dd style="white-space:pre-wrap">${e.description}</dd>` : ""}
        <dt>${t("cal.reminders")}</dt><dd>${rs.length ? html`<ul class="list-plain">${rs.map((x) => html`<li>${t(`cal.r_${x.rule}`)} · ${x.channel === "email" ? t("cal.by_email") : t("cal.in_app")} —
          <strong>${t(`cal.rs_${x.status}`)}</strong> <span class="muted">${x.status === "sent" ? new Date(`${x.sent_at}Z`).toLocaleString(loc()) : new Date(`${x.remind_at_utc}Z`).toLocaleString(loc())}</span>
          ${x.error && x.status !== "sent" ? html`<div class="muted">${x.error}</div>` : ""}</li>`)}</ul>` : t("cal.no_reminders")}</dd></dl>
      <div class="row" style="margin-top:14px">
        ${e.status === "scheduled" ? html`<button class="btn btn-sm" data-act="completed">✓ ${t("cal.complete")}</button>` : html`<button class="btn btn-ghost btn-sm" data-act="scheduled">↺ ${t("cal.reopen")}</button>`}
        <button class="btn btn-ghost btn-sm" id="evEdit">✎ ${t("cal.edit")}</button>
        ${e.status === "scheduled" ? html`<button class="btn btn-ghost btn-sm" id="evMove">📅 ${t("cal.reschedule")}</button><button class="btn btn-ghost btn-sm" data-act="cancelled">${t("cal.cancel_event")}</button>` : ""}
        <span class="spacer"></span><button class="btn btn-danger btn-sm" id="evDel">${t("common.delete")}</button><button class="btn btn-ghost btn-sm" data-close>${t("common.close")}</button></div>`);
    $$("[data-act]", d).forEach((b) => { b.onclick = () => { d.close(); setStatus(e.id, b.dataset.act); }; });
    $("#evEdit", d).onclick = () => { d.close(); editEvent(e); };
    $("#evMove", d)?.addEventListener("click", () => { d.close(); editEvent(e, {}, true); });
    $("#evDel", d).onclick = async () => {
      if (!confirm(t("cal.confirm_delete"))) return;
      try { await api(`/api/calendar/events/${encodeURIComponent(e.id)}`, { method: "DELETE" }); d.close(); toast(t("cal.deleted")); load(); }
      catch (err) { toast(err.message, true); }
    };
  }

  function editEvent(e, preset = {}, focusDate = false) {
    const v = { kind: "task", priority: "normal", reminders: cfg.default_reminders, email_reminders: false, event_time: "", description: "", case_id: "", title: "", ...(e || {}), ...preset };
    const d = dialog(html`<h2>${e ? t("cal.edit_title") : t("cal.new_title")}</h2>
      <form class="stack" id="evForm" novalidate>
        <div><label class="f" for="ev_title">${t("cal.f_title")} *</label><input id="ev_title" maxlength="200" required value="${v.title}"></div>
        <div class="form-grid cal-form">
          <div><label class="f" for="ev_kind">${t("cal.f_kind")}</label><select id="ev_kind">${cfg.kinds.map((k) => html`<option value="${k}" ${k === v.kind ? raw("selected") : ""}>${KIND_ICON[k]} ${kindLabel(k)}</option>`)}</select></div>
          <div><label class="f" for="ev_date">${t("cal.date")} *</label><input type="date" id="ev_date" required value="${v.event_date || todayIso()}"></div>
          <div><label class="f" for="ev_time">${t("cal.f_time")}</label><input type="time" id="ev_time" value="${v.event_time || ""}"><div class="hint">${t("cal.f_time_hint")}</div></div>
          <div class="span-2"><label class="f" for="ev_case">${t("cal.case")}</label><select id="ev_case"><option value="">${t("cal.no_case")}</option>
            ${myCases.map((c) => html`<option value="${c.id}" ${c.id === v.case_id ? raw("selected") : ""}>${c.title}</option>`)}</select></div>
          <div><label class="f" for="ev_pri">${t("cal.f_priority")}</label><select id="ev_pri">${cfg.priorities.map((p) => html`<option value="${p}" ${p === v.priority ? raw("selected") : ""}>${t(`cal.p_${p}`)}</option>`)}</select></div>
        </div>
        <div><label class="f" for="ev_desc">${t("cal.description")}</label><textarea id="ev_desc" rows="3" maxlength="4000">${v.description || ""}</textarea></div>
        <fieldset class="fs"><legend>${t("cal.reminders")}</legend>
          ${cfg.reminders.map((rr) => html`<label class="check"><input type="checkbox" name="rem" value="${rr}" ${v.reminders.includes(rr) ? raw("checked") : ""}> ${t(`cal.r_${rr}`)}</label>`)}
          <div style="margin-top:6px"><label class="check"><input type="checkbox" id="ev_email" ${v.email_reminders && cfg.email_available ? raw("checked") : ""} ${cfg.email_available ? "" : raw("disabled")}> ${t("cal.also_email")}</label>
            ${cfg.email_available ? "" : html`<div class="hint">${t("cal.email_off")}</div>`}</div>
          <div class="hint">${t("cal.tz_note", { tz: e?.timezone || TZ })}</div></fieldset>
        <div class="row"><button class="btn" type="submit">${t("common.save")}</button><button class="btn btn-ghost" type="button" data-close>${t("common.cancel")}</button></div>
      </form>`);
    (focusDate ? $("#ev_date", d) : $("#ev_title", d)).focus();
    $("#evForm", d).onsubmit = async (ev) => {
      ev.preventDefault();
      const body = { title: $("#ev_title", d).value.trim(), kind: $("#ev_kind", d).value, event_date: $("#ev_date", d).value,
        event_time: $("#ev_time", d).value || null, case_id: $("#ev_case", d).value || null, priority: $("#ev_pri", d).value,
        description: $("#ev_desc", d).value.trim(), reminders: $$("input[name=rem]:checked", d).map((x) => x.value),
        email_reminders: $("#ev_email", d).checked, timezone: e?.timezone || TZ };
      if (!body.title) { toast(t("cal.need_title"), true); $("#ev_title", d).focus(); return; }
      if (!body.event_date) { toast(t("cal.need_date"), true); $("#ev_date", d).focus(); return; }
      try {
        const saved = await api(e ? `/api/calendar/events/${encodeURIComponent(e.id)}` : "/api/calendar/events", { method: e ? "PATCH" : "POST", body });
        d.close(); toast(e ? t("common.saved") : t("cal.created"));
        cursor = saved.event_date; load();
      } catch (err) { toast(err.message, true); }
    };
  }

  renderAlerts();
  await load();
  const open = r.q.get("event");
  if (open) {
    try { const e = await api(`/api/calendar/events/${encodeURIComponent(open)}`); cursor = e.event_date; await load(); showEvent(e); }
    catch (err) { toast(err.message, true); }
  }
}

/** Compact upcoming list for the dashboard (hearings from case files + the person's own entries). */
export async function upcomingCard(el) {
  try {
    const u = await api(`/api/calendar/upcoming?tz=${encodeURIComponent(TZ)}&days=45&limit=8`);
    mount(el, u.items.length ? html`<div class="table-wrap"><table class="gov"><thead><tr><th>${t("col.date")}</th><th>${t("cal.item")}</th><th>${t("col.type")}</th><th>${t("col.matter")}</th></tr></thead><tbody>
      ${u.items.map((e) => html`<tr class="clickable" data-href="${e.source === "case" ? `#/cases/${encodeURIComponent(e.case_id)}` : `#/calendar?event=${encodeURIComponent(e.id)}`}">
        <td><strong>${fmtLong(e.event_date)}</strong><br><span class="small muted">${when(e)}</span></td><td>${e.title}</td>
        <td><span class="chip">${KIND_ICON[e.kind] || ""} ${kindLabel(e.kind)}</span></td><td class="small">${e.source === "case" ? e.case_title : e.case_title || "—"}</td></tr>`)}</tbody></table></div>
      ${u.overdue ? html`<div class="card-b small"><a href="#/calendar">${t("cal.overdue", { n: u.overdue })} →</a></div>` : ""}`
      : html`<div class="empty">${t("cal.none_upcoming")} <a href="#/calendar">${t("cal.add_one")}</a></div>`);
    $$("tr[data-href]", el).forEach((tr) => { tr.onclick = () => { location.hash = tr.dataset.href; }; });
  } catch (err) { mount(el, html`<div class="card-b notice notice-red">${err.message}</div>`); }
}

/** Desktop alert for new reminder notifications (only if the person turned alerts on and the browser allows). */
export function desktopAlerts(items) {
  if (!("Notification" in window) || Notification.permission !== "granted" || !store.get("desktopAlerts", false)) return;
  const shown = new Set(store.get("alerted", []));
  for (const n of items.filter((x) => x.kind === "reminder" && !x.read_at && !shown.has(x.id))) {
    try {
      const note = new Notification(n.title, { body: n.body || "", tag: `nyaya-${n.id}` });
      note.onclick = () => { window.focus(); if (n.link) location.hash = n.link.replace(/^#/, "#"); note.close(); };
    } catch { /* the browser refused */ }
    shown.add(n.id);
  }
  store.set("alerted", [...shown].slice(-100));
}
