/* Module 3 — voice input (speech-to-text) and optional read-aloud (text-to-speech) for Legal Research.

   Uses the browser's Web Speech API. NyaySutra never receives or stores audio: the browser turns speech into
   text, the text appears in the question box for editing, and nothing is sent until the user presses Send.
   Where the browser has no speech recognition the microphone button is not shown at all. */
import { $, api, confirmDialog, html, mount, raw, state, toast } from "./core.js";
import { lang, t } from "./i18n.js";

const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
const VOICE_LANGS = [["en-IN", "English"], ["hi-IN", "हिंदी"], ["mr-IN", "मराठी"]];
const UI_TO_VOICE = { en: "en-IN", hi: "hi-IN", mr: "mr-IN" };

export function voiceControls(slot, textarea, status) {
  if (!SR) {
    mount(status, html`<span class="muted">${t("voice.unsupported")}</span>`);
    return;
  }
  const saved = state.prefs.voice_lang || UI_TO_VOICE[lang()] || "en-IN";
  mount(slot, html`<span class="voice"><select id="vLang" aria-label="${t("voice.lang")}" title="${t("voice.lang")}">${VOICE_LANGS.map(([c, l]) => html`<option value="${c}" ${c === saved ? raw("selected") : ""}>${l}</option>`)}</select>
    <button type="button" class="btn btn-ghost mic" id="micBtn" aria-pressed="false" aria-label="${t("voice.start")}" title="${t("voice.start")}">🎤</button></span>`);
  let rec = null, base = "", listening = false;
  const setStatus = (msg, err = false) => mount(status, html`<span class="${err ? "err-text" : ""}">${msg}</span>`);
  const stop = () => { if (rec) rec.stop(); };
  const start = async () => {
    if (state.prefs.voice_consent !== "granted") {
      const ok = await confirmDialog(t("voice.consent_title"), t("voice.consent_body"), t("voice.consent_ok"));
      if (!ok) return;
      try { state.prefs = await api("/api/prefs", { method: "PUT", body: { values: { voice_consent: "granted" } } }); } catch { state.prefs.voice_consent = "granted"; }
    }
    rec = new SR();
    rec.lang = $("#vLang").value; rec.interimResults = true; rec.continuous = true; rec.maxAlternatives = 1;
    base = textarea.value ? textarea.value.replace(/\s*$/, " ") : "";
    rec.onstart = () => { listening = true; $("#micBtn").setAttribute("aria-pressed", "true"); $("#micBtn").classList.add("rec"); $("#micBtn").setAttribute("aria-label", t("voice.stop")); setStatus(t("voice.listening")); };
    rec.onresult = (e) => {
      let finalText = "", interim = "";
      for (let i = 0; i < e.results.length; i++) { const r = e.results[i]; (r.isFinal ? (finalText += r[0].transcript) : (interim += r[0].transcript)); }
      textarea.value = base + finalText + interim;
      textarea.dataset.inputMode = "voice";
    };
    rec.onerror = (e) => {
      const key = { "not-allowed": "voice.denied", "service-not-allowed": "voice.denied", "no-speech": "voice.no_speech", network: "voice.network", "audio-capture": "voice.no_mic", "language-not-supported": "voice.lang_unsupported" }[e.error];
      setStatus(t(key || "voice.error", { err: e.error }), true);
    };
    rec.onend = () => {
      listening = false; rec = null; $("#micBtn").setAttribute("aria-pressed", "false"); $("#micBtn").classList.remove("rec"); $("#micBtn").setAttribute("aria-label", t("voice.start"));
      if (!status.querySelector(".err-text")) setStatus(textarea.value.trim() ? t("voice.review") : "");
      textarea.focus();
    };
    try { rec.start(); } catch (err) { setStatus(t("voice.error", { err: err.message }), true); }
  };
  $("#micBtn").onclick = () => (listening ? stop() : start());
  $("#vLang").onchange = async (e) => { try { state.prefs = await api("/api/prefs", { method: "PUT", body: { values: { voice_lang: e.target.value } } }); } catch { /* local only */ } };
}

// ── Read aloud ──
const canSpeak = "speechSynthesis" in window;
export const speakButton = (answerLang) => canSpeak ? html`<button type="button" class="btn btn-ghost btn-sm speak" data-speak="${UI_TO_VOICE[answerLang || "en"] || "en-IN"}" aria-label="${t("voice.read_aloud")}" title="${t("voice.read_aloud")}">🔊</button>` : "";

export function bindSpeak(container) {
  if (!canSpeak) return;
  container.addEventListener("click", (e) => {
    const b = e.target.closest("[data-speak]"); if (!b) return;
    if (speechSynthesis.speaking) { speechSynthesis.cancel(); if (b.classList.contains("on")) { b.classList.remove("on"); return; } }
    const text = b.closest(".msg-ai")?.querySelector(".md")?.innerText || "";
    if (!text.trim()) return;
    const u = new SpeechSynthesisUtterance(text.slice(0, 6000));
    u.lang = b.dataset.speak;
    const voice = speechSynthesis.getVoices().find((v) => v.lang === u.lang) || speechSynthesis.getVoices().find((v) => v.lang.startsWith(u.lang.slice(0, 2)));
    if (voice) u.voice = voice; else if (u.lang !== "en-IN") toast(t("voice.no_voice"), true);
    b.classList.add("on"); u.onend = u.onerror = () => b.classList.remove("on");
    speechSynthesis.speak(u);
  });
}
