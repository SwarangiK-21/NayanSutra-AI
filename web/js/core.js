/* NyaySutra AI — shared frontend core: safe templating, API client, Markdown, UI helpers.
   No external libraries; CSP allows only same-origin scripts. */
import { t } from "./i18n.js";

// ── Safe HTML templating: every interpolated value is escaped unless wrapped in raw() ──
const RAW = Symbol("raw");
const ESC = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
export const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ESC[c]);
export const raw = (s) => ({ [RAW]: String(s) });
const val = (v) => v == null || v === false ? "" : Array.isArray(v) ? v.map(val).join("")
  : typeof v === "object" && RAW in v ? v[RAW] : esc(v);
export const html = (strings, ...vals) => raw(strings.reduce((o, s, i) => o + s + (i < vals.length ? val(vals[i]) : ""), ""));
export const mount = (el, tpl) => { el.innerHTML = tpl[RAW]; return el; };
export const $ = (s, el = document) => el.querySelector(s);
export const $$ = (s, el = document) => [...el.querySelectorAll(s)];

export const store = {
  get(k, d) { try { const v = localStorage.getItem("nyaya." + k); return v === null ? d : JSON.parse(v); } catch { return d; } },
  set(k, v) { try { localStorage.setItem("nyaya." + k, JSON.stringify(v)); } catch { /* storage unavailable */ } },
};

export const state = { csrf: null, status: null, caseId: store.get("caseId", null), chatId: null, scope: store.get("scope", "all"),
  streaming: false, pollTimer: null, msgSources: new Map(), msgSeq: 0, prefs: {}, ref: null };

export const TYPE_BADGE = { Civil: "b-civil", Criminal: "b-criminal", Family: "b-family", Revenue: "b-revenue", Consumer: "b-consumer", Writ: "b-writ" };
export const COLL_LABEL = () => ({ case: t("coll.case"), bare_act: t("coll.bare_act"), judgment: t("coll.judgment"), draft: t("coll.draft") });
const LOCALE = { en: "en-IN", hi: "hi-IN", mr: "mr-IN" };
export const locale = () => LOCALE[document.documentElement.lang] || "en-IN";
export const fmtDate = (d) => { if (!d) return "—"; const x = new Date(d); return isNaN(x) ? d : x.toLocaleDateString(locale(), { day: "2-digit", month: "short", year: "numeric" }); };
export const fmtDateTime = (d) => { if (!d) return "—"; const x = new Date(d); return isNaN(x) ? d : x.toLocaleString(locale(), { day: "2-digit", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit" }); };
export const daysTo = (d) => Math.round((new Date(d) - new Date(new Date().toDateString())) / 864e5);
export const num = (n) => Number(n || 0).toLocaleString(locale());

// ── Toast ──
let toastT;
export function toast(msg, err = false) {
  const el = $("#toast"); el.textContent = msg; el.className = "toast" + (err ? " err" : ""); el.hidden = false;
  clearTimeout(toastT); toastT = setTimeout(() => { el.hidden = true; }, 4200);
}

// ── API ──
let onUnauthenticated = () => {};
export function setUnauthenticatedHandler(fn) { onUnauthenticated = fn; }
export async function initSession() {
  const r = await fetch("/api/session", { credentials: "same-origin" });
  const s = await r.json();
  state.csrf = s.csrf || null;
  $("#logoutBtn").classList.toggle("hidden", !s.password_required);
  return s;
}
/** JSON API call. Errors carry .status, .code and .field (server validation) so views can translate them. */
export async function api(path, { method = "GET", body, form, retry = true } = {}) {
  const opt = { method, headers: {}, credentials: "same-origin" };
  if (method !== "GET") opt.headers["X-Nyaya-CSRF"] = state.csrf || "";
  if (form) opt.body = form;
  else if (body !== undefined) { opt.headers["Content-Type"] = "application/json"; opt.body = JSON.stringify(body); }
  const r = await fetch(path, opt);
  if (r.status === 401 && retry) {
    const s = await initSession();
    if (!s.authenticated) { onUnauthenticated(); throw new Error(t("err.sign_in")); }
    return api(path, { method, body, form, retry: false });
  }
  if (!r.ok) {
    let j = {}; try { j = await r.json(); } catch { /* not json */ }
    const msg = typeof j.detail === "string" && j.detail ? j.detail : t("err.request_failed", { status: r.status });
    const e = new Error(j.code ? t(`err.${j.code}`, {}, msg) : msg);
    e.status = r.status; e.code = j.code; e.field = j.field;
    throw e;
  }
  return r.json();
}
export async function reference() {
  if (!state.ref) state.ref = await api("/api/reference");
  return state.ref;
}

// ── Markdown (input is escaped first, so the output cannot contain foreign HTML) ──
function inline(s) {
  return s
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|[\s(])\*([^*\s][^*]*?)\*(?=[\s.,;:)]|$)/g, "$1<em>$2</em>")
    .replace(/【\s*(\d{1,2})(?:†[^】]*)?】/g, "[$1]")
    .replace(/\[●([^\]]{0,120})\]/g, '<mark class="ph" title="Placeholder — fill in">[●$1]</mark>')
    .replace(/⟦(F\d{1,3})⟧/g, '<button type="button" class="fact" data-fact="$1" aria-label="Fact $1">$1</button>')
    .replace(/\[(\d{1,2})\]/g, '<button type="button" class="cite" data-cite="$1" aria-label="Source $1">$1</button>');
}
export function markdown(src) {
  const L = esc(src || "").split("\n");
  const out = []; let i = 0;
  const isBlockStart = (l) => /^(#{1,4})\s|^```|^\s*([-*+]|\d+[.)])\s+|^\s*&gt;|^\s*\|.*\|\s*$|^\s*(-{3,}|\*{3,})\s*$/.test(l);
  while (i < L.length) {
    const l = L[i];
    if (/^```/.test(l)) {
      const buf = []; i++;
      while (i < L.length && !/^```/.test(L[i])) buf.push(L[i++]);
      i++; out.push(`<pre><code>${buf.join("\n")}</code></pre>`); continue;
    }
    if (!l.trim()) { i++; continue; }
    let m;
    if ((m = l.match(/^(#{1,4})\s+(.*)$/))) { const n = m[1].length; out.push(`<h${n}>${inline(m[2])}</h${n}>`); i++; continue; }
    if (/^\s*(-{3,}|\*{3,}|_{3,})\s*$/.test(l)) { out.push("<hr>"); i++; continue; }
    if (/^\s*\|.*\|\s*$/.test(l) && i + 1 < L.length && /^\s*\|?\s*:?-{2,}/.test(L[i + 1])) {
      const cells = (r) => r.trim().replace(/^\||\|$/g, "").split("|").map((c) => inline(c.trim()));
      const head = cells(l); i += 2; const rows = [];
      while (i < L.length && /^\s*\|.*\|\s*$/.test(L[i])) rows.push(cells(L[i++]));
      out.push(`<table><thead><tr>${head.map((c) => `<th>${c}</th>`).join("")}</tr></thead><tbody>${
        rows.map((r) => `<tr>${r.map((c) => `<td>${c}</td>`).join("")}</tr>`).join("")}</tbody></table>`);
      continue;
    }
    if (/^\s*&gt;\s?/.test(l)) {
      const buf = [];
      while (i < L.length && /^\s*&gt;\s?/.test(L[i])) buf.push(L[i++].replace(/^\s*&gt;\s?/, ""));
      out.push(`<blockquote>${inline(buf.join("<br>"))}</blockquote>`); continue;
    }
    if (/^\s*([-*+]|\d+[.)])\s+/.test(l)) {
      const ordered = /^\s*\d+[.)]\s+/.test(l); const items = [];
      while (i < L.length && (/^\s*([-*+]|\d+[.)])\s+/.test(L[i]) || (/^\s{2,}\S/.test(L[i]) && items.length))) {
        if (/^\s*([-*+]|\d+[.)])\s+/.test(L[i])) items.push(L[i].replace(/^\s*([-*+]|\d+[.)])\s+/, ""));
        else items[items.length - 1] += " " + L[i].trim();
        i++;
      }
      const tag = ordered ? "ol" : "ul";
      out.push(`<${tag}>${items.map((x) => `<li>${inline(x)}</li>`).join("")}</${tag}>`); continue;
    }
    const buf = [];
    while (i < L.length && L[i].trim() && !(buf.length && isBlockStart(L[i]))) buf.push(L[i++]);
    out.push(`<p>${inline(buf.join("<br>"))}</p>`);
  }
  return raw(out.join(""));
}

// ── Page chrome ──
export function crumbs(items) {
  mount($("#crumbs"), html`<a href="#/dashboard">${t("nav.home")}</a>${items.map(([x, href]) => html` › ${href ? html`<a href="${href}">${x}</a>` : x}`)}`);
}
export function bindRowLinks(el) { $$("tr[data-href]", el).forEach((tr) => { tr.onclick = () => { location.hash = tr.dataset.href; }; }); }
export function showDrawer(title) { if (title) $("#drawerTitle").textContent = title; const d = $("#drawer"); d.hidden = false; $("#drawerClose").focus(); }
export function hideDrawer() { $("#drawer").hidden = true; }
export const formData = (form) => Object.fromEntries([...new FormData(form).entries()].filter(([, v]) => typeof v === "string").map(([k, v]) => [k, v.trim()]));
/** Show a server validation error next to its field (falls back to a toast). */
export function fieldError(form, err) {
  $$(".field-err", form).forEach((e) => e.remove());
  $$("[aria-invalid]", form).forEach((e) => e.removeAttribute("aria-invalid"));
  const input = err.field && form.querySelector(`[name="${err.field}"]`);
  if (!input) { toast(err.message, true); return; }
  input.setAttribute("aria-invalid", "true");
  const msg = document.createElement("div"); msg.className = "field-err"; msg.textContent = err.message;
  msg.id = `err_${err.field}`; input.setAttribute("aria-describedby", msg.id);
  input.insertAdjacentElement("afterend", msg); input.focus();
  // The message goes away as soon as the user corrects the field.
  const clear = () => { msg.remove(); input.removeAttribute("aria-invalid"); input.removeAttribute("aria-describedby"); };
  input.addEventListener("input", clear, { once: true }); input.addEventListener("change", clear, { once: true });
}
export const spinner = (text) => html`<div class="status-line"><span class="spinner"></span> ${text || t("common.loading")}</div>`;

/** Poll a background job until it finishes; onProgress(job) is called with each update. Resolves with job.result. */
export async function pollJob(job, onProgress = () => {}) {
  let j = job;
  for (;;) {
    onProgress(j);
    if (j.status === "done") return j.result;
    if (j.status === "error") throw new Error(j.error || t("common.failed"));
    await new Promise((r) => setTimeout(r, 1200));
    j = await api(`/api/jobs/${encodeURIComponent(j.id)}`);
  }
}
export const progressBar = (j) => html`<div class="progress" role="progressbar" aria-valuemin="0" aria-valuemax="${j.total || 1}" aria-valuenow="${j.done || 0}">
  <div class="progress-fill" style="width:${j.total ? Math.round(100 * j.done / j.total) : 8}%"></div></div><div class="small muted">${j.message || t("common.working")}</div>`;

/** Accessible confirmation dialog (native <dialog>). Resolves true/false. */
export function confirmDialog(title, bodyText, okLabel) {
  return new Promise((resolve) => {
    const d = document.createElement("dialog");
    d.className = "dlg";
    mount(d, html`<form method="dialog"><h2>${title}</h2><p>${bodyText}</p>
      <div class="row" style="justify-content:flex-end"><button class="btn btn-ghost" value="no">${t("common.cancel")}</button>
      <button class="btn btn-saffron" value="yes">${okLabel || t("common.continue")}</button></div></form>`);
    document.body.appendChild(d);
    d.addEventListener("close", () => { resolve(d.returnValue === "yes"); d.remove(); });
    d.showModal();
  });
}
/** Consent before document text leaves this computer for the configured LLM. */
export const aiConsent = () => confirmDialog(t("consent.title"), t("consent.body"), t("consent.ok"));
export function download(url) { const a = document.createElement("a"); a.href = url; a.rel = "noopener"; document.body.appendChild(a); a.click(); a.remove(); }
