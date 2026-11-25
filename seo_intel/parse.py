"""Fast HTML parsing (lxml) for crawl-time fields + render-need signals."""
from __future__ import annotations

import json
import re

from lxml import html as LH
from lxml import etree

JS_REDIRECT = re.compile(
    r"""(?:window\.|document\.|top\.|self\.)?location(?:\.href)?\s*=\s*['"]([^'"]+)['"]|"""
    r"""location\.(?:replace|assign)\(\s*['"]([^'"]+)['"]\s*\)""")
FRAMEWORK_MARKERS = re.compile(
    r"""id=["'](?:root|app|__next|__nuxt|svelte|ember-app)["']\s*>\s*</|ng-app|data-reactroot|"""
    r"""__NEXT_DATA__|window\.__NUXT__|window\.__INITIAL_STATE__|window\.__APOLLO_STATE__|"""
    r"""<app-root|data-server-rendered|hydrate""", re.I)
HANDLER = re.compile(r"addEventListener|\.on\(|onclick|click|submit|function\s+\w+\s*\(|\bif\s*\(", re.I)
AUTH_NAV = re.compile(r"^/(url|account|customer_authentication|login|log-in|signin|sign-in|auth|oauth|sso|consent)"
                      r"(/|\?|$)|[?&](sso\w*|return_to|redirect_uri|continue|state)=", re.I)
NOSCRIPT_JS = re.compile(r"enable javascript|requires javascript|javascript is (?:disabled|required)", re.I)
WS = re.compile(r"\s+")
DROP_TAGS = ("script", "style", "noscript", "template", "svg", "iframe", "canvas")


def to_tree(html_text: str):
    if not html_text or not html_text.strip():
        return None
    try:
        return LH.document_fromstring(html_text.encode("utf-8", "replace"),
                                      parser=LH.HTMLParser(encoding="utf-8", remove_comments=True))
    except (etree.ParserError, ValueError):
        return None


def text_of(el) -> str:
    return WS.sub(" ", el.text_content() or "").strip()


def visible_text(tree) -> str:
    if tree is None:
        return ""
    body = tree.find("body")
    if body is None:
        body = tree
    parts = []
    for t in body.iter():
        if not isinstance(t.tag, str):
            continue
        if t.tag in DROP_TAGS:
            continue
        if t.text and not _in_dropped(t):
            parts.append(t.text)
        if t.tail and t.getparent() is not None and not _in_dropped(t.getparent()):
            parts.append(t.tail)
    return WS.sub(" ", " ".join(parts)).strip()


def _in_dropped(el):
    while el is not None:
        if isinstance(el.tag, str) and el.tag in DROP_TAGS:
            return True
        el = el.getparent()
    return False


def _ld_types(node, acc, depth=0):
    if depth > 6 or len(acc) > 40:
        return
    if isinstance(node, list):
        for x in node[:50]:
            _ld_types(x, acc, depth + 1)
    elif isinstance(node, dict):
        t = node.get("@type")
        for x in (t if isinstance(t, list) else [t]):
            if isinstance(x, str) and x:
                acc.add(x.rsplit("/", 1)[-1].rsplit(":", 1)[-1][:40])
        for k in ("@graph", "mainEntity", "itemListElement", "item", "offers"):
            if k in node:
                _ld_types(node[k], acc, depth + 1)


_TRAILING_COMMA = re.compile(r",\s*([}\]])")


def schema_types(tree):
    """schema.org types a page declares (JSON-LD @type incl. @graph / mainEntity, microdata itemtype) and its
    og:type as 'og:<type>'. Sorted, so the stored value never depends on markup order."""
    acc = set()
    for sc in tree.iter("script"):
        if (sc.get("type") or "").lower().strip() == "application/ld+json" and sc.text:
            raw = sc.text.strip().rstrip(";")
            try:
                _ld_types(json.loads(raw), acc)
            except (ValueError, RecursionError):
                # many CMS templates leave a trailing comma ("},\n]"): read it the forgiving way, so a
                # one-character edit to the markup does not change what the page is
                try:
                    _ld_types(json.loads(_TRAILING_COMMA.sub(r"\1", raw)), acc)
                except (ValueError, RecursionError):
                    continue
    for el in tree.xpath("//*[@itemtype]")[:100]:
        for t in (el.get("itemtype") or "").split():
            acc.add(t.rstrip("/").rsplit("/", 1)[-1][:40])
    for m in tree.iter("meta"):
        if (m.get("property") or "").lower() == "og:type" and m.get("content"):
            acc.add("og:" + m.get("content").strip().lower()[:30])
    return sorted(a for a in acc if a)


def parse_page(html_text: str, url: str):
    """Return dict with SEO fields + raw outlinks [(href, anchor)] + render signals."""
    out = {"title": None, "h1": None, "meta_robots": None, "canonical": None, "meta_refresh": None,
           "js_redirect": None, "hreflang": [], "lang": None, "links": [], "signals": {}, "schema": []}
    tree = to_tree(html_text)
    if tree is None:
        out["signals"] = {"text_len": 0, "scripts": 0, "framework": False, "noscript_js": False,
                          "html_len": len(html_text or "")}
        return out, None
    head_title = tree.find(".//title")
    if head_title is not None:
        out["title"] = text_of(head_title)[:500] or None
    h1 = tree.find(".//h1")
    if h1 is not None:
        out["h1"] = text_of(h1)[:500] or None
    root = tree.getroottree().getroot()
    out["lang"] = (root.get("lang") or "").strip().lower() or None
    robots_vals = []
    for m in tree.iter("meta"):
        name = (m.get("name") or "").lower()
        he = (m.get("http-equiv") or "").lower()
        if name in ("robots", "googlebot"):
            robots_vals.append((m.get("content") or "").lower())
        if he == "refresh":
            c = m.get("content") or ""
            mm = re.search(r"url\s*=\s*['\"]?([^'\";]+)", c, re.I)
            delay = re.match(r"\s*(\d+)", c)
            if mm:
                out["meta_refresh"] = mm.group(1).strip()
            elif delay:
                out["meta_refresh"] = None
    out["meta_robots"] = ",".join(robots_vals) or None
    for l in tree.iter("link"):
        rel = (l.get("rel") or "").lower().split()
        if "canonical" in rel and out["canonical"] is None and l.get("href"):
            out["canonical"] = l.get("href").strip()
        if "alternate" in rel and l.get("hreflang") and l.get("href"):
            out["hreflang"].append((l.get("hreflang").strip().lower(), l.get("href").strip()))
    out["schema"] = schema_types(tree)
    scripts = 0
    for s in tree.iter("script"):
        scripts += 1
        # only small, unconditional scripts count: code inside click/submit handlers or named
        # functions is not a redirect that runs on page load
        if s.text and out["js_redirect"] is None and len(s.text) < 600 and not HANDLER.search(s.text):
            m = JS_REDIRECT.search(s.text)
            if m:
                out["js_redirect"] = (m.group(1) or m.group(2)).strip()
    for a in tree.iter("a"):
        href = a.get("href")
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        rel = (a.get("rel") or "").lower()
        out["links"].append((href.strip(), text_of(a)[:200], "nofollow" in rel))
    vt = visible_text(tree)
    noscript = " ".join(text_of(n) for n in tree.iter("noscript"))
    out["signals"] = {"text_len": len(vt), "scripts": scripts,
                      "framework": bool(FRAMEWORK_MARKERS.search(html_text[:200000])),
                      "noscript_js": bool(NOSCRIPT_JS.search(noscript)), "html_len": len(html_text)}
    return out, tree
