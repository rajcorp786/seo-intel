"""Shared topic space (target + competitor) and intent classification.

Vectors: hashed TF-IDF over (title x2, H1 x2, title/H1 bigrams, incoming in-content anchor text,
main content) using the multilingual tokenizer (stop-words removed), df-pruned to 60k features,
reduced with TruncatedSVD (LSA, 128 dims, fixed random_state) and L2-normalised. Fitted on BOTH
sites jointly so pages are comparable across sites (needed for gap analysis).

Intent: transparent cue-scoring model over URL, title, H1, main text, incoming anchors.
Low-margin pages may be sent to the LLM, capped at 2% of pages (see llm.py budget).
"""
from __future__ import annotations

import re

import numpy as np
from sklearn.decomposition import TruncatedSVD
from scipy import sparse
from sklearn.feature_extraction.text import HashingVectorizer
from sklearn.utils import murmurhash3_32
from sklearn.preprocessing import normalize

from .textnorm import tokens

DIMS = 128
MAX_TOKENS = 600
N_FEATURES = 2 ** 20
KEEP_FEATURES = 60000
CHUNK = 2000


def analyzer(text):
    t = tokens(text, drop_stop=True)[:MAX_TOKENS]
    return t


def doc_text(p, anchors):
    title = p["title"] or ""
    h1 = p["h1"] or ""
    # title/H1 bigrams carry phrase meaning cheaply; body is unigrams (memory: ~300 nnz/doc)
    th = tokens(f"{title} {h1}", drop_stop=True)
    bi = " ".join(a + "_" + b for a, b in zip(th, th[1:]))
    return " ".join([title, title, h1, h1, bi, bi, " ".join(anchors[:50]), p["main_text"] or ""])


def _bucket(tok):
    return abs(murmurhash3_32(tok, seed=0)) % N_FEATURES


class TopicSpace:
    """Hashing TF-IDF (no vocabulary dict, streamed in chunks) -> df pruning to KEEP_FEATURES ->
    TruncatedSVD. Memory stays ~O(nnz) instead of O(vocabulary) (an exact TfidfVectorizer with
    bigrams peaked > 2.5 GB at 20k pages - DECISIONS.md)."""

    def __init__(self, seed=42):
        self.seed = seed
        self.X = None

    def fit(self, docs, doc_fn=None):
        self.doc_fn = doc_fn
        hv = HashingVectorizer(analyzer=analyzer, n_features=N_FEATURES, alternate_sign=False, norm=None,
                               dtype=np.float32)
        parts, buf = [], []
        for d in docs:
            buf.append(d)
            if len(buf) >= CHUNK:
                parts.append(hv.transform(buf))
                buf = []
        if buf:
            parts.append(hv.transform(buf))
        X = sparse.vstack(parts).tocsr()
        del parts, buf
        n = X.shape[0]
        df = np.bincount(X.indices, minlength=N_FEATURES)
        ok = (df >= (2 if n >= 50 else 1)) & (df <= max(2, 0.5 * n))
        cand = np.where(ok)[0]
        if len(cand) > KEEP_FEATURES:
            cand = cand[np.lexsort((cand, -df[cand]))[:KEEP_FEATURES]]
        cand = np.sort(cand)
        self.col_of = {int(b): i for i, b in enumerate(cand)}
        X = X[:, cand]
        X.data = 1 + np.log(X.data)
        self.idf = (np.log((1 + n) / (1 + df[cand])) + 1).astype(np.float32)
        X = X @ sparse.diags(self.idf)
        X = normalize(X)
        k = max(2, min(DIMS, X.shape[1] - 1, X.shape[0] - 1))
        self.svd = TruncatedSVD(n_components=k, random_state=self.seed, algorithm="randomized", n_iter=5)
        Z = self.svd.fit_transform(X)
        del X
        import gc
        gc.collect()
        self.X = True
        self.Z = normalize(Z).astype(np.float32)
        return self.Z

    def _scores(self, texts):
        from collections import Counter
        c = Counter()
        for t in texts:
            c.update(analyzer(t))
        out = []
        for tok, tf in c.items():
            col = self.col_of.get(_bucket(tok))
            if col is not None:
                out.append((-(1 + np.log(tf)) * float(self.idf[col]), tok))
        out.sort()
        return [t for _, t in out]

    def top_terms(self, row_idx, n=5):
        if not self.doc_fn:
            return []
        return [t.replace("_", " ") for t in self._scores([self.doc_fn(row_idx)])[:n]]

    def centroid_terms(self, idxs, n=6):
        if not self.doc_fn:
            return []
        return [t.replace("_", " ") for t in self._scores([self.doc_fn(i) for i in sorted(idxs)[:60]])[:n]]


# ----------------------------------------------------------------- intent
CUES = {
    "transactional": [
        (r"add to (cart|bag|basket)|buy now|checkout|order now|book now|in stock|out of stock|add to wishlist|"
         r"free shipping|cash on delivery|emi|subscribe now|sign up|download now|get started|request a quote|"
         r"₹\s?\d|rs\.?\s?\d|\$\s?\d|€\s?\d|खरीदें|अभी खरीदें|kharido|order karein", 3.0, "text"),
        (r"/(product|products|p|item|shop|buy|cart|checkout|book|booking|pricing|plans|signup|register)(/|$|-)", 2.0, "url"),
        (r"\b(tool|checker|generator|calculator|finder|analy[sz]er|converter|tester)\b|\btry (it )?free\b", 3.0, "title"),
        (r"/(tools?|calculator|generator|checker|app)/", 2.0, "url"),
    ],
    "commercial": [
        (r"\bbest\b|\btop \d+|\bvs\.?\b|versus|review|reviews|compare|comparison|alternatives?|cheapest|"
         r"affordable|pros and cons|worth it|which is better|सबसे अच्छा|बेस्ट|तुलना|sabse (accha|achha|best)", 2.5, "title"),
        (r"/(best|top|review|reviews|compare|vs|alternatives?)(/|-|$)", 2.0, "url"),
    ],
    "informational": [
        (r"\bhow to\b|\bwhat is\b|\bwhat are\b|\bwhy\b|\bguide\b|tutorial|tips|explained|meaning|definition|"
         r"\bkaise\b|kya hai|kyu|क्या है|कैसे|क्यों|जानिए|\?", 2.5, "title"),
        (r"/(blog|blogs|news|article|articles|guide|guides|learn|how-to|wiki|faq|help|kb|resources?)(/|$|-)", 2.0, "url"),
    ],
    "navigational": [
        (r"^(home|about|about us|contact|contact us|login|log in|sign in|my account|careers|sitemap|"
         r"privacy policy|terms|terms of service|cookie policy)\s*($|[|\-–—:·•,])|हमारे बारे में|संपर्क", 3.0, "title"),
        (r"/(about|about-us|contact|contact-us|login|signin|account|careers|privacy|terms|sitemap|tag|tags|"
         r"category|categories|author)(/|$|-|\.)", 2.5, "url"),
    ],
}
COMPILED = {k: [(re.compile(rx, re.I), w, f) for rx, w, f in v] for k, v in CUES.items()}

# schema.org / Open Graph types the page itself declares. Site-wide types (WebSite, Organization,
# BreadcrumbList, WebPage, ImageObject ...) are on every page and carry no intent, so they are not listed.
SCHEMA_INTENT = {
    "Product": ("transactional", 4.0), "ProductGroup": ("transactional", 4.0), "IndividualProduct": ("transactional", 4.0),
    "Offer": ("transactional", 3.0), "AggregateOffer": ("transactional", 3.0), "SoftwareApplication": ("transactional", 3.0),
    "WebApplication": ("transactional", 3.0), "Event": ("transactional", 2.0), "og:product": ("transactional", 3.0),
    "Review": ("commercial", 3.0), "ItemList": ("commercial", 1.0),
    "Article": ("informational", 3.0), "BlogPosting": ("informational", 3.5), "NewsArticle": ("informational", 3.5),
    "TechArticle": ("informational", 3.5), "HowTo": ("informational", 4.0), "FAQPage": ("informational", 2.0),
    "QAPage": ("informational", 3.5), "Recipe": ("informational", 3.0), "og:article": ("informational", 2.0),
    "AboutPage": ("navigational", 4.0), "ContactPage": ("navigational", 4.0), "ProfilePage": ("navigational", 3.0),
    "CollectionPage": ("navigational", 2.0), "SearchResultsPage": ("navigational", 4.0),
}
INTENTS = ["informational", "commercial", "transactional", "navigational"]


def intent_scores(p, anchors, inlink_count, outlink_ratio, schema=None):
    """Rule scores per intent. Signals: title/H1 + incoming anchors, URL path, body cues, schema.org /
    og:type declarations, length, and link density (outlink_ratio = in-content links per 100 words)."""
    url = p["url"].lower()
    title = f"{p['title'] or ''} {p['h1'] or ''}".lower()
    text = (p["main_text"] or "")[:4000].lower()
    anc = " ".join(anchors[:30]).lower()
    s = dict.fromkeys(INTENTS, 0.0)
    for intent, rules in COMPILED.items():
        for rx, w, field in rules:
            hay = {"url": url, "title": title + " " + anc, "text": text}[field]
            n = len(rx.findall(hay))
            if n:
                s[intent] += w * min(3, n) ** 0.5
            if field == "title" and rx.search(text[:600]):
                s[intent] += 0.5 * w
    has_review = False
    for t in sorted(set(schema or ())):
        hit = SCHEMA_INTENT.get(t)
        if hit:
            s[hit[0]] += hit[1]
            has_review |= t == "Review"
    if has_review and s["transactional"] > 0:
        s["commercial"] -= 3.0  # reviews embedded in a product page are not a review page
    wc = p["word_count"] or 0
    if wc > 700:
        s["informational"] += 1.5
    if wc < 400 and outlink_ratio > 8:
        s["navigational"] += 2.0  # a listing / hub: one in-content link every dozen words
    path = url.split("://", 1)[-1].split("/", 1)[-1].split("?")[0].strip("/")
    if path == "":
        s["navigational"] += 3.0
    return s


def classify_intent(scores):
    ordered = sorted(scores.items(), key=lambda kv: (-kv[1], INTENTS.index(kv[0])))
    best, second = ordered[0], ordered[1]
    if best[1] == 0:
        return "informational", 0.0
    margin = (best[1] - second[1]) / (best[1] + 1e-9)
    return best[0], round(margin, 4)
