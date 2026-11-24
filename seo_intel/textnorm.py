"""Tokenisation for English, Hindi (Devanagari) and Hinglish (romanised Hindi).

* NFKC; Devanagari kept as whole akshara runs (matras/virama are combining marks,
  which Python's \\w would split on, so we use explicit code-point ranges)
* nukta removed, chandrabindu -> anusvara (common spelling variants)
* danda/double danda are sentence separators
* Hinglish spelling variance (bahut/bahuut, accha/achha/acha) reduced by a
  phonetic key used for shingling: squeeze repeated letters, 'h' after consonant
  dropped, w->v, z->j, q->k, ee->i, oo->u
"""
from __future__ import annotations

import re
import unicodedata

DEV = "\u0900-\u0963\u0966-\u097F\uA8E0-\uA8FF"  # danda U+0964/U+0965 are separators
TOKEN = re.compile(rf"[{DEV}]+|[a-z0-9]+(?:['’][a-z]+)?")
SENT_SPLIT = re.compile(r"(?<=[.!?।॥])\s+|\n+")
DANDA = re.compile(r"[।॥]")

EN_STOP = set("""a an the and or but if of to in on at by for with from as is are was were be been being
this that these those it its it's i you he she we they them his her our your their my me us not no yes
do does did done have has had having will would can could should may might must shall than then there
here what which who whom whose when where why how all any each few more most other some such only own
same so too very just also into over under about after before between through during up down out off
again further once both s t don now""".split())
HI_STOP = set("""है हैं था थी थे हो होना होता होती होते और या का की के को में से पर तक भी ही तो यह ये वह वे
इस उस इन उन एक कुछ किसी कोई जो जिस जिन कि लिए साथ द्वारा नहीं न अपने अपना अपनी हम आप तुम मैं
कर करें करना किया गया गई गए रहा रही रहे""".split())
HINGLISH_STOP = set("""hai hain tha thi the ho hona hota hoti hote aur ya ka ki ke ko mein me se par tak bhi hi to
yeh ye woh wo vo is us in un ek kuch kisi koi jo jis jin ki liye saath nahi nahin na apne apna apni hum
aap tum main kar karen karna kiya gaya gayi gaye raha rahi rahe kya kaise kyun kab kahan""".split())
STOP = EN_STOP | HI_STOP | HINGLISH_STOP
HINGLISH_MARKERS = set("hai hain ka ki ke ko mein nahi nahin kya aur bhi kaise kyun liye wala wali apna".split())


def norm(text: str) -> str:
    t = unicodedata.normalize("NFKC", text or "").lower()
    t = t.replace("़", "").replace("ँ", "ं")
    return t


def tokens(text: str, drop_stop=False):
    toks = TOKEN.findall(norm(text))
    if drop_stop:
        toks = [t for t in toks if t not in STOP and len(t) > 1]
    return toks


_PH = [(re.compile(r"(.)\1+"), r"\1"), (re.compile(r"([bcdfgjklmnpqrstvwxyz])h"), r"\1"),
       (re.compile(r"w"), "v"), (re.compile(r"z"), "j"), (re.compile(r"q"), "k")]


def phonetic(tok: str) -> str:
    if not tok or not tok[0].isascii():
        return tok
    t = tok.replace("ee", "i").replace("oo", "u")
    for rx, rep in _PH:
        t = rx.sub(rep, t)
    return t


def shingle_tokens(text: str):
    return [phonetic(t) for t in tokens(text)]


def script_profile(text: str):
    toks = tokens(text)
    if not toks:
        return "unknown"
    dev = sum(1 for t in toks if not t[0].isascii())
    if dev / len(toks) > 0.5:
        return "hi"
    lat = [t for t in toks if t[0].isascii()]
    hm = sum(1 for t in lat if t in HINGLISH_MARKERS)
    if lat and hm / len(lat) > 0.06:
        return "hinglish"
    return "mixed" if dev / len(toks) > 0.15 else "en"


def sentences(text: str):
    out = []
    for s in SENT_SPLIT.split(text or ""):
        s = s.strip()
        if len(s) >= 3:
            out.append(s)
    return out
