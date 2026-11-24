"""Sitemap discovery (robots Sitemap: lines + conventional paths), index recursion, gzip."""
from __future__ import annotations

import gzip

from lxml import etree

MAX_FILES = 60


def parse_sitemap(body: bytes):
    """Return (urls[(loc,lastmod)], child_sitemaps[loc])."""
    if body[:2] == b"\x1f\x8b":
        try:
            body = gzip.decompress(body)
        except OSError:
            return [], []
    try:
        root = etree.fromstring(body, parser=etree.XMLParser(recover=True, resolve_entities=False, huge_tree=True))
    except etree.XMLSyntaxError:
        return [], []
    if root is None:
        return [], []
    urls, kids = [], []
    tag = etree.QName(root).localname.lower() if isinstance(root.tag, str) else ""
    for el in root:
        if not isinstance(el.tag, str):
            continue
        loc = lastmod = None
        for c in el:
            if not isinstance(c.tag, str):
                continue
            n = etree.QName(c).localname.lower()
            if n == "loc" and c.text:
                loc = c.text.strip()
            elif n == "lastmod" and c.text:
                lastmod = c.text.strip()[:25]
        if not loc:
            continue
        if tag == "sitemapindex":
            kids.append(loc)
        else:
            urls.append((loc, lastmod))
    return urls, kids


async def discover(fetcher, start_sitemaps, max_urls, deadline_ok):
    seen, out = set(), []
    queue = list(dict.fromkeys(start_sitemaps))
    files = 0
    while queue and files < MAX_FILES and len(out) < max_urls and deadline_ok():
        sm = queue.pop(0)
        if sm in seen:
            continue
        seen.add(sm)
        r = await fetcher.fetch(sm, any_body=True)
        files += 1
        if r.get("status") != 200 or not r.get("body"):
            continue
        urls, kids = parse_sitemap(r["body"])
        out.extend(urls[: max_urls - len(out)])
        queue.extend(k for k in kids if k not in seen)
    return out, files
