"""Spider-trap detection, decided BEFORE fetching so the budget is protected.

Detectors (all site-agnostic, deterministic):
  session   - session-id params/path params (stripped by urlnorm, reported here)
  calendar  - date-like path/query values; a template with > CAL_LIMIT distinct
              date values is a trap (infinite next/prev month links)
  facet     - >= FACET_PARAMS non-paging params; 2-param combinations are sampled 1-in-FACET_SAMPLE
              by URL hash (faceted-filter explosion, decided without depending on crawl order)
  path      - depth > MAX_DEPTH_SEGS, a segment repeated >= 3 times, or a repeating
              segment sequence (/a/b/a/b/), or path length > 300
  template  - a URL template whose fetched pages are >= 80% identical main content
              (catches anything the rules above miss: "infinite" near-identical pages)
"""
from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from urllib.parse import parse_qsl, urlsplit

from .urlnorm import template

PAGING = re.compile(r"^(p|pg|page|paged|pagenum|page_no|pageno|start|offset|from|limit|per_page)$", re.I)
DATE_RE = re.compile(r"(?<!\d)(19|20)\d{2}[-/_.]?(0[1-9]|1[0-2])([-/_.]?(0[1-9]|[12]\d|3[01]))?(?!\d)")
DATE_KEYS = re.compile(r"^(date|day|month|year|yr|mon|week|cal|calendar|ym|d|m|y|start_date|end_date|from_date|to_date)$", re.I)

CAL_LIMIT = 24
CAL_CHAIN = 3
FACET_PARAMS = 3
FACET_SAMPLE = 4
MAX_DEPTH_SEGS = 12
TEMPLATE_MIN = 12
TEMPLATE_DUP = 0.8


class TrapDetector:
    def __init__(self):
        self.cal = defaultdict(set)
        self.cal_min_depth = {}
        self.facet = defaultdict(set)
        self.blocked_templates = {}
        self.tpl_hashes = defaultdict(list)
        self.trapped_fetches = 0

    def check(self, url: str, depth: int | None = None):
        """Return None if OK, else (kind, pattern)."""
        sp = urlsplit(url)
        path = sp.path
        segs = [s for s in path.split("/") if s]
        tpl = template(url)
        if tpl in self.blocked_templates:
            return self.blocked_templates[tpl]
        # growing / repeating paths
        if len(path) > 300 or len(segs) > MAX_DEPTH_SEGS:
            return ("path", f"depth>{MAX_DEPTH_SEGS} or len>300: {_shorten(tpl)}")
        counts = defaultdict(int)
        for s in segs:
            counts[s] += 1
        rep = [s for s, c in counts.items() if c >= 3]
        if rep:
            return ("path", f"segment '{sorted(rep)[0]}' repeated >=3x")
        for L in (2, 3):
            for i in range(0, len(segs) - 2 * L + 1):
                if segs[i:i + L] == segs[i + L:i + 2 * L]:
                    return ("path", f"repeating sequence /{'/'.join(segs[i:i + L])}/")
        params = parse_qsl(sp.query, keep_blank_values=True)
        # calendar
        date_vals = DATE_RE.findall(path) or [v for k, v in params if DATE_KEYS.match(k) or DATE_RE.search(v)]
        if date_vals:
            key = re.sub(r"\d", "N", path) + "?" + ",".join(sorted(k for k, _ in params))
            if depth is not None:
                md = self.cal_min_depth.setdefault(key, depth)
                if depth < md:
                    self.cal_min_depth[key] = depth
                elif depth >= md + CAL_CHAIN:
                    # next/prev chains: each date page discovered only from the previous one
                    return ("calendar", f"date chain {_shorten(key)} (discovered {CAL_CHAIN}+ levels deep via "
                                        f"next/prev links)")
            self.cal[key].add(url)
            if len(self.cal[key]) > CAL_LIMIT:
                pat = ("calendar", f"date pattern {_shorten(key)} (> {CAL_LIMIT} dates)")
                return pat
        # facets
        non_paging = [k for k, _ in params if not PAGING.match(k)]
        if len(set(non_paging)) >= FACET_PARAMS:
            return ("facet", f"{path}?{'&'.join(sorted(set(non_paging)))} ({len(set(non_paging))} filter params)")
        if len(set(non_paging)) == 2:
            # order-independent sampling of 2-filter combinations (keeps runs repeatable)
            h = int(hashlib.sha1(url.encode()).hexdigest()[:8], 16)
            if h % FACET_SAMPLE:
                return ("facet", f"{path}?{'&'.join(sorted(set(non_paging)))} (2-filter combination, "
                                 f"1 in {FACET_SAMPLE} kept)")
        return None

    def observe(self, url: str, content_hash: str | None):
        """After a fetch: template-level near-identical content => trap template."""
        if not content_hash:
            return None
        tpl = template(url)
        if "{n}" not in tpl and "?" not in tpl and "{h}" not in tpl:
            return None
        hs = self.tpl_hashes[tpl]
        hs.append(content_hash)
        if len(hs) >= TEMPLATE_MIN and tpl not in self.blocked_templates:
            top = max(hs.count(h) for h in set(hs))
            if top / len(hs) >= TEMPLATE_DUP:
                self.blocked_templates[tpl] = ("template", f"{_shorten(tpl)} -> {top}/{len(hs)} identical pages")
                return self.blocked_templates[tpl] + (len(hs),)
        return None


def _shorten(s, n=120):
    return s if len(s) <= n else s[:n] + "..."
