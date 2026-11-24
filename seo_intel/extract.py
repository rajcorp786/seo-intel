"""Main-content extraction by learning each site's template (no off-the-shelf extractor).

1. Segment every page into text blocks: block-level elements with their *own*
   text (text not inside a nested block). Each block gets a structural signature:
   the last 6 ancestors as tag#id.class (digits stripped), plus a landmark flag.
2. Learn on a deterministic sample of the site (<= SAMPLE pages):
     exact[(sig, text-hash)] = #pages      -> repeated text = boilerplate
     sig_pages[sig] = #pages, sig_linkdens -> repeated *widgets* whose text varies
                                              (menus with active state, related lists)
3. A block is boilerplate if
     exact >= max(3, EXACT_SHARE*S)    or
     sig on >= WIDGET_SHARE*S pages and mean link density >= 0.45  or
     inside <nav>/<header>/<footer>/<aside> (or ARIA equivalents) with link density >= 0.3
4. Main content = remaining blocks in document order, minus link-heavy short blocks.
Links inside boilerplate blocks are tagged 'nav' (ignored/low weight in the link graph).
"""
from __future__ import annotations

import hashlib
import os
import re
from collections import Counter, defaultdict
from multiprocessing import get_context

from . import parse as P
from .textnorm import tokens

BLOCK_TAGS = {"p", "li", "h1", "h2", "h3", "h4", "h5", "h6", "td", "th", "dd", "dt", "blockquote",
              "pre", "figcaption", "div", "section", "article", "main", "header", "footer", "nav",
              "aside", "ul", "ol", "table", "tr", "form", "address", "caption", "summary", "details", "body"}
LANDMARK_TAGS = {"nav", "header", "footer", "aside"}
LANDMARK_ROLES = {"navigation", "banner", "contentinfo", "complementary", "search", "menu", "menubar"}
SKIP = set(P.DROP_TAGS) | {"button", "select", "option", "label", "input"}
SAMPLE = 400
EXACT_SHARE = 0.25
WIDGET_SHARE = 0.5
DIGITS = re.compile(r"\d+")
WS = re.compile(r"\s+")


def _sig_part(el):
    t = el.tag
    i = el.get("id")
    c = el.get("class")
    s = t
    if i:
        s += "#" + DIGITS.sub("", i)[:30]
    if c:
        cls = sorted(DIGITS.sub("", x)[:30] for x in c.split()[:3])
        s += "." + ".".join(cls)
    return s


def _own(el, acc, anchors):
    if el.text:
        acc.append(el.text)
    for ch in el:
        if isinstance(ch.tag, str) and ch.tag not in SKIP and ch.tag not in BLOCK_TAGS:
            if ch.tag == "a":
                anchors.append(ch)
            _own(ch, acc, anchors)
        if ch.tail:
            acc.append(ch.tail)


def blocks(tree):
    """List of dicts: sig, text (own text, nested blocks excluded), anchors, landmark, ld."""
    out = []
    body = tree.find("body")
    if body is None:
        body = tree
    stack = [(body, [], False)]
    while stack:
        el, path, landmark = stack.pop()
        if not isinstance(el.tag, str) or el.tag in SKIP:
            continue
        role = (el.get("role") or "").lower()
        lm = landmark or el.tag in LANDMARK_TAGS or role in LANDMARK_ROLES
        my = path + [_sig_part(el)]
        if el.tag in BLOCK_TAGS:
            acc, anchors = [], []
            _own(el, acc, anchors)
            text = WS.sub(" ", "".join(acc)).strip()
            if text or anchors:
                lc = sum(len(P.text_of(a)) for a in anchors)
                out.append({"sig": "/".join(my[-6:]), "text": text, "anchors": anchors, "landmark": lm,
                            "ld": min(1.0, lc / max(1, len(text))) if text else 1.0, "tag": el.tag,
                            "pos": len(out)})
        kids = [c for c in el if isinstance(c.tag, str)]
        for c in reversed(kids):
            stack.append((c, my, lm))
    return out


def _h(s):
    return hashlib.md5(s.encode("utf-8", "replace")).hexdigest()[:12]


class TemplateModel:
    def __init__(self):
        self.exact = Counter()
        self.sig_pages = Counter()
        self.sig_ld = defaultdict(float)
        self.n = 0

    def learn(self, trees):
        for tree in trees:
            if tree is None:
                continue
            self.n += 1
            seen_e, seen_s = set(), {}
            for b in blocks(tree):
                if b["text"]:
                    seen_e.add((b["sig"], _h(b["text"])))
                seen_s.setdefault(b["sig"], []).append(b["ld"])
            for k in seen_e:
                self.exact[k] += 1
            for s, lds in seen_s.items():
                self.sig_pages[s] += 1
                self.sig_ld[s] += sum(lds) / len(lds)

    def to_dict(self):
        n = max(1, self.n)
        ex_min = max(3, EXACT_SHARE * n) if n >= 4 else n + 1
        wid_min = max(3, WIDGET_SHARE * n) if n >= 4 else n + 1
        return {"exact": {f"{s}\t{h}" for (s, h), c in self.exact.items() if c >= ex_min},
                "widget": {s for s, c in self.sig_pages.items()
                           if c >= wid_min and self.sig_ld[s] / c >= 0.45},
                "n": self.n}


def is_boiler(b, model):
    if model.get("no_landmarks"):
        return False
    if b["text"] and f"{b['sig']}\t{_h(b['text'])}" in model["exact"]:
        return True
    if b["sig"] in model["widget"]:
        return True
    if b["landmark"] and b["ld"] >= 0.3:
        return True
    if b["landmark"] and len(b["text"]) < 40:
        return True
    return False


def extract(tree, model):
    """Return (main_text, nav_anchor_ids set)."""
    if tree is None:
        return "", set()
    parts = []
    nav_ids = set()
    for b in blocks(tree):
        boiler = is_boiler(b, model)
        if not boiler and not model.get("no_landmarks") and b["ld"] > 0.6 and len(b["text"]) < 200 \
                and len(b["anchors"]) >= 2:
            boiler = True  # link list inside content area (tag cloud, pager)
        if boiler:
            for a in b["anchors"]:
                nav_ids.add(a.get("data-si"))
            continue
        if b["text"]:
            parts.append(b["text"])
    return "\n".join(parts), nav_ids


def anchor_order(tree):
    """Anchors in the same order/filter as parse.parse_page enumerates links."""
    out = []
    for a in tree.iter("a"):
        href = a.get("href")
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        out.append(a)
    return out


# ----------------------------------------------------------------- driver
def _work(args):
    url, blob, model = args
    from .store import Store
    html = Store.unz(blob)
    tree = P.to_tree(html) if html else None
    if tree is None:
        return url, "", [], 0
    order = anchor_order(tree)
    for i, a in enumerate(order):  # stable identity (lxml proxy id() is not stable)
        a.set("data-si", str(i))
    main, nav_ids = extract(tree, model)
    kinds = [(i, "nav" if str(i) in nav_ids else "content") for i in range(len(order))]
    return url, main, kinds, len(tokens(main))


def run_extraction(store, site, log, workers=None):
    rows = store.db.execute(
        "SELECT url FROM pages WHERE site=? AND status=200 AND html IS NOT NULL ORDER BY url", (site,)).fetchall()
    urls = [r[0] for r in rows]
    if not urls:
        return None
    cached = store.get_meta(f"template_model:{site}")
    incremental = store.get_meta(f"mode:{site}") == "incremental" and cached
    if incremental:
        # re-use last run's template; only re-extract pages that changed
        model = {"exact": set(cached["exact"]), "widget": set(cached["widget"]), "n": cached["n"]}
        tm = TemplateModel()
        tm.n = cached["n"]
        urls = [r[0] for r in store.db.execute(
            "SELECT url FROM pages WHERE site=? AND status=200 AND html IS NOT NULL AND "
            "(unchanged=0 OR main_text IS NULL) ORDER BY url", (site,))]
    else:
        sample = sorted(urls, key=lambda u: hashlib.sha1(u.encode()).hexdigest())[:SAMPLE]
        tm = TemplateModel()
        tm.learn(P.to_tree(store.html(u)) for u in sample)
        model = tm.to_dict()
        store.set_meta(f"template_model:{site}", {"exact": sorted(model["exact"]), "widget": sorted(model["widget"]),
                                                   "n": model["n"]})
    if os.environ.get("SEO_INTEL_ABLATE_TEMPLATE") == "1":  # ablation: no template removal
        model = {"exact": set(), "widget": set(), "n": 0}
        model["no_landmarks"] = True
    log(f"[{site}] template {'re-used' if incremental else 'learned'} on {tm.n} pages: {len(model['exact'])} "
        f"repeated blocks, {len(model['widget'])} widget signatures; extracting {len(urls)} pages")
    workers = workers or max(1, min(4, (os.cpu_count() or 2)))
    ctx = get_context("spawn") if os.name == "nt" else get_context("fork")

    def gen():
        for u in urls:
            blob = store.db.execute("SELECT html FROM pages WHERE url=?", (u,)).fetchone()[0]
            yield (u, blob, model)

    n = 0
    with ctx.Pool(workers) as pool:
        store.db.execute("BEGIN")
        for url, main, kinds, wc in pool.imap(_work, gen(), chunksize=16):
            store.db.execute("UPDATE pages SET main_text=?, main_hash=?, word_count=? WHERE url=?",
                             (main, _h(main) if main else None, wc, url))
            if kinds:
                store.db.executemany("UPDATE links SET block=CASE WHEN block='nofollow' THEN 'nofollow-'||? ELSE ? END "
                                     "WHERE src=? AND ord=?", [(k, k, url, i) for i, k in kinds])
            n += 1
            if n % 2000 == 0:
                store.db.execute("COMMIT")
                store.db.execute("BEGIN")
        store.db.execute("COMMIT")
    store.set_meta(f"template:{site}", {"sample": tm.n, "exact": len(model["exact"]), "widget": len(model["widget"])})
    return model
