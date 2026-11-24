"""Internal link graph, PageRank and click depth (numpy arrays: ~16 bytes per edge).

Nodes  : HTML 200 pages of the site, minus pages inside detected spider traps (links to
         redirecting URLs are resolved to the target).
Edges  : de-duplicated src->dst pairs. In-content links weight 1.0; links in template blocks
         (nav/header/footer/sidebars found in Part B) are ignored for PageRank
         (NAV_WEIGHT = 0) but still count for click depth (users and bots do click them).
         nofollow links are ignored for both.
PageRank: damping 0.85, power iteration to L1 < 1e-12, dangling mass spread uniformly.
"""
from __future__ import annotations

from array import array
from collections import deque

import numpy as np
from scipy import sparse

DAMPING = 0.85
NAV_WEIGHT = 0.0
MAX_ANCHORS = 20


class LinkGraph:
    def __init__(self, store, site):
        self.site = site
        rows = store.db.execute(
            "SELECT url, status, final_url FROM pages WHERE site=? ORDER BY url", (site,)).fetchall()
        # pages inside detected spider traps are not real pages: keep them out of the graph
        from .urlnorm import template
        trap_t = {template(e) for k, e in store.db.execute("SELECT kind, example FROM traps WHERE site=?", (site,))
                  if k in ("calendar", "template", "path")}
        self.nodes = [u for u, st, _ in rows if st == 200 and template(u) not in trap_t]
        self.idx = {u: i for i, u in enumerate(self.nodes)}
        self.resolve = {u: fu for u, st, fu in rows if st in (301, 302, 303, 307, 308) and fu in self.idx}
        self.n = n = len(self.nodes)
        src, dst, cont = array("q"), array("q"), array("b")
        self.incoming_anchors = {}
        for s_url, d_url, anchor, block in store.db.execute(
                "SELECT l.src, l.dst, l.anchor, l.block FROM links l JOIN pages p ON p.url=l.src "
                "WHERE p.site=? AND p.status=200 ORDER BY l.src, l.ord", (site,)):
            d = self.resolve.get(d_url, d_url)
            t = self.idx.get(d)
            s = self.idx.get(s_url)
            if t is None or s is None or t == s:
                continue
            if block and block.startswith("nofollow"):
                continue
            is_c = block != "nav"
            src.append(s)
            dst.append(t)
            cont.append(1 if is_c else 0)
            if is_c and anchor:
                lst = self.incoming_anchors.setdefault(d, [])
                if len(lst) < MAX_ANCHORS and anchor not in lst:
                    lst.append(anchor)
        s = np.frombuffer(src, dtype=np.int64) if len(src) else np.zeros(0, np.int64)
        t = np.frombuffer(dst, dtype=np.int64) if len(dst) else np.zeros(0, np.int64)
        c = np.frombuffer(cont, dtype=np.int8) if len(cont) else np.zeros(0, np.int8)
        key = s * max(n, 1) + t
        # all edges (dedup) and content edges (dedup)
        self.all_keys = np.unique(key)
        self.content_keys = np.unique(key[c == 1])
        self.W = self.matrix()

    def matrix(self, extra=()):
        n = max(self.n, 1)
        ck = self.content_keys
        if extra:
            ck = np.union1d(ck, np.array([s * n + t for s, t in extra], dtype=np.int64))
        rows, cols = ck // n, ck % n
        w = np.ones(len(ck))
        if NAV_WEIGHT > 0:
            nav = np.setdiff1d(self.all_keys, ck)
            rows = np.concatenate([rows, nav // n])
            cols = np.concatenate([cols, nav % n])
            w = np.concatenate([w, np.full(len(nav), NAV_WEIGHT)])
        return sparse.csr_matrix((w, (rows, cols)), shape=(self.n, self.n), dtype=np.float64)

    def has_content_edge(self, s, t):
        k = s * max(self.n, 1) + t
        i = np.searchsorted(self.content_keys, k)
        return i < len(self.content_keys) and self.content_keys[i] == k

    def pagerank(self, W=None):
        return pagerank(self.W if W is None else W)

    def click_depth(self, home):
        h = self.resolve.get(home, home)
        depth = np.full(self.n, -1, dtype=np.int64)
        if h not in self.idx:
            return depth
        n = max(self.n, 1)
        A = sparse.csr_matrix((np.ones(len(self.all_keys)), (self.all_keys // n, self.all_keys % n)),
                              shape=(self.n, self.n))
        q = deque([self.idx[h]])
        depth[self.idx[h]] = 0
        while q:
            v = q.popleft()
            for t in A.indices[A.indptr[v]:A.indptr[v + 1]]:
                if depth[t] < 0:
                    depth[t] = depth[v] + 1
                    q.append(t)
        return depth

    def content_inlinks(self):
        n = max(self.n, 1)
        return np.bincount(self.content_keys % n, minlength=self.n)

    def content_outlinks(self):
        n = max(self.n, 1)
        return np.bincount(self.content_keys // n, minlength=self.n)


def pagerank(W, d=DAMPING, tol=1e-12, max_iter=500):
    n = W.shape[0]
    if n == 0:
        return np.zeros(0)
    out = np.asarray(W.sum(axis=1)).ravel()
    dangling = out == 0
    inv = np.zeros(n)
    inv[~dangling] = 1.0 / out[~dangling]
    PT = (sparse.diags(inv) @ W).T.tocsr()
    r = np.full(n, 1.0 / n)
    for _ in range(max_iter):
        r_new = d * (PT @ r) + (d * r[dangling].sum() + (1 - d)) / n
        if np.abs(r_new - r).sum() < tol:
            r = r_new
            break
        r = r_new
    return r / r.sum()


def save_graph_stats(store, site, g: LinkGraph, home):
    pr = g.pagerank()
    depth = g.click_depth(home)
    inl = g.content_inlinks()
    outc = g.content_outlinks()
    store.db.execute("CREATE TABLE IF NOT EXISTS graph(url TEXT PRIMARY KEY, site TEXT, pagerank REAL, "
                     "click_depth INT, content_inlinks INT, content_outlinks INT)")
    store.db.execute("BEGIN")
    store.db.execute("DELETE FROM graph WHERE site=?", (site,))
    store.db.executemany("INSERT INTO graph VALUES(?,?,?,?,?,?)",
                         [(u, site, float(pr[i]), int(depth[i]), int(inl[i]), int(outc[i]))
                          for i, u in enumerate(g.nodes)])
    store.db.execute("COMMIT")
    return pr, depth
