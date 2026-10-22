/* Internationalisation: one JSON dictionary per language in /static/i18n/, English as the fallback.
   Adding a language = adding <code>.json + an entry in LANGS. Legal text (statutes, citations, party
   names) is content, not UI, and is never passed through t(). */
export const LANGS = [
  { code: "en", label: "English", toggle: true },
  { code: "hi", label: "हिंदी", toggle: true },
  { code: "mr", label: "मराठी", toggle: false },  // dictionary is partial; enable once reviewed
];
const dicts = { en: {} };
let current = "en";

async function load(code) {
  if (dicts[code] && Object.keys(dicts[code]).length) return;
  const r = await fetch(`/static/i18n/${code}.json`, { credentials: "same-origin" });
  dicts[code] = r.ok ? await r.json() : {};
}

/** Translate a key; {name} placeholders are filled from vars. Falls back to English, then to `fallback`, then the key. */
export function t(key, vars = {}, fallback) {
  let s = dicts[current]?.[key] ?? dicts.en[key] ?? fallback ?? key;
  for (const [k, v] of Object.entries(vars)) s = s.split(`{${k}}`).join(String(v));
  return s;
}
export const lang = () => current;

/** Static markup: data-i18n="key" sets text; data-i18n-attr="aria-label:key;placeholder:key" sets attributes. */
export function applyStatic(root = document) {
  root.querySelectorAll("[data-i18n]").forEach((el) => { el.textContent = t(el.dataset.i18n); });
  root.querySelectorAll("[data-i18n-attr]").forEach((el) => {
    el.dataset.i18nAttr.split(";").forEach((pair) => { const [a, k] = pair.split(":"); if (a && k) el.setAttribute(a.trim(), t(k.trim())); });
  });
}

export async function setLang(code) {
  if (!LANGS.some((l) => l.code === code)) code = "en";
  await Promise.all([load("en"), load(code)]);
  current = code;
  document.documentElement.lang = code;
  try { localStorage.setItem("nyaya.lang", JSON.stringify(code)); } catch { /* storage unavailable */ }
  applyStatic();
}

export function initialLang() {
  try { const v = JSON.parse(localStorage.getItem("nyaya.lang")); if (v) return v; } catch { /* none */ }
  return (navigator.language || "en").toLowerCase().startsWith("hi") ? "hi" : "en";
}
