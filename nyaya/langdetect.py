"""Script and language detection for uploaded documents (no network, no model).

Script comes from Unicode blocks. Devanagari is shared by Hindi and Marathi, so those two are
separated with Marathi-only letters and very common function words; when the evidence is thin the
result says so (confidence 'low') instead of guessing silently.
"""
from __future__ import annotations

import re
from collections import Counter

# (name, first code point, last code point, default language code)
SCRIPTS = [
    ("Devanagari", 0x0900, 0x097F, "hi"), ("Bengali", 0x0980, 0x09FF, "bn"), ("Gurmukhi", 0x0A00, 0x0A7F, "pa"),
    ("Gujarati", 0x0A80, 0x0AFF, "gu"), ("Odia", 0x0B00, 0x0B7F, "or"), ("Tamil", 0x0B80, 0x0BFF, "ta"),
    ("Telugu", 0x0C00, 0x0C7F, "te"), ("Kannada", 0x0C80, 0x0CFF, "kn"), ("Malayalam", 0x0D00, 0x0D7F, "ml"),
    ("Arabic", 0x0600, 0x06FF, "ur"), ("Latin", 0x0041, 0x024F, "en"),
]
LANG_NAMES = {"en": "English", "hi": "Hindi", "mr": "Marathi", "bn": "Bengali", "pa": "Punjabi", "gu": "Gujarati",
              "or": "Odia", "ta": "Tamil", "te": "Telugu", "kn": "Kannada", "ml": "Malayalam", "ur": "Urdu",
              "und": "Undetermined"}

_MR_WORDS = {"आहे", "आणि", "नाही", "होते", "आहेत", "करण्यात", "यांच्या", "त्यांच्या", "मध्ये", "म्हणून", "केले", "असे", "व", "हे", "तर"}
_HI_WORDS = {"है", "और", "नहीं", "था", "थे", "हैं", "किया", "द्वारा", "में", "का", "की", "के", "को", "से", "यह", "गया"}
_WORD = re.compile(r"[ऀ-ॿ]+")


def _script_of(ch: str) -> str | None:
    cp = ord(ch)
    for name, lo, hi, _ in SCRIPTS:
        if lo <= cp <= hi and (name != "Latin" or ch.isalpha()):
            return name
    return None


def detect(text: str, sample: int = 20000) -> dict:
    text = (text or "")[:sample]
    counts = Counter(s for s in map(_script_of, text) if s)
    total = sum(counts.values())
    if not total:
        return {"lang": "und", "lang_name": LANG_NAMES["und"], "script": None, "confidence": "low", "scripts": {}}
    shares = {k: round(v / total, 3) for k, v in counts.most_common()}
    script = max(counts, key=counts.get)
    lang = next(code for name, *_, code in SCRIPTS if name == script)
    confidence = "high" if shares[script] >= 0.8 else "medium" if shares[script] >= 0.55 else "low"
    if script == "Devanagari":
        words = Counter(_WORD.findall(text))
        mr = sum(n for w, n in words.items() if w in _MR_WORDS) + 3 * text.count("ळ")
        hi = sum(n for w, n in words.items() if w in _HI_WORDS)
        if mr + hi < 5:
            confidence = "low"
            lang = "mr" if mr > hi else "hi"
        else:
            lang = "mr" if mr > hi else "hi"
            ratio = max(mr, hi) / (mr + hi)
            if ratio < 0.65:
                confidence = "low"
    secondary = [k for k, v in shares.items() if k != script and v >= 0.1]
    return {"lang": lang, "lang_name": LANG_NAMES.get(lang, lang), "script": script, "confidence": confidence,
            "scripts": shares, "mixed_with": secondary}
