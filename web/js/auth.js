/* Accounts: sign-in, two-step verification (TOTP), recovery codes, sign-up, password reset, account settings,
   user administration and case members. The browser never sees Supabase keys or tokens — every call goes to the
   NyaySutra backend, which holds the session server-side. What a user may do is decided by the server; hiding a
   button here is only for clarity. */
import { $, $$, api, confirmDialog, crumbs, fmtDateTime, html, initSession, mount, raw, spinner, state, toast } from "./core.js";
import { t } from "./i18n.js";

export const can = (perm) => Boolean(state.session?.permissions?.includes(perm));
const isSupabase = () => state.session?.auth_mode === "supabase";

/** Show only the navigation the role can use (the server enforces the same rules). */
export function applyNavPermissions() {
  $$("[data-perm]").forEach((el) => { el.classList.toggle("hidden", !el.dataset.perm.split("|").some(can)); });
  const s = state.session || {};
  $("#logoutBtn").classList.toggle("hidden", !(s.user && (isSupabase() || s.password_required)));
  $("#accountLink").classList.toggle("hidden", !(s.authenticated && isSupabase()));
  $("#signinLink").classList.toggle("hidden", Boolean(s.user) || !isSupabase());
  if (s.authenticated && s.user) $("#accountLink").textContent = `👤 ${s.user.display_name || s.user.email || ""}`;
}

async function afterSignIn(r, go) {
  state.csrf = r.csrf;
  state.session = await initSession();
  applyNavPermissions();
  if (r.state === "mfa_pending") return renderMfa();
  if (r.state === "mfa_enroll") return renderEnroll(true);
  location.hash = "#/dashboard"; go();
}

const card = (title, body) => html`<div class="card" style="max-width:460px;margin:32px auto">
  <div class="card-h"><h2>${title}</h2></div><div class="card-b">${body}</div></div>`;
const pwRules = () => html`<div class="hint">${t("auth.pw_rules")}</div>`;

// ── Sign in ──
export function renderLogin(go, q = new URLSearchParams()) {
  crumbs([[t("login.title")]]);
  const local = !isSupabase();
  const err = q.get("e");
  mount($("#main"), card(t("login.title"), html`
    ${err ? html`<div class="notice notice-red" style="margin-bottom:10px">${t(`auth.link_${err}`, {}, t("auth.link_error"))}</div>` : ""}
    ${q.get("reset") ? html`<div class="notice notice-green" style="margin-bottom:10px">${t("auth.reset_done")}</div>` : ""}
    <form class="stack" id="loginForm">
      ${local ? "" : html`<div><label class="f" for="em">${t("auth.email")}</label><input type="email" id="em" autocomplete="username" required maxlength="254"></div>`}
      <div><label class="f" for="pw">${t("login.password")}</label><input type="password" id="pw" autocomplete="current-password" required maxlength="200"></div>
      <button class="btn" type="submit">${t("login.submit")}</button></form>
    ${local ? "" : html`<div class="row small" style="margin-top:12px;justify-content:space-between">
      <a href="#/forgot">${t("auth.forgot")}</a><a href="#/signup">${t("auth.create")}</a></div>`}
    <hr><div class="small muted">${t("auth.public")} <a href="#/acts">${t("nav.acts")}</a> · <a href="#/awareness">${t("nav.awareness")}</a> · <a href="#/limitation">${t("nav.limitation")}</a></div>`));
  $("#loginForm").onsubmit = async (e) => {
    e.preventDefault();
    const body = local ? { password: $("#pw").value } : { email: $("#em").value.trim(), password: $("#pw").value };
    try { await afterSignIn(await api("/api/auth/login", { method: "POST", body, retry: false }), go); }
    catch (err2) { $("#pw").value = ""; toast(err2.message, true); }
  };
  (local ? $("#pw") : $("#em")).focus();
}

// ── Second factor at sign-in ──
export function renderMfa(go) {
  crumbs([[t("auth.mfa_title")]]);
  mount($("#main"), card(t("auth.mfa_title"), html`<p class="small">${t("auth.mfa_prompt")}</p>
    <form class="stack" id="mfaForm"><div><label class="f" for="code">${t("auth.code")}</label>
      <input id="code" inputmode="numeric" autocomplete="one-time-code" pattern="[0-9 ]{6,7}" maxlength="7" required></div>
      <button class="btn" type="submit">${t("auth.verify")}</button></form>
    <details style="margin-top:12px"><summary class="small">${t("auth.lost_device")}</summary>
      <form class="stack" id="rcForm" style="margin-top:8px"><div class="small muted">${t("auth.recovery_hint")}</div>
        <input id="rc" autocomplete="off" maxlength="20" placeholder="XXXXX-XXXXX" required>
        <button class="btn btn-ghost btn-sm" type="submit">${t("auth.use_recovery")}</button></form></details>
    <div style="margin-top:12px"><button class="btn btn-ghost btn-sm" id="mfaCancel">${t("common.cancel")}</button></div>`));
  const done = async (r) => { state.csrf = r.csrf; state.session = await initSession(); applyNavPermissions();
    if (r.state === "mfa_enroll") return renderEnroll(true, go); location.hash = "#/dashboard"; (go || (() => location.reload()))(); };
  $("#mfaForm").onsubmit = async (e) => { e.preventDefault();
    try { await done(await api("/api/auth/mfa/verify", { method: "POST", body: { code: $("#code").value }, retry: false })); }
    catch (err) { $("#code").value = ""; toast(err.message, true); if (err.status === 401 && /sign in again/i.test(err.message)) location.reload(); } };
  $("#rcForm").onsubmit = async (e) => { e.preventDefault();
    try { await done(await api("/api/auth/mfa/recovery", { method: "POST", body: { code: $("#rc").value }, retry: false })); toast(t("auth.recovery_used")); }
    catch (err) { toast(err.message, true); } };
  $("#mfaCancel").onclick = async () => { await api("/api/auth/logout", { method: "POST" }); location.reload(); };
  $("#code").focus();
}

// ── Set up an authenticator app (required for some roles; optional for others) ──
export async function renderEnroll(required, go, el = $("#main")) {
  if (el === $("#main")) crumbs([[t("auth.enroll_title")]]);
  mount(el, spinner());
  let f;
  try { f = await api("/api/auth/mfa/enroll", { method: "POST" }); } catch (err) { mount(el, html`<div class="notice notice-red">${err.message}</div>`); return; }
  mount(el, card(t("auth.enroll_title"), html`
    ${required ? html`<div class="notice" style="margin-bottom:10px">${t("auth.enroll_required")}</div>` : ""}
    <ol class="small"><li>${t("auth.enroll_step1")}</li><li>${t("auth.enroll_step2")}</li></ol>
    <div style="text-align:center"><img src="${f.qr_code}" alt="${t("auth.qr_alt")}" width="200" height="200" style="background:#fff;padding:6px"></div>
    <details><summary class="small">${t("auth.cant_scan")}</summary><code style="word-break:break-all">${f.secret}</code></details>
    <form class="stack" id="enForm" style="margin-top:10px"><div><label class="f" for="ecode">${t("auth.code")}</label>
      <input id="ecode" inputmode="numeric" autocomplete="one-time-code" maxlength="7" required></div>
      <button class="btn" type="submit">${t("auth.enable")}</button></form>`));
  $("#enForm").onsubmit = async (e) => {
    e.preventDefault();
    try {
      const r = await api("/api/auth/mfa/enroll/verify", { method: "POST", body: { factor_id: f.factor_id, code: $("#ecode").value }, retry: false });
      state.csrf = r.csrf; state.session = await initSession(); applyNavPermissions();
      showRecoveryCodes(el, r.recovery_codes, () => { location.hash = required ? "#/dashboard" : "#/account"; (go || (() => location.reload()))(); });
    } catch (err) { $("#ecode").value = ""; toast(err.message, true); }
  };
}

function showRecoveryCodes(el, codes, next) {
  mount(el, card(t("auth.codes_title"), html`<div class="notice" style="margin-bottom:10px">${t("auth.codes_hint")}</div>
    <pre style="font-size:1rem;line-height:1.7;text-align:center">${codes.join("\n")}</pre>
    <div class="row"><button class="btn btn-ghost btn-sm" id="dlCodes">${t("auth.codes_download")}</button>
      <span class="spacer"></span><button class="btn" id="codesDone">${t("auth.codes_saved")}</button></div>`));
  $("#dlCodes").onclick = () => {
    const url = URL.createObjectURL(new Blob([`NyaySutra AI recovery codes\n\n${codes.join("\n")}\n`], { type: "text/plain" }));
    const a = document.createElement("a"); a.href = url; a.download = "nyaysutra-recovery-codes.txt"; document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  };
  $("#codesDone").onclick = next;
}

// ── Register / forgot / reset ──
export function renderSignup(go, q) {
  crumbs([[t("auth.create")]]);
  mount($("#main"), card(t("auth.create"), html`
    ${state.session?.signup_mode === "invite_only" ? html`<div class="notice" style="margin-bottom:10px">${t("auth.invite_only")}</div>` : ""}
    <form class="stack" id="suForm">
      <div><label class="f" for="sn">${t("auth.name")}</label><input id="sn" autocomplete="name" maxlength="80" required></div>
      <div><label class="f" for="se">${t("auth.email")}</label><input type="email" id="se" autocomplete="email" maxlength="254" required value="${q.get("email") || ""}"></div>
      <div><label class="f" for="sp">${t("login.password")}</label><input type="password" id="sp" autocomplete="new-password" minlength="12" maxlength="72" required>${pwRules()}</div>
      <div><label class="f" for="sp2">${t("auth.confirm_pw")}</label><input type="password" id="sp2" autocomplete="new-password" maxlength="72" required></div>
      <button class="btn" type="submit">${t("auth.create")}</button></form>
    <div class="small" style="margin-top:12px"><a href="#/login">${t("auth.have_account")}</a></div>`));
  $("#suForm").onsubmit = async (e) => {
    e.preventDefault();
    if ($("#sp").value !== $("#sp2").value) { toast(t("auth.pw_mismatch"), true); return; }
    try {
      const r = await api("/api/auth/signup", { method: "POST", body: { email: $("#se").value.trim(), password: $("#sp").value, display_name: $("#sn").value.trim() }, retry: false });
      mount($("#main"), card(t("auth.check_email"), html`<p>${r.message}</p><p class="small muted">${t("auth.same_browser")}</p>
        <button class="btn btn-ghost btn-sm" id="resend">${t("auth.resend")}</button>`));
      const email = $("#se")?.value;
      $("#resend").onclick = async () => { try { toast((await api("/api/auth/resend", { method: "POST", body: { email: email || q.get("email") || "" } })).message); } catch (err) { toast(err.message, true); } };
    } catch (err) { toast(err.message, true); }
  };
}

export function renderForgot() {
  crumbs([[t("auth.forgot")]]);
  mount($("#main"), card(t("auth.forgot"), html`<p class="small">${t("auth.forgot_hint")}</p>
    <form class="stack" id="fgForm"><div><label class="f" for="fe">${t("auth.email")}</label><input type="email" id="fe" autocomplete="email" maxlength="254" required></div>
      <button class="btn" type="submit">${t("auth.send_link")}</button></form>`));
  $("#fgForm").onsubmit = async (e) => {
    e.preventDefault();
    try { const r = await api("/api/auth/password/forgot", { method: "POST", body: { email: $("#fe").value.trim() }, retry: false });
      mount($("#main"), card(t("auth.check_email"), html`<p>${r.message}</p><p class="small muted">${t("auth.same_browser")}</p>`)); }
    catch (err) { toast(err.message, true); }
  };
}

export function renderReset() {
  crumbs([[t("auth.reset_title")]]);
  if (state.session?.state !== "recovery") {
    mount($("#main"), card(t("auth.reset_title"), html`<p>${t("auth.reset_expired")}</p><a class="btn btn-sm" href="#/forgot">${t("auth.send_link")}</a>`));
    return;
  }
  mount($("#main"), card(t("auth.reset_title"), html`<form class="stack" id="rsForm">
    <div><label class="f" for="rp">${t("auth.new_pw")}</label><input type="password" id="rp" autocomplete="new-password" minlength="12" maxlength="72" required>${pwRules()}</div>
    <div><label class="f" for="rp2">${t("auth.confirm_pw")}</label><input type="password" id="rp2" autocomplete="new-password" maxlength="72" required></div>
    <button class="btn" type="submit">${t("auth.set_pw")}</button></form>`));
  $("#rsForm").onsubmit = async (e) => {
    e.preventDefault();
    if ($("#rp").value !== $("#rp2").value) { toast(t("auth.pw_mismatch"), true); return; }
    try { await api("/api/auth/password/reset", { method: "POST", body: { password: $("#rp").value }, retry: false });
      location.hash = "#/login?reset=1"; location.reload(); }
    catch (err) { toast(err.message, true); }
  };
}

// ── Account settings ──
export async function viewAccount(main) {
  crumbs([[t("auth.account")]]);
  const a = await api("/api/account");
  const roleName = t(`role_name.${a.role}`, {}, a.role);
  mount(main, html`<h1 class="page-title">${t("auth.account")}</h1>
    <div class="grid g-2">
      <div class="card"><div class="card-h"><h2>${t("auth.profile")}</h2></div><form class="card-b stack" id="profForm">
        <div class="small muted">${a.user.email || ""} · ${t("auth.role")}: <strong>${roleName}</strong></div>
        <div><label class="f" for="dn">${t("auth.name")}</label><input id="dn" maxlength="80" required value="${a.user.display_name || ""}"></div>
        <div><button class="btn btn-sm" type="submit">${t("common.save")}</button></div></form></div>
      ${a.auth_mode === "supabase" ? html`<div class="card"><div class="card-h"><h2>${t("auth.change_pw")}</h2></div><form class="card-b stack" id="pwForm">
        <div><label class="f" for="cp">${t("auth.current_pw")}</label><input type="password" id="cp" autocomplete="current-password" maxlength="200" required></div>
        <div><label class="f" for="np">${t("auth.new_pw")}</label><input type="password" id="np" autocomplete="new-password" minlength="12" maxlength="72" required>${pwRules()}</div>
        <div><button class="btn btn-sm" type="submit">${t("auth.change_pw")}</button></div></form></div>` : ""}
      ${a.auth_mode === "supabase" ? html`<div class="card"><div class="card-h"><h2>${t("auth.mfa")}</h2></div><div class="card-b stack" id="mfaCard">
        <div>${a.mfa.enabled ? html`<span class="chip chip-ok">${t("auth.mfa_on")}</span> <span class="small muted">${t("auth.codes_left", { n: a.mfa.recovery_codes_left })}</span>`
          : html`<span class="chip chip-warn">${t("auth.mfa_off")}</span>`} ${a.mfa.required ? html`<span class="small muted">${t("auth.mfa_required_role")}</span>` : ""}</div>
        ${a.mfa.enabled ? html`<form class="row" id="mfaAct"><input id="mcode" inputmode="numeric" maxlength="7" placeholder="${t("auth.code")}" aria-label="${t("auth.code")}" style="max-width:140px" required>
            <button class="btn btn-ghost btn-sm" type="submit" data-act="codes">${t("auth.new_codes")}</button>
            ${a.mfa.required ? "" : html`<button class="btn btn-danger btn-sm" type="submit" data-act="off">${t("auth.mfa_disable")}</button>`}</form>`
          : html`<div><button class="btn btn-sm" id="mfaOn">${t("auth.mfa_enable")}</button></div>`}</div></div>` : ""}
      <div class="card"><div class="card-h"><h2>${t("auth.sessions")}</h2>${a.sessions.length > 1 ? html`<button class="btn btn-ghost btn-sm" id="revokeOthers">${t("auth.revoke_others")}</button>` : ""}</div>
        <div class="table-wrap"><table class="gov"><thead><tr><th>${t("auth.device")}</th><th>${t("auth.last_active")}</th><th></th></tr></thead><tbody>
        ${a.sessions.map((s) => html`<tr><td class="small">${(s.user_agent || "—").slice(0, 70)}<br><span class="muted">${s.ip || ""}</span></td>
          <td class="small">${fmtDateTime(s.last_seen_at)}</td>
          <td>${s.current ? html`<span class="chip chip-ok">${t("auth.this_session")}</span>` : html`<button class="btn btn-ghost btn-sm" data-revoke="${s.public_id}">${t("auth.revoke")}</button>`}</td></tr>`)}
        </tbody></table></div></div>
    </div>`);
  $("#profForm").onsubmit = async (e) => { e.preventDefault();
    try { await api("/api/account", { method: "PATCH", body: { display_name: $("#dn").value.trim() } }); state.session = await initSession(); applyNavPermissions(); toast(t("common.saved")); }
    catch (err) { toast(err.message, true); } };
  $("#pwForm")?.addEventListener("submit", async (e) => { e.preventDefault();
    try { const r = await api("/api/auth/password/change", { method: "POST", body: { current_password: $("#cp").value, new_password: $("#np").value } });
      state.csrf = (await initSession()).csrf || state.csrf; toast(t("auth.pw_changed")); viewAccount(main); void r; }
    catch (err) { toast(err.message, true); } });
  $("#mfaOn")?.addEventListener("click", () => renderEnroll(false, () => viewAccount(main), $("#mfaCard")));
  $("#mfaAct")?.addEventListener("submit", async (e) => { e.preventDefault();
    const act = e.submitter?.dataset.act; const code = $("#mcode").value;
    try {
      if (act === "off") {
        if (!await confirmDialog(t("auth.mfa_disable"), t("auth.mfa_disable_confirm"), t("auth.mfa_disable"))) return;
        await api("/api/auth/mfa/disable", { method: "POST", body: { code } }); toast(t("auth.mfa_off")); viewAccount(main);
      } else {
        const r = await api("/api/auth/mfa/recovery-codes", { method: "POST", body: { code } });
        showRecoveryCodes($("#mfaCard"), r.recovery_codes, () => viewAccount(main));
      }
    } catch (err) { toast(err.message, true); } });
  $$("[data-revoke]", main).forEach((b) => { b.onclick = async () => {
    try { await api(`/api/auth/sessions/${encodeURIComponent(b.dataset.revoke)}`, { method: "DELETE" }); toast(t("auth.revoked")); viewAccount(main); } catch (err) { toast(err.message, true); } }; });
  $("#revokeOthers")?.addEventListener("click", async () => {
    try { const r = await api("/api/auth/sessions/revoke-others", { method: "POST" }); toast(t("auth.revoked_n", { n: r.revoked })); viewAccount(main); } catch (err) { toast(err.message, true); } });
}

// ── Administration ──
export async function viewAdmin(main) {
  crumbs([[t("admin.title")]]);
  if (!can("users.manage")) { location.hash = "#/updates?tab=activity"; return; }
  const [{ users, roles }, { invitations }] = await Promise.all([api("/api/admin/users"), api("/api/admin/invitations")]);
  const me = state.session.user.id;
  const roleOpts = (sel) => roles.map((r) => html`<option value="${r}" ${r === sel ? raw("selected") : ""}>${t(`role_name.${r}`, {}, r)}</option>`);
  mount(main, html`<h1 class="page-title">${t("admin.title")}</h1><p class="page-lead">${t("admin.lead")}</p>
    <div class="card"><div class="card-h"><h2>${t("admin.users", { n: users.length })}</h2><a class="small" href="#/updates?tab=activity">${t("admin.audit")} →</a></div>
      <div class="table-wrap"><table class="gov"><thead><tr><th>${t("auth.name")}</th><th>${t("auth.role")}</th><th>${t("admin.status")}</th><th>${t("auth.mfa")}</th><th>${t("auth.last_active")}</th></tr></thead><tbody>
      ${users.map((u) => html`<tr><td><strong>${u.display_name || "—"}</strong><br><span class="small muted">${u.email}</span></td>
        <td>${u.user_id === me ? t(`role_name.${u.role}`, {}, u.role) : html`<select data-role="${u.user_id}" aria-label="${t("auth.role")}">${roleOpts(u.role)}</select>`}</td>
        <td>${u.user_id === me ? t(`admin.st_${u.status}`) : html`<button class="btn btn-ghost btn-sm" data-status="${u.user_id}" data-to="${u.status === "active" ? "disabled" : "active"}">${u.status === "active" ? t("admin.disable") : t("admin.enable")}</button>`}</td>
        <td>${u.mfa_enabled ? "✓" : "—"}</td><td class="small">${fmtDateTime(u.last_seen)}</td></tr>`)}
      </tbody></table></div></div>
    <div class="grid g-2" style="margin-top:16px">
      <div class="card"><div class="card-h"><h2>${t("admin.invite")}</h2></div><form class="card-b stack" id="invForm">
        <div class="small muted">${t("admin.invite_hint")}</div>
        <div><label class="f" for="ie">${t("auth.email")}</label><input type="email" id="ie" maxlength="254" required></div>
        <div><label class="f" for="ir">${t("auth.role")}</label><select id="ir">${roleOpts("client")}</select></div>
        <div><button class="btn btn-sm" type="submit">${t("admin.invite")}</button></div><div id="invLink"></div></form></div>
      <div class="card"><div class="card-h"><h2>${t("admin.pending")}</h2></div><div class="card-b">
        ${invitations.filter((i) => !i.accepted_at).length ? html`<ul class="list-plain">${invitations.filter((i) => !i.accepted_at).map((i) => html`<li>${i.email} · ${t(`role_name.${i.role}`, {}, i.role)}
          <span class="small muted">${t("admin.expires")} ${fmtDateTime(i.expires_at)}</span> <button class="btn btn-ghost btn-sm" data-uninvite="${i.id}">${t("admin.revoke")}</button></li>`)}</ul>`
          : html`<div class="empty">${t("admin.none_pending")}</div>`}</div></div>
    </div>`);
  $$("[data-role]", main).forEach((s) => { s.onchange = async () => {
    try { await api(`/api/admin/users/${encodeURIComponent(s.dataset.role)}`, { method: "PATCH", body: { role: s.value } }); toast(t("common.saved")); }
    catch (err) { toast(err.message, true); viewAdmin(main); } }; });
  $$("[data-status]", main).forEach((b) => { b.onclick = async () => {
    if (b.dataset.to === "disabled" && !await confirmDialog(t("admin.disable"), t("admin.disable_confirm"), t("admin.disable"))) return;
    try { await api(`/api/admin/users/${encodeURIComponent(b.dataset.status)}`, { method: "PATCH", body: { status: b.dataset.to } }); viewAdmin(main); }
    catch (err) { toast(err.message, true); } }; });
  $("#invForm").onsubmit = async (e) => { e.preventDefault();
    try { const r = await api("/api/admin/invitations", { method: "POST", body: { email: $("#ie").value.trim(), role: $("#ir").value } });
      mount($("#invLink"), html`<div class="notice notice-green small">${t("admin.invited")} <code style="word-break:break-all">${r.signup_link}</code></div>`); }
    catch (err) { toast(err.message, true); } };
  $$("[data-uninvite]", main).forEach((b) => { b.onclick = async () => {
    try { await api(`/api/admin/invitations/${encodeURIComponent(b.dataset.uninvite)}`, { method: "DELETE" }); viewAdmin(main); } catch (err) { toast(err.message, true); } }; });
}

// ── People on a case ──
export async function membersCard(el, caseId) {
  let data;
  try { data = await api(`/api/cases/${encodeURIComponent(caseId)}/members`); } catch { el.innerHTML = ""; return; }
  const me = state.session?.user?.id;
  mount(el, html`<div class="card"><div class="card-h"><h2>${t("members.title")}</h2></div><div class="card-b">
    <ul class="list-plain">${data.members.map((m) => html`<li><strong>${m.display_name || m.email || "—"}</strong> <span class="chip">${t(`members.${m.access}`, {}, m.access)}</span>
      <span class="small muted">${t(`role_name.${m.role}`, {}, m.role || "")}</span>
      ${data.can_manage && m.user_id !== me ? html` <button class="btn btn-ghost btn-sm" data-unmember="${m.user_id}">${t("members.remove")}</button>` : ""}</li>`)}</ul>
    ${data.can_manage ? html`<form class="stack" id="memForm" style="margin-top:10px"><div class="small muted">${t("members.add_hint")}</div>
      <input id="memQ" placeholder="${t("members.search_ph")}" aria-label="${t("members.search_ph")}" autocomplete="off" list="memList">
      <datalist id="memList"></datalist>
      <div class="row"><select id="memAccess" aria-label="${t("members.access")}"><option value="advocate">${t("members.advocate")}</option><option value="client">${t("members.client")}</option><option value="owner">${t("members.owner")}</option></select>
        <button class="btn btn-sm" type="submit">${t("members.add")}</button></div></form>` : ""}
  </div></div>`);
  let timer;
  $("#memQ", el)?.addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(async () => {
    try { const { people } = await api(`/api/org/directory?q=${encodeURIComponent($("#memQ", el).value)}`);
      mount($("#memList", el), html`${people.map((p) => html`<option value="${p.email}">${p.display_name} (${t(`role_name.${p.role}`, {}, p.role)})</option>`)}`); } catch { /* directory needs advocate/admin */ }
  }, 250); });
  $("#memForm", el)?.addEventListener("submit", async (e) => { e.preventDefault();
    try { await api(`/api/cases/${encodeURIComponent(caseId)}/members`, { method: "POST", body: { email: $("#memQ", el).value.trim(), access: $("#memAccess", el).value } });
      toast(t("common.saved")); membersCard(el, caseId); } catch (err) { toast(err.message, true); } });
  $$("[data-unmember]", el).forEach((b) => { b.onclick = async () => {
    try { await api(`/api/cases/${encodeURIComponent(caseId)}/members/${encodeURIComponent(b.dataset.unmember)}`, { method: "DELETE" }); membersCard(el, caseId); }
    catch (err) { toast(err.message, true); } }; });
}
