"""Data minimisation before text leaves the server, and redaction of logs.

* Pseudonymisation for the external LLM: Aadhaar, PAN, phone, e-mail, bank account, payment card, passport and UPI
  identifiers are replaced by placeholders such as [AADHAAR_1] before a prompt is sent, and the real values are put
  back into the reply on this server. The mapping exists only in memory for one call.
  Names, addresses and facts are NOT removed: legal analysis needs them. That is a deliberate, documented limit.
* Log redaction: every log record passes through a filter that removes configured secrets, bearer/JWT tokens,
  Supabase keys, database URLs with passwords and e-mail addresses.
"""
from __future__ import annotations

import logging
import re

from . import config

# ── Identifier patterns (checked with checksums/context where possible to avoid hiding legal numbers) ──
_AADHAAR = re.compile(r"(?<![\d-])([2-9]\d{3})[ -]?(\d{4})[ -]?(\d{4})(?![\d-])")
_PAN = re.compile(r"\b[A-Z]{3}[PCHFATBLJG][A-Z]\d{4}[A-Z]\b")
_PHONE = re.compile(r"(?<![\w+])(?:\+91[ -]?|0091[ -]?|0)?([6-9]\d{4})[ -]?(\d{5})(?!\d)")
_EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9.-]{1,253}\.[A-Za-z]{2,24}\b")
_UPI = re.compile(r"\b[A-Za-z0-9._-]{2,64}@(?:ok(?:axis|hdfcbank|icici|sbi)|ybl|ibl|axl|paytm|upi|apl|fbl|ptyes|ptsbi)\b",
                  re.I)
_CARD = re.compile(r"(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)")
_ACCOUNT = re.compile(r"(?i)\b(?:a/c|acct?|account|bank\s+account|खाता)\b[^\d\n]{0,25}(\d{9,18})\b")
_PASSPORT = re.compile(r"(?i)\bpassport\b[^\n]{0,25}?\b([A-PR-WY][1-9]\d{6})\b")

_VERHOEFF_D = [[0, 1, 2, 3, 4, 5, 6, 7, 8, 9], [1, 2, 3, 4, 0, 6, 7, 8, 9, 5], [2, 3, 4, 0, 1, 7, 8, 9, 5, 6],
               [3, 4, 0, 1, 2, 8, 9, 5, 6, 7], [4, 0, 1, 2, 3, 9, 5, 6, 7, 8], [5, 9, 8, 7, 6, 0, 4, 3, 2, 1],
               [6, 5, 9, 8, 7, 1, 0, 4, 3, 2], [7, 6, 5, 9, 8, 2, 1, 0, 4, 3], [8, 7, 6, 5, 9, 3, 2, 1, 0, 4],
               [9, 8, 7, 6, 5, 4, 3, 2, 1, 0]]
_VERHOEFF_P = [[0, 1, 2, 3, 4, 5, 6, 7, 8, 9], [1, 5, 7, 6, 2, 8, 3, 0, 9, 4], [5, 8, 0, 3, 7, 9, 6, 1, 4, 2],
               [8, 9, 1, 6, 0, 4, 3, 5, 2, 7], [9, 4, 5, 3, 1, 2, 6, 8, 7, 0], [4, 2, 8, 6, 5, 7, 3, 9, 0, 1],
               [2, 7, 9, 3, 8, 0, 6, 4, 1, 5], [7, 0, 4, 6, 9, 1, 3, 2, 5, 8]]


def verhoeff_ok(num: str) -> bool:
    c = 0
    for i, ch in enumerate(reversed(num)):
        c = _VERHOEFF_D[c][_VERHOEFF_P[i % 8][int(ch)]]
    return c == 0


def luhn_ok(num: str) -> bool:
    total, alt = 0, False
    for ch in reversed(num):
        d = int(ch)
        if alt:
            d = d * 2 - 9 if d > 4 else d * 2
        total += d
        alt = not alt
    return total % 10 == 0


PLACEHOLDER = re.compile(r"\[(AADHAAR|PAN|PHONE|EMAIL|UPI|CARD|ACCOUNT|PASSPORT)_(\d{1,3})\]")


class Redactor:
    """One instance per LLM call: redact() every outgoing text, restore() every reply."""

    def __init__(self) -> None:
        self.by_value: dict[str, str] = {}
        self.by_tag: dict[str, str] = {}
        self.counts: dict[str, int] = {}

    def _tag(self, kind: str, value: str) -> str:
        if value in self.by_value:
            return self.by_value[value]
        self.counts[kind] = self.counts.get(kind, 0) + 1
        tag = f"[{kind}_{self.counts[kind]}]"
        self.by_value[value], self.by_tag[tag] = tag, value
        return tag

    def redact(self, text: str) -> str:
        if not text:
            return text
        text = _EMAIL.sub(lambda m: self._tag("EMAIL", m.group(0)), text)
        text = _UPI.sub(lambda m: self._tag("UPI", m.group(0)), text)
        text = _PAN.sub(lambda m: self._tag("PAN", m.group(0)), text)
        text = _ACCOUNT.sub(lambda m: m.group(0).replace(m.group(1), self._tag("ACCOUNT", m.group(1))), text)
        text = _PASSPORT.sub(lambda m: m.group(0).replace(m.group(1), self._tag("PASSPORT", m.group(1))), text)

        def card(m):
            digits = re.sub(r"\D", "", m.group(0))
            return self._tag("CARD", m.group(0)) if 13 <= len(digits) <= 19 and luhn_ok(digits) else m.group(0)
        text = _CARD.sub(card, text)

        def aadhaar(m):
            digits = "".join(m.groups())
            return self._tag("AADHAAR", m.group(0)) if verhoeff_ok(digits) else m.group(0)
        text = _AADHAAR.sub(aadhaar, text)
        text = _PHONE.sub(lambda m: self._tag("PHONE", m.group(0)), text)
        return text

    def restore(self, text: str) -> str:
        if not self.by_tag or not text:
            return text
        return PLACEHOLDER.sub(lambda m: self.by_tag.get(m.group(0), m.group(0)), text)

    def stream(self, tokens):
        """Restore placeholders in a token stream, holding back text that may be the start of a placeholder."""
        buf = ""
        for tok in tokens:
            buf += tok
            cut = buf.rfind("[")
            if cut == -1 or "]" in buf[cut:] or len(buf) - cut > 16:
                out, buf = self.restore(buf), ""
            else:
                out, buf = self.restore(buf[:cut]), buf[cut:]
            if out:
                yield out
        if buf:
            yield self.restore(buf)


def redact_messages(messages: list[dict]) -> tuple[list[dict], Redactor]:
    r = Redactor()
    if not config.AI_PII_REDACTION:
        return messages, r
    return [{**m, "content": r.redact(m.get("content") or "")} for m in messages], r


# ── Log redaction ─────────────────────────────────────────────────────────────
_LOG_PATTERNS = [
    (re.compile(r"sb_(?:secret|publishable)_[A-Za-z0-9_-]{8,}"), "[supabase-key]"),
    (re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"), "[jwt]"),
    (re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._~+/=-]{16,}"), r"\1 [token]"),
    (re.compile(r"(?i)(postgres(?:ql)?://[^:/\s]+:)[^@\s]+@"), r"\1[password]@"),
    (_EMAIL, "[email]"),
]


def _secret_values() -> list[str]:
    vals = [config.OLLAMA_API_KEY, config.SUPABASE_SECRET_KEY, config.SUPABASE_PUBLISHABLE_KEY, config.APP_PASSWORD,
            config.APP_SECRET_KEY]
    m = re.match(r"postgres(?:ql)?://[^:/\s]+:([^@\s]+)@", config.SUPABASE_DB_URL or "")
    if m:
        vals.append(m.group(1))
    return [v for v in vals if v and len(v) >= 6]


def scrub(text: str) -> str:
    for v in _secret_values():
        text = text.replace(v, "[secret]")
    for pat, rep in _LOG_PATTERNS:
        text = pat.sub(rep, text)
    return text


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:  # malformed format args: leave the record alone
            return True
        clean = scrub(msg)
        if record.exc_info and record.exc_info[1] is not None:
            record.exc_text = scrub(logging.Formatter().formatException(record.exc_info))
            record.exc_info = None
        if clean != msg or record.args:
            record.msg, record.args = clean, None
        return True


def install_log_filter() -> None:
    f = RedactingFilter()
    root = logging.getLogger()
    for h in root.handlers:
        if not any(isinstance(x, RedactingFilter) for x in h.filters):
            h.addFilter(f)
    if not any(isinstance(x, RedactingFilter) for x in root.filters):
        root.addFilter(f)
