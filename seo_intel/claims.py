"""Structured claims + a verifier that re-derives every claim from the crawl store.

Claim kinds (all values are re-computed by code, never trusted):
  field        {url, field, value}        status, final_url, canonical, title, h1, meta_robots,
                                          x_robots, lang, word_count, redirect_hops (3xx responses only),
                                          redirect_leaves_site, soft404,
                                          canonical_target_status, intent
  graph        {url, metric, value}       pagerank_x1000 (4dp), click_depth, content_inlinks,
                                          content_outlinks, inlinks_to_redirect
  pair         {url_a, url_b, metric, value}  content_overlap (2dp), topic_similarity (3dp),
                                          title_overlap (2dp), severity (1dp), link_exists
  sentence     {url, value}               exact sentence present in the page's main content
  anchor       {url, sentence, value}     anchor text is an exact substring of that sentence
  chain        {url, metric, value}       redirect_chain / canonical_chain: list of URLs
  gap          {gap_id, metric, value}    competitor_pages, competitor_words_total, target_pages,
                                          best_target_similarity (3dp), competitor_url (membership)
  pr_after     {urls, value}              total pagerank_x1000 of these URLs after adding the plan
                                          links, recomputed on a freshly rebuilt graph
"""
from __future__ import annotations

import json

import numpy as np

PRECISION = {"pagerank_x1000": 4, "content_overlap": 2, "topic_similarity": 3, "title_overlap": 2,
             "severity": 1, "best_target_similarity": 3, "pr_after": 4}


def ckey(c):
    return json.dumps({k: v for k, v in c.items() if k != "id"}, sort_keys=True, ensure_ascii=False)


class Ledger:
    def __init__(self):
        self.claims = {}
        self.by_key = {}

    def add(self, c):
        k = ckey(c)
        if k in self.by_key:
            return self.by_key[k]
        cid = f"C{len(self.claims) + 1}"
        c = dict(c)
        c["id"] = cid
        self.claims[cid] = c
        self.by_key[k] = cid
        return cid

    def get(self, cid):
        return self.claims.get(cid)


def _round(metric, v):
    p = PRECISION.get(metric)
    if p is None or v is None or isinstance(v, (bool, str, list)):
        return v
    return round(float(v), p)


class Verifier:
    def __init__(self, ctx, log):
        self.ctx = ctx
        self.log = log
        self.rejections = []

    def actual(self, c):
        ctx = self.ctx
        k = c.get("kind")
        if k == "field":
            return ctx.page_field(c["url"], c["field"])
        if k == "graph":
            return _round(c["metric"], ctx.graph_metric(c["url"], c["metric"]))
        if k == "pair":
            return _round(c["metric"], ctx.pair_metric(c["url_a"], c["url_b"], c["metric"]))
        if k == "sentence":
            mt = ctx.page_field(c["url"], "main_text") or ""
            return c["value"] if c["value"] and c["value"] in mt else None
        if k == "anchor":
            mt = ctx.page_field(c["url"], "main_text") or ""
            ok = c["sentence"] in mt and c["value"] and c["value"] in c["sentence"]
            return c["value"] if ok else None
        if k == "title_phrase":
            import re as _re
            toks = lambda x: " ".join(_re.findall(r"\w+", (x or "").lower()))
            p = ctx.page(c["url"])
            hay = f" {toks(p['title'])} | {toks(p['h1'])} " if p else ""
            return c["value"] if c["value"] and f" {toks(c['value'])} " in hay else None
        if k == "chain":
            return ctx.chain(c["url"], c["metric"])
        if k == "gap":
            return _round(c["metric"], ctx.gap_metric(c["gap_id"], c["metric"], c.get("value")))
        if k == "pr_after":
            return _round("pr_after", ctx.pr_after(c["urls"]))
        if k == "pr_before":
            return _round("pr_after", ctx.pr_before(c["urls"]))
        raise KeyError(f"unknown claim kind {k}")

    def verify(self, c):
        try:
            a = self.actual(c)
        except Exception as e:  # unknown url/metric => claim cannot be supported
            a = f"<error {type(e).__name__}: {e}>"
        v = c.get("value")
        metric = c.get("metric") or c.get("field")
        if isinstance(a, np.generic):
            a = a.item()
        if isinstance(v, list) or isinstance(a, list):
            ok = a == v
        elif isinstance(v, bool) or isinstance(a, bool):
            ok = isinstance(v, bool) and isinstance(a, bool) and a == v
        elif isinstance(v, (int, float)) and isinstance(a, (int, float)):
            ok = abs(_round(metric, v) - _round(metric, a)) < 1e-9
        else:
            ok = a == v
        if not ok:
            rec = {"claim": c, "actual": a if not isinstance(a, np.generic) else a.item()}
            self.rejections.append(rec)
            self.log(f"REJECTED {c.get('id', '?')}: {json.dumps(rec, ensure_ascii=False, default=str)[:400]}")
        return ok
