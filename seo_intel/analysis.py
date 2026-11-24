"""Runs Parts B-D over the crawl store and exposes a verification Context for Part E."""
from __future__ import annotations

import json
from urllib.parse import urlsplit

import numpy as np

from . import cannibal, dedup, extract, gaps as gapmod, linkopt, soft404, topics
from . import parse as P
from .graph import LinkGraph, pagerank, save_graph_stats
from .textnorm import script_profile, tokens

LIGHT = "url, site, status, final_url, chain, canonical, title, h1, meta_robots, x_robots, lang, hreflang, " \
        "word_count, soft404, depth"


class Context:
    def __init__(self, store, cfg, log, llm):
        self.store, self.cfg, self.log, self.llm = store, cfg, log, llm
        self.graphs, self.graphinfo, self.anchors = {}, {}, {}
        self.zi, self.Z, self.space = {}, None, None
        self.sigs, self.intents = {}, {}
        self.groups, self.pairs, self.gaps, self.plan = [], [], [], {}
        self._pr_cache = {}
        self.results = {}

    # ------------------------------------------------------------ lookups
    def page(self, url):
        return self.store.db.execute(f"SELECT {LIGHT} FROM pages WHERE url=?", (url,)).fetchone()

    def page_field(self, url, field):
        if field == "main_text":
            r = self.store.db.execute("SELECT main_text FROM pages WHERE url=?", (url,)).fetchone()
            return r[0] if r else None
        if field == "intent":
            return self.intents.get(url, (None,))[0]
        p = self.page(url)
        if p is None:
            return None
        if field == "redirect_hops":
            return len(json.loads(p["chain"] or "[]"))
        if field == "soft404":
            return bool(p["soft404"])
        if field == "canonical_target_status":
            if not p["canonical"]:
                return None
            q = self.page(p["canonical"])
            return q["status"] if q else "not_crawled"
        if field in ("status", "final_url", "canonical", "title", "h1", "meta_robots", "x_robots", "lang",
                     "word_count"):
            return p[field]
        raise KeyError(field)

    def graph_metric(self, url, metric):
        if metric == "inlinks_to_redirect":
            return self.store.count("SELECT COUNT(DISTINCT src) FROM links WHERE dst=?", (url,))
        r = self.store.db.execute("SELECT pagerank, click_depth, content_inlinks, content_outlinks FROM graph "
                                  "WHERE url=?", (url,)).fetchone()
        if r is None:
            return None
        return {"pagerank_x1000": r[0] * 1000, "click_depth": r[1], "content_inlinks": r[2],
                "content_outlinks": r[3]}[metric]

    def pair_metric(self, a, b, metric):
        if metric == "content_link_exists":
            g = self.graphs[self.page(a)["site"]]
            return bool(g.has_content_edge(g.idx[a], g.idx[g.resolve.get(b, b)]))
        if metric == "content_overlap":
            return float(np.mean(self.sigs[a] == self.sigs[b])) if a in self.sigs and b in self.sigs else 0.0
        if metric == "topic_similarity":
            return float(self.Z[self.zi[a]] @ self.Z[self.zi[b]])
        pa, pb = self.page(a), self.page(b)
        if metric == "title_overlap":
            return cannibal.jac(cannibal.kw(pa), cannibal.kw(pb))
        if metric == "severity":
            sim = float(self.Z[self.zi[a]] @ self.Z[self.zi[b]])
            ov = self.pair_metric(a, b, "content_overlap")
            ia, ib = sorted((a, b))
            f = cannibal.pair_features(ia, ib, self.page(ia), self.page(ib), sim, ov, self.graphinfo,
                                       self.anchors, {u: v[0] for u, v in self.intents.items()})
            return cannibal.severity(f)
        raise KeyError(metric)

    def chain(self, url, metric):
        if metric == "redirect_chain":
            p = self.page(url)
            if not p:
                return None
            ch = json.loads(p["chain"] or "[]")
            return [c[0] for c in ch] + [p["final_url"]] if ch else None
        if metric == "canonical_chain":
            out, cur = [url], url
            for _ in range(10):
                p = self.page(cur)
                if not p or not p["canonical"] or p["canonical"] == cur:
                    break
                cur = p["canonical"]
                out.append(cur)
                if cur in out[:-1]:
                    break
            return out if len(out) > 1 else None
        raise KeyError(metric)

    def gap_metric(self, gid, metric, value=None):
        g = next((x for x in self.gaps if x["id"] == gid), None)
        if g is None:
            return None
        if metric == "competitor_url":
            ok = value in g["competitor_urls"] and self.page(value) is not None \
                 and self.page(value)["site"] == "competitor"
            return value if ok else None
        if metric == "competitor_pages":
            # recount from the store: competitor 200 pages assigned to this cluster
            return sum(1 for u in g["competitor_urls"] if (self.page(u) or {"status": 0})["status"] == 200)
        if metric == "competitor_words_total":
            return sum((self.page(u)["word_count"] or 0) for u in g["competitor_urls"])
        return g[metric]

    def _pr(self, urls, with_plan):
        site = self.page(urls[0])["site"]
        key = (site, with_plan)
        if key not in self._pr_cache:
            g = LinkGraph(self.store, site)  # fresh rebuild from the store (independent of optimiser state)
            extra = []
            if with_plan:
                for l in self.plan.get("links", []):
                    extra.append((g.idx[l["source"]], g.idx[l["target"]]))
            self._pr_cache[key] = (g.idx, pagerank(g.matrix(extra)))
        idx, pr = self._pr_cache[key]
        return float(sum(pr[idx[u]] for u in urls)) * 1000

    def pr_after(self, urls):
        return self._pr(urls, True)

    def pr_before(self, urls):
        return self._pr(urls, False)


# ===================================================================== stages
def mem_mb():
    import psutil
    p = psutil.Process()
    return p.memory_info().rss / 2 ** 20


def run_all(store, cfg, log, llm):
    ctx = Context(store, cfg, log, llm)
    R = ctx.results
    models = {}
    for site in ("target", "competitor"):
        models[site] = extract.run_extraction(store, site, log, workers=cfg.workers)
        log(f"  mem after extraction {site}: {mem_mb():.0f} MB")
    stage_soft404(ctx, models)
    log(f"  mem after stage_soft404: {mem_mb():.0f} MB")
    stage_dedup(ctx)
    log(f"  mem after stage_dedup: {mem_mb():.0f} MB")
    for site in ("target", "competitor"):
        g = LinkGraph(store, site)
        ctx.graphs[site] = g
        home = store.get_meta(f"start_url:{site}")
        pr, depth = save_graph_stats(store, site, g, home)
        inl = g.content_inlinks()
        for i, u in enumerate(g.nodes):
            ctx.graphinfo[u] = {"pagerank": float(pr[i]), "click_depth": int(depth[i]),
                                "content_inlinks": int(inl[i])}
        ctx.anchors.update(g.incoming_anchors)
        R[f"graph_{site}"] = {"nodes": g.n, "content_edges": int(len(g.content_keys)),
                              "all_edges": int(len(g.all_keys)),
                              "unreachable_from_home": int((depth < 0).sum()),
                              "max_click_depth": int(depth.max()) if g.n else 0}
    stage_topics(ctx)
    log(f"  mem after stage_topics: {mem_mb():.0f} MB")
    stage_intents(ctx)
    log(f"  mem after stage_intents: {mem_mb():.0f} MB")
    stage_cannibal(ctx)
    log(f"  mem after stage_cannibal: {mem_mb():.0f} MB")
    stage_gaps(ctx)
    log(f"  mem after stage_gaps: {mem_mb():.0f} MB")
    stage_links(ctx)
    log(f"  mem after stage_links: {mem_mb():.0f} MB")
    return ctx


def stage_soft404(ctx, models):
    st = ctx.store
    n = 0
    for site in ("target", "competitor"):
        probe = st.get_meta(f"probe404:{site}") or {}
        ptoks = None
        if probe.get("html") and models.get(site):
            main, _ = extract.extract(P.to_tree(probe["html"]), models[site])
            ptoks = tokens(main)
        home = st.get_meta(f"start_url:{site}")
        res = []
        for p in st.db.execute("SELECT url, title, h1, main_text, meta_refresh, js_redirect FROM pages WHERE "
                               "site=? AND status=200 AND html IS NOT NULL ORDER BY url", (site,)):
            moved = (p["meta_refresh"] and p["meta_refresh"] != p["url"]) or (p["js_redirect"] and p["js_redirect"] != p["url"])
            r = None if moved else soft404.classify(p, ptoks, p["url"] == home)
            res.append((1 if r else 0, r, p["url"]))
            n += bool(r)
        st.db.execute("BEGIN")
        st.db.executemany("UPDATE pages SET soft404=?, soft404_reason=? WHERE url=?", res)
        st.db.execute("COMMIT")
        ctx.results[f"probe404_{site}"] = {"url": probe.get("url"), "status": probe.get("status")}
    ctx.log(f"soft-404: {n} pages flagged")


def stage_dedup(ctx):
    mh = dedup.MinHasher(ctx.cfg.seed)
    st = ctx.store
    out = {}
    for site in ("target", "competitor"):
        sigs = {}
        st.db.execute("CREATE TABLE IF NOT EXISTS sigs(url TEXT PRIMARY KEY, main_hash TEXT, sig BLOB)")
        cache = {u: (h, b) for u, h, b in st.db.execute("SELECT url, main_hash, sig FROM sigs")}
        new = []
        todo = []
        for u, h in st.db.execute("SELECT url, main_hash FROM pages WHERE site=? AND status=200 AND "
                                  "word_count>=20 ORDER BY url", (site,)):
            c = cache.get(u)
            if c and c[0] == h:
                sigs[u] = np.frombuffer(c[1], dtype=np.uint64)
            else:
                todo.append((u, h))
        for u, h, sig in _parallel_signatures(st, todo, ctx.cfg.seed, ctx.cfg.workers):
            if sig is not None:
                sigs[u] = np.frombuffer(sig, dtype=np.uint64)
                new.append((u, h, sig))
        if new:
            st.db.execute("BEGIN")
            st.db.executemany("INSERT OR REPLACE INTO sigs VALUES(?,?,?)", new)
            st.db.execute("COMMIT")
        ctx.sigs.update(sigs)
        pairs, ncand = dedup.lsh_pairs(sigs, threshold=dedup.OVERLAP_T)
        out[site] = {"pages_signed": len(sigs), "lsh_candidates": ncand,
                     "near_duplicates": [p for p in pairs if p[2] >= dedup.NEAR_DUP_T],
                     "high_overlap": [p for p in pairs if dedup.OVERLAP_T <= p[2] < dedup.NEAR_DUP_T]}
        if site == "target":
            pool = [u for (u,) in st.db.execute(
                "SELECT url FROM pages WHERE site='target' AND status=200 AND word_count>=60 ORDER BY url")]
            import hashlib as _hl
            pool = sorted(pool, key=lambda u: _hl.sha1(u.encode()).hexdigest())[:2000]
            texts = [(u, st.db.execute("SELECT main_text FROM pages WHERE url=?", (u,)).fetchone()[0])
                     for u in sorted(pool)]
            planted = dedup.plant_duplicates(texts, n=200, seed=ctx.cfg.seed)
            psigs = dict(sigs)
            for src, pid, txt in planted:
                s = mh.signature(txt)
                if s is not None:
                    psigs[pid] = s
            pp, _ = dedup.lsh_pairs(psigs, threshold=dedup.OVERLAP_T)
            found = {(a, b) for a, b, _ in pp} | {(b, a) for a, b, _ in pp}
            hit = sum(1 for src, pid, _ in planted if (src, pid) in found)
            out["planted_recall"] = {"planted": len(planted), "found": hit,
                                     "recall": round(hit / len(planted), 4) if planted else None,
                                     "threshold": dedup.OVERLAP_T}
    ctx.results["dedup"] = out
    ctx.dup_pairs = out["target"]["near_duplicates"] + out["target"]["high_overlap"]
    ctx.log(f"dedup: target near-dups {len(out['target']['near_duplicates'])}, "
            f"planted recall {out.get('planted_recall', {}).get('recall')}")


def _sig_worker(args):
    path, urls, seed = args
    import sqlite3
    db = sqlite3.connect(path)
    mh = dedup.MinHasher(seed)
    out = []
    for u, h in urls:
        r = db.execute("SELECT main_text FROM pages WHERE url=?", (u,)).fetchone()
        s = mh.signature(r[0] if r else "")
        out.append((u, h, s.tobytes() if s is not None else None))
    db.close()
    return out


def _parallel_signatures(st, todo, seed, workers):
    import os
    from multiprocessing import get_context
    if len(todo) < 2000:
        mh = dedup.MinHasher(seed)
        for u, h in todo:
            r = st.db.execute("SELECT main_text FROM pages WHERE url=?", (u,)).fetchone()
            s = mh.signature(r[0] if r else "")
            yield u, h, (s.tobytes() if s is not None else None)
        return
    w = workers or max(1, min(4, os.cpu_count() or 2))
    chunks = [(st.path, todo[i:i + 1000], seed) for i in range(0, len(todo), 1000)]
    ctx = get_context("spawn") if os.name == "nt" else get_context("fork")
    with ctx.Pool(w) as pool:
        for part in pool.imap(_sig_worker, chunks):
            yield from part


def _pages_for_topics(st):
    return st.db.execute("SELECT url, site, title, h1, main_text, word_count FROM pages WHERE status=200 AND "
                         "html IS NOT NULL AND soft404=0 ORDER BY site DESC, url").fetchall()


def trap_templates(st):
    from .urlnorm import template
    return {template(e) for k, e in st.db.execute("SELECT kind, example FROM traps")
            if k in ("calendar", "template", "path")}


def stage_topics(ctx):
    st = ctx.store
    from .urlnorm import template
    tt = trap_templates(st)
    urls = [(r[0], r[1]) for r in st.db.execute(
        "SELECT url, site, meta_refresh, js_redirect FROM pages WHERE status=200 AND html IS NOT NULL AND soft404=0 "
        "ORDER BY site DESC, url")
        if template(r[0]) not in tt and not (r[2] and r[2] != r[0]) and not (r[3] and r[3] != r[0])]
    ctx.results["excluded_trap_pages"] = st.count(
        "SELECT COUNT(*) FROM pages WHERE status=200 AND html IS NOT NULL AND soft404=0") - len(urls)
    ctx.zi = {u: i for i, (u, _) in enumerate(urls)}
    ctx.zsite = [s for _, s in urls]

    def text(u):
        p = st.db.execute("SELECT url, title, h1, main_text FROM pages WHERE url=?", (u,)).fetchone()
        return topics.doc_text(p, ctx.anchors.get(u, []))

    def gen():
        for u, _ in urls:
            yield text(u)

    sp = topics.TopicSpace(ctx.cfg.seed)
    if len(urls) >= 3:
        ctx.Z = sp.fit(gen(), doc_fn=lambda i: text(urls[i][0]))
    else:
        ctx.Z = np.zeros((len(urls), 2))
        sp.X = None
    ctx.space = sp
    ctx.results["topics"] = {"docs": len(urls), "dims": int(ctx.Z.shape[1])}


def stage_intents(ctx):
    st = ctx.store
    rows = st.db.execute("SELECT url, site, title, h1, substr(main_text, 1, 4000) AS main_text, word_count FROM pages "
                         "WHERE status=200 AND html IS NOT NULL AND soft404=0 ORDER BY url")
    low = []
    n_rows = 0
    for p in rows:
        n_rows += 1
        g = ctx.graphinfo.get(p["url"], {})
        outr = 0.0
        sc = topics.intent_scores(p, ctx.anchors.get(p["url"], []), g.get("content_inlinks", 0), outr)
        it, margin = topics.classify_intent(sc)
        ctx.intents[p["url"]] = (it, margin, "rules")
        low.append((margin, p["url"]))
    budget = int(0.02 * n_rows)
    low.sort()
    used = 0
    if ctx.llm.enabled and budget > 0:
        cand = [u for m, u in low if m < 0.25][:budget]
        for i in range(0, len(cand), 20):
            batch = cand[i:i + 20]
            res = llm_intents(ctx, batch)
            if res is None:
                break
            for u, it in res.items():
                if it in topics.INTENTS:
                    ctx.intents[u] = (it, ctx.intents[u][1], "llm")
                    used += 1
    st.db.execute("CREATE TABLE IF NOT EXISTS intent(url TEXT PRIMARY KEY, intent TEXT, margin REAL, source TEXT)")
    st.db.execute("BEGIN")
    st.db.execute("DELETE FROM intent")
    st.db.executemany("INSERT INTO intent VALUES(?,?,?,?)", [(u, *v) for u, v in sorted(ctx.intents.items())])
    st.db.execute("COMMIT")
    dist = {}
    for v in ctx.intents.values():
        dist[v[0]] = dist.get(v[0], 0) + 1
    ctx.results["intents"] = {"pages": n_rows, "llm_classified": used,
                              "llm_share": round(used / max(1, n_rows), 4), "distribution": dist}


def llm_intents(ctx, urls):
    items = []
    for u in urls:
        p = ctx.store.db.execute("SELECT title, h1, substr(main_text,1,300) FROM pages WHERE url=?", (u,)).fetchone()
        items.append({"url": u, "title": (p[0] or "")[:120], "h1": (p[1] or "")[:120], "text": (p[2] or "")[:300]})
    system = ("Classify each web page's search intent as exactly one of: informational, commercial, "
              "transactional, navigational. Reply with JSON object {url: intent} only.")
    resp = ctx.llm.call(system, [{"role": "user", "content": json.dumps(items, ensure_ascii=False)}],
                        max_tokens=1200)
    from .llm import parse_json, text_of
    data = parse_json(text_of(resp)) if resp else None
    return data if isinstance(data, dict) else None


def _page_dict(row, zi):
    return {"url": row["url"], "title": row["title"], "h1": row["h1"], "lang": row["lang"],
            "canonical": row["canonical"], "hreflang": json.loads(row["hreflang"] or "[]"), "zi": zi}


def stage_cannibal(ctx):
    st = ctx.store
    pages = []
    for r in st.db.execute(f"SELECT {LIGHT} FROM pages WHERE site='target' AND status=200 AND soft404=0 "
                           "ORDER BY url"):
        if r["url"] in ctx.zi:
            mr = (r["meta_robots"] or "") + (r["x_robots"] or "")
            if "noindex" in mr.lower():
                continue
            pages.append(_page_dict(r, ctx.zi[r["url"]]))
    intents = {u: v[0] for u, v in ctx.intents.items()}
    groups, pairs, excl, ex_examples, ncand = cannibal.find_groups(
        pages, ctx.Z, ctx.sigs, intents, ctx.graphinfo, ctx.anchors, ctx.cfg.seed, ctx.dup_pairs)
    ctx.groups, ctx.pairs = groups, pairs
    ctx.results["cannibal"] = {"pages": len(pages), "ann_candidates": ncand, "verified_pairs": len(pairs),
                               "groups": len(groups), "excluded_healthy": excl,
                               "excluded_examples": {k: list(v) for k, v in sorted(ex_examples.items())}}
    ctx.log(f"cannibalization: {len(groups)} groups from {ncand} ANN candidates; excluded {excl}")


def stage_gaps(ctx):
    rows = []
    for u, i in ctx.zi.items():
        p = ctx.page(u)
        rows.append((i, p["site"], u, p["word_count"] or 0))
    rows.sort(key=lambda r: r[0])
    pr = {u: v["pagerank"] for u, v in ctx.graphinfo.items()}
    gl, summary = gapmod.find_gaps(ctx.Z, rows, ctx.space, ctx.cfg.seed, pr)
    ctx.gaps = gl
    ctx.results["gaps"] = summary | {"gaps": len(gl)}


def choose_priority(ctx, n=10):
    """Default priority pages when none are supplied: commercial/transactional target pages
    with substantial content and below-median internal PageRank."""
    cands = []
    prs = [v["pagerank"] for u, v in ctx.graphinfo.items() if (ctx.page(u) or {"site": ""})["site"] == "target"]
    med = float(np.median(prs)) if prs else 0
    for u, (it, m, _) in sorted(ctx.intents.items()):
        p = ctx.page(u)
        if p["site"] != "target" or u not in ctx.graphinfo:
            continue
        if it in ("commercial", "transactional") and (p["word_count"] or 0) >= 150 and ctx.graphinfo[u]["pagerank"] <= med:
            cands.append(((p["word_count"] or 0), u))
    if len(cands) < n:
        for u, (it, m, _) in sorted(ctx.intents.items()):
            p = ctx.page(u)
            if p["site"] == "target" and u in ctx.graphinfo and it == "informational" and (p["word_count"] or 0) >= 300 \
                    and ctx.graphinfo[u]["pagerank"] <= med:
                cands.append(((p["word_count"] or 0) / 2, u))
    cands.sort(key=lambda x: (-x[0], x[1]))
    return [u for _, u in cands[:n]]


def stage_links(ctx):
    cfg = ctx.cfg
    g = ctx.graphs["target"]
    if g.n < 3:
        ctx.plan = {"links": [], "note": "graph too small"}
        return
    pri = [normalize_input(u) for u in cfg.priority_urls] if cfg.priority_urls else choose_priority(ctx)
    pri = [g.resolve.get(u, u) for u in pri]
    pri = [u for u in dict.fromkeys(pri) if u in g.idx]
    if not pri:
        ctx.plan = {"links": [], "note": "no valid priority URLs"}
        return
    pri_idx = np.array([g.idx[u] for u in pri])
    space = ctx.space
    eligible = []
    for u in g.nodes:
        p = ctx.page(u)
        mr = ((p["meta_robots"] or "") + (p["x_robots"] or "")).lower()
        if u in ctx.zi and not p["soft404"] and "noindex" not in mr and "nofollow" not in mr:
            eligible.append(u)
    E = np.array([ctx.zi[u] for u in eligible])
    cands = []
    for t in pri:
        if t not in ctx.zi:
            continue
        tz = ctx.Z[ctx.zi[t]]
        sims = ctx.Z[E] @ tz
        order = np.lexsort((np.arange(len(E)), -sims))[:linkopt.SOURCES_PER_TARGET]
        tp = ctx.page(t)
        terms = space.top_terms(ctx.zi[t], 8) if space.X is not None else []
        phrases = linkopt.target_phrases(dict(tp), terms, ctx.anchors.get(t, []))
        from .textnorm import tokens as _tk
        title_words = set(_tk(f"{tp['title'] or ''} {tp['h1'] or ''}", drop_stop=True))
        for j in order:
            if sims[j] < linkopt.REL_MIN:
                break
            s = eligible[j]
            if s == t or g.has_content_edge(g.idx[s], g.idx[t]):
                continue
            mt = ctx.page_field(s, "main_text") or ""
            ex = [r[0] for r in ctx.store.db.execute("SELECT anchor FROM links WHERE src=? AND block='content'", (s,))]
            fa = linkopt.find_anchor(mt, phrases, title_words, ex)
            if not fa:
                continue
            cands.append((g.idx[s], g.idx[t], {"source": s, "target": t, "sentence": fa[0], "anchor": fa[1],
                                               "relevance": round(float(sims[j]), 3)}))
    cands.sort(key=lambda c: (c[0], c[1]))
    base_pr = g.pagerank()
    chosen, Wn = linkopt.greedy(g.W, pri_idx, cands, cfg.k)
    after = pagerank(Wn)
    # proof: rebuild from scratch with the chosen links and recompute
    W2 = g.matrix([(c[0], c[1]) for c in chosen])
    after2 = pagerank(W2)
    links = []
    for s, t, meta, gain in chosen:
        links.append(meta | {"predicted_gain_x1000": round(gain * 1000, 6)})
    per_url = [{"url": u, "pagerank_x1000_before": round(float(base_pr[g.idx[u]]) * 1000, 4),
                "pagerank_x1000_after_predicted": round(float(after[g.idx[u]]) * 1000, 4),
                "pagerank_x1000_after_recomputed": round(float(after2[g.idx[u]]) * 1000, 4)} for u in pri]
    ctx.plan = {"priority_urls": pri, "k": cfg.k, "max_per_source": linkopt.MAX_PER_SOURCE,
                "candidates": len(cands), "links": links,
                "total_before_x1000": round(float(base_pr[pri_idx].sum()) * 1000, 4),
                "total_after_predicted_x1000": round(float(after[pri_idx].sum()) * 1000, 4),
                "total_after_recomputed_x1000": round(float(after2[pri_idx].sum()) * 1000, 4),
                "per_url": per_url,
                "greedy_vs_optimal_small_graphs": _compare_cached(ctx)}
    ctx.log(f"link plan: {len(links)} links from {len(cands)} candidates; priority PR x1000 "
            f"{ctx.plan['total_before_x1000']} -> {ctx.plan['total_after_recomputed_x1000']}")


def _compare_cached(ctx):
    key = f"compare_small:{ctx.cfg.seed}"
    v = ctx.store.get_meta(key)
    if v is None:
        v = {k: x for k, x in linkopt.compare_small(ctx.cfg.seed, trials=10).items() if k != "details"}
        ctx.store.set_meta(key, v)
    return v


def normalize_input(u):
    from .urlnorm import normalize
    return normalize(u if "://" in u else "https://" + u)[0]
