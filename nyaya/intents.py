"""Code-level intent rules around the planner.

The planner model classifies the meaning of the whole message (see prompts.PLANNER). These rules exist for the cases
where a model must not decide, or must not be the only one deciding:

* clock_question() — "what time is it / today's date" is answered from the clock, before any model sees it, so the
  time can never be invented. Anchored on the whole message, so "what is the time limit for an appeal" is not a time
  question.
* has_legal_angle() — a safety net against an over-restrictive classifier: a message the planner calls out_of_scope
  is still researched when it carries a legal signal (cybercrime, IT Act, FIR, court, rights, …).
* looks_like_code_request() — used only when the planner itself failed, so an unrelated coding request still gets
  the redirect instead of a library search.
* strip_parties() — client and opposite-party names are removed before a query is sent to the web.
"""
from __future__ import annotations

import re

from . import guardrails

_PRE = (r"(?:(?:hi|hello|hey|ok|okay|please|pls|plz|so|and|now|nyaysutra|tell\s+me|can\s+you\s+tell\s+me|"
        r"could\s+you\s+tell\s+me|do\s+you\s+know|may\s+i\s+know|i\s+want\s+to\s+know)[\s,]*)*")
_SUF = (r"(?:[\s,]+(?:now|right\s+now|today|currently|at\s+the\s+moment|in\s+india|in\s+ist|ist|indian\s+standard\s+time|"
        r"here|in\s+(?:mumbai|delhi|pune|kolkata|chennai|bengaluru|bangalore|hyderabad)|please|pls|plz|"
        r"bhai|ji|sir|madam|abhi))*")
_TIME = (r"(?:what(?:'s|\s+is)?\s+(?:the\s+)?(?:current\s+|exact\s+|present\s+)?time(?:\s+is\s+it)?|what\s+time\s+is\s+it|"
         r"(?:the\s+)?current\s+time|(?:the\s+)?time\s+(?:right\s+)?now|time\s+please|time\s*\?*|"
         r"(?:the\s+)?(?:current\s+)?(?:date\s+and\s+time|time\s+and\s+date)|"
         r"(?:abhi\s+)?kitne\s+baje\s+(?:hai|hain|he|hue)?|(?:abhi\s+)?(?:kya\s+)?time\s+(?:kya\s+)?(?:hua|hai|ho\s+gaya)|"
         r"samay\s+kya\s+(?:hai|hua)|(?:अभी\s+)?कितने\s+बजे(?:\s+हैं|\s+है)?|(?:अभी\s+)?(?:क्या\s+)?समय\s+(?:क्या\s+)?(?:है|हुआ)|"
         r"(?:आता\s+)?किती\s+वाजले(?:\s+आहेत)?|(?:आत्ता\s+)?वेळ\s+काय\s+(?:आहे|झाली))")
_DATE = (r"(?:what(?:'s|\s+is)?\s+(?:the\s+)?(?:today'?s\s+|current\s+|present\s+)?date(?:\s+today)?|today'?s\s+date|"
         r"(?:the\s+)?current\s+date|(?:what|which)\s+day\s+is\s+(?:it|today)|what\s+is\s+today|"
         r"aaj\s+(?:ki\s+)?(?:kya\s+)?(?:tarikh|tareekh|tarik|date)\s*(?:kya\s+)?(?:hai)?|"
         r"aaj\s+(?:kaun\s*sa|konsa|kon\s*sa)\s+din\s*(?:hai)?|आज\s+(?:की\s+)?(?:क्या\s+)?तारीख\s*(?:क्या\s+)?(?:है)?|"
         r"आज\s+कौन\s*सा\s+(?:दिन|वार)\s+है|आजची\s+तारीख\s*(?:काय\s+)?(?:आहे)?|आज\s+कोणता\s+वार\s+आहे)")
_TIME_RE = re.compile(rf"{_PRE}{_TIME}{_SUF}", re.I)
_DATE_RE = re.compile(rf"{_PRE}{_DATE}{_SUF}", re.I)


def _clean(message: str) -> str:
    t = guardrails.normalise(message).strip().lower()
    t = re.sub(r"[\s?!.।]+$", "", t)
    return re.sub(r"\s+", " ", t)


def clock_question(message: str) -> str | None:
    """'time' or 'date' when the whole message only asks for the current time or date; else None."""
    t = _clean(message)
    if not t or len(t) > 80:
        return None
    if _TIME_RE.fullmatch(t):
        return "time"
    if _DATE_RE.fullmatch(t):
        return "date"
    return None


_LEGAL = re.compile(
    r"\b(?:law|laws|legal|illegal|unlawful|lawful|legally|act|acts|section|sections|sec|article|clause|statute|rule|"
    r"court|courts|judge|judgment|judgement|verdict|tribunal|bench|high\s+court|supreme\s+court|case\s+law|precedent|"
    r"fir|bail|police|complaint|offen[cs]es?|crimes?|criminal|cyber\s*crimes?|cyber\s*(?:attack|security|fraud)|"
    r"punish\w*|penalt\w*|prosecut\w*|accused|trial|sentenc\w*|convict\w*|acquit\w*|rights?|constitution\w*|"
    r"ipc|bns|bnss|crpc|cpc|bsa|uapa|pmla|ndps|posh|rti|it\s+act|dpdp|data\s+protection|privacy|evidence|"
    r"contract|agreement|lawyer|advocate|attorney|petition|writ|appeal|liab\w*|complian\w*|regulat\w*|"
    r"copyright|patent|trademark|licen[cs]\w*|gst|income\s+tax|divorce|custody|maintenance|alimony|property|"
    r"tenant|landlord|rent|consumer|fraud|cheating|defamation|harass\w*|dowry|terror\w*|attack|murder|"
    r"arrest|custodial|investigation|nia|cbi|kanoon|kanooni|kanuni|adalat|vakil|thana|"
    r"कानून|क़ानून|कायदा|अदालत|न्यायालय|धारा|एफआईआर|जमानत|पुलिस|वकील|अपराध|शिकायत|अधिकार)\b", re.I)


def has_legal_angle(message: str) -> bool:
    return bool(_LEGAL.search(guardrails.normalise(message)))


_CODE = re.compile(
    r"\b(?:write|give|generate|create|make|build|show|fix|debug|explain|need)\b[^.?!\n]{0,40}?\b(?:code|program|"
    r"script|function|class|algorithm|website|web\s*site|web\s*page|app|html|css|javascript|typescript|python|java|"
    r"c\+\+|sql\s+query|regex|api)\b|\b(?:in|using)\s+(?:python|java|javascript|c\+\+|c#|golang|rust|php|kotlin|swift)\b",
    re.I)


_RECENT = re.compile(
    r"\b(?:latest|recent|recently|news|today'?s?|current(?:ly)?|this\s+(?:week|month|year)|yesterday|updates?|"
    r"happening|headlines|breaking|new\s+(?:laws?|bills?|acts?|rules?|amendments?|judgments?|rulings?)|"
    r"khabar|samachar|taza|taaza)\b|ताज़ा|ताजा|समाचार|खबर|ख़बर|आज|बातमी|नवीन|अलीकडील", re.I)


def asks_for_recent(message: str) -> bool:
    """The planner's 'news' is kept only when the message asks for something recent; a question about a past event
    (e.g. "tell me about the 26/11 attacks") is research."""
    return bool(_RECENT.search(guardrails.normalise(message)))


def looks_like_code_request(message: str) -> bool:
    t = guardrails.normalise(message)
    return bool(_CODE.search(t)) and not has_legal_angle(t)


OFF_TOPIC = {
    "en": ("I'm **NyaySutra**, your legal assistance AI. I specialise in Indian laws, legal rights, case research and "
           "legal procedures, so I can't help with general-purpose requests like this one (for example programming, "
           "maths problems or website development).\n\nI can help with the legal side of it instead, for example:\n"
           "- the law on cybercrime, data protection or software contracts and licences;\n"
           "- your rights, and how to file a police complaint or FIR;\n"
           "- a statute, a court judgment or one of your case documents."),
    "hi": ("मैं **न्यायसूत्र** हूँ, आपका कानूनी सहायता AI। मैं भारतीय कानून, कानूनी अधिकार, केस अनुसंधान और कानूनी प्रक्रियाओं में "
           "मदद करता हूँ, इसलिए इस तरह के सामान्य अनुरोध (जैसे प्रोग्रामिंग, गणित या वेबसाइट बनाना) में मदद नहीं कर सकता।\n\n"
           "मैं इसके कानूनी पहलू में मदद कर सकता हूँ — जैसे साइबर अपराध या डेटा संरक्षण का कानून, आपके अधिकार, "
           "FIR दर्ज करने की प्रक्रिया, या किसी अधिनियम, निर्णय अथवा आपके केस दस्तावेज़ पर प्रश्न।"),
    "mr": ("मी **न्यायसूत्र**, तुमचा कायदेशीर सहाय्यक AI आहे. मी भारतीय कायदे, कायदेशीर हक्क, केस संशोधन आणि कायदेशीर "
           "प्रक्रियांमध्ये मदत करतो, त्यामुळे अशा सामान्य विनंतीत (उदा. प्रोग्रामिंग, गणित किंवा वेबसाइट) मदत करू शकत नाही.\n\n"
           "मी याच्या कायदेशीर बाजूत मदत करू शकतो — उदा. सायबर गुन्हे किंवा डेटा संरक्षणाचा कायदा, तुमचे हक्क, FIR "
           "दाखल करण्याची प्रक्रिया, किंवा एखादा अधिनियम, निकाल अथवा तुमच्या केस दस्तऐवजावरील प्रश्न."),
}

CLARIFY_DEFAULT = ("Could you tell me a little more about what you need? For example, the law or section involved, "
                   "what happened, or which document you mean.")


def strip_parties(query: str, case: dict | None) -> str:
    """Remove the case's client and opposite-party names from a query before it leaves the server."""
    if not case:
        return query
    for field in ("client_name", "opposite_party"):
        for name in re.split(r"[,;/&]|\band\b", case.get(field) or ""):
            name = name.strip()
            if len(name) >= 3:
                query = re.sub(re.escape(name), " ", query, flags=re.I)
    return re.sub(r"\s+", " ", query).strip()
