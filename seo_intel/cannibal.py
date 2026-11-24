"""Cannibalization groups (sub-quadratic) with healthy-pair exclusion and a severity score.

Candidates: inverted-file ANN over the topic vectors (k-means cells, multi-probe), plus MinHash
duplicate pairs from Part B; cosine >= SIM_MIN inside cells. Work ~O(n^1.5), never all pairs.
(Random-hyperplane LSH 12x12 bits was tried first: ~41% recall at cosine 0.75 - DECISIONS.md.)

Healthy pairs excluded automatically:
  hreflang   - either page lists the other as an hreflang alternate, or different lang
  pagination - same URL template where only a page number differs
  sub-page   - product/page + its own FAQ/reviews/specs/Q&A (child path with such a slug)
  hierarchy  - parent category + child (one URL path is an ancestor of the other)
  canonical  - one already canonicalises or redirects to the other (handled elsewhere)

Severity (0-100), only signals available without Search Console:
  35 * topic_sim      cosine of topic vectors, rescaled from [SIM_MIN,1] to [0,1]
  20 * intent_match   1 if same intent else 0
  15 * content_overlap MinHash Jaccard of main content
  10 * title_overlap  Jaccard of title+H1 keywords
  10 * anchor_conflict share of incoming anchor phrases used for both pages
   5 * link_split     1 - |in_a - in_b|/(in_a + in_b): internal links split evenly
   5 * shallow        1 if both pages at click depth <= 3
"""
from __future__ import annotations

import re
from collections import defaultdict
from urllib.parse import urlsplit

import numpy as np

from .textnorm import tokens
from .urlnorm import template

NPROBE = 3
BLOCK = 512
TOP_NEIGHBOURS = 12  # kNN graph: memory <= n*12 pairs even when thousands of pages look alike
SIM_MIN = 0.62
GROUP_MIN_SEVERITY = 45
KEEP_MIN_SEVERITY = 40  # pairs below this are never grouped (45) and dominate memory at 50k pages
SUBPAGE = re.compile(r"^(reviews?|faqs?|questions?|q-?and-?a|qa|specs?|specifications?|ratings?|"
                     r"comments?|photos?|gallery|video|amp|print)$", re.I)
PAGENUM = re.compile(r"([?&](p|pg|page|paged|start|offset)=\d+)|(/page/\d+/?$)|(/p\d+/?$)", re.I)


def ivf_candidates(Z, idxs, seed):
    """Inverted-file ANN: k-means into ~4*sqrt(n) cells, each page assigned to its NPROBE nearest
    cells, exact cosine only inside cells (matrix blocks), keeping each page's TOP_NEIGHBOURS.
    Cost ~ n * NPROBE * cell_size, i.e. O(n^1.5), never all pairs.
    Returns {(zi_a, zi_b): cosine} with cosine >= SIM_MIN."""
    from sklearn.cluster import MiniBatchKMeans
    idxs = np.asarray(idxs)
    n = len(idxs)
    out = {}
    if n < 2:
        return out
    sub = Z[idxs]
    k = int(max(1, min(n // 8, round(4 * n ** 0.5))))
    if k <= 1:
        cells = [np.arange(n)]
    else:
        km = MiniBatchKMeans(n_clusters=k, random_state=seed, n_init=1, batch_size=4096, max_iter=100)
        km.fit(sub)
        C = km.cluster_centers_ / np.maximum(np.linalg.norm(km.cluster_centers_, axis=1, keepdims=True), 1e-12)
        near = np.argsort(-(sub @ C.T), axis=1, kind="stable")[:, :min(NPROBE, k)]
        cells = [np.where((near == c).any(axis=1))[0] for c in range(k)]
    for cell in cells:
        if len(cell) < 2:
            continue
        for s0 in range(0, len(cell), BLOCK):
            blk = cell[s0:s0 + BLOCK]
            S = sub[blk] @ sub[cell].T
            for r in range(len(blk)):
                S[r, s0 + r] = -1.0  # self
            m = min(TOP_NEIGHBOURS, S.shape[1] - 1)
            if m <= 0:
                continue
            top = np.argpartition(-S, m - 1, axis=1)[:, :m]
            for i in range(len(blk)):
                for j in top[i]:
                    v = S[i, j]
                    if v >= SIM_MIN:
                        a, b = int(idxs[blk[i]]), int(idxs[cell[j]])
                        key = (a, b) if a < b else (b, a)
                        out[key] = float(v)
    return out


def _path(u):
    return urlsplit(u).path.rstrip("/") or "/"


def healthy_reason(a, b, pa, pb):
    """pa/pb: page dicts. Returns reason string if the pair is legitimately similar."""
    hl_a = {h for _, h in pa["hreflang"]}
    hl_b = {h for _, h in pb["hreflang"]}
    if b in hl_a or a in hl_b:
        return "hreflang alternates"
    if pa["lang"] and pb["lang"] and pa["lang"][:2] != pb["lang"][:2]:
        return "different languages"
    if (pa["canonical"] and pa["canonical"] == b) or (pb["canonical"] and pb["canonical"] == a):
        return "already canonicalised"
    if PAGENUM.search(a) or PAGENUM.search(b):
        if PAGENUM.sub("", a).rstrip("/?&") == PAGENUM.sub("", b).rstrip("/?&") or template(a) == template(b) and \
                re.sub(r"\d+", "", a) == re.sub(r"\d+", "", b):
            return "paginated series"
    xa, xb = _path(a), _path(b)
    for parent, child in ((xa, xb), (xb, xa)):
        if child.startswith(parent + "/") or (parent == "/" and child != "/"):
            rest = child[len(parent):].strip("/")
            if "/" not in rest and SUBPAGE.match(rest.split(".")[0]):
                return "page + its own FAQ/reviews sub-page"
            if parent != "/":
                return "parent category + child"
    sa, sb = urlsplit(a), urlsplit(b)
    if sa.path == sb.path and sa.query != sb.query:
        qa = dict(x.split("=", 1) for x in sa.query.split("&") if "=" in x)
        qb = dict(x.split("=", 1) for x in sb.query.split("&") if "=" in x)
        diff = {k for k in set(qa) | set(qb) if qa.get(k) != qb.get(k)}
        if diff and all(k.lower() in ("tab", "view", "section", "sort", "order") for k in diff):
            return "same page, different view parameter"
    return None


def kw(p):
    return set(tokens(f"{p['title'] or ''} {p['h1'] or ''}", drop_stop=True))


def jac(a, b):
    return len(a & b) / len(a | b) if (a or b) else 0.0


def find_groups(pages, Z, minsigs, intents, graphinfo, anchors_by_url, seed, dup_pairs=()):
    """pages: list of page dicts for ONE site (index aligned with Z rows given in p['zi'])."""
    by_url = {p["url"]: p for p in pages}
    idxs = [p["zi"] for p in pages]
    zi2url = {p["zi"]: p["url"] for p in pages}
    cand = ivf_candidates(Z, idxs, seed)
    for a, b, _ in dup_pairs:
        if a in by_url and b in by_url:
            ia, ib = by_url[a]["zi"], by_url[b]["zi"]
            cand.setdefault((min(ia, ib), max(ia, ib)), float(Z[ia] @ Z[ib]))
    pairs, excluded = [], defaultdict(int)
    excluded_examples = {}
    for ia, ib in sorted(cand):
        sim = cand[(ia, ib)]
        a, b = sorted((zi2url[ia], zi2url[ib]))
        pa, pb = by_url[a], by_url[b]
        ov = 0.0
        if a in minsigs and b in minsigs:
            ov = float(np.mean(minsigs[a] == minsigs[b]))
        if sim < SIM_MIN and ov < 0.5:
            continue
        hr = healthy_reason(a, b, pa, pb)
        if hr:
            excluded[hr] += 1
            excluded_examples.setdefault(hr, (a, b))
            continue
        f = pair_features(a, b, pa, pb, sim, ov, graphinfo, anchors_by_url, intents)
        sev = severity(f)
        if sev >= KEEP_MIN_SEVERITY:
            pairs.append({"url_a": a, "url_b": b, "severity": sev, **f})
    groups = _group(pairs, graphinfo)
    return groups, pairs, dict(excluded), excluded_examples, len(cand)


def pair_features(a, b, pa, pb, sim, ov, graphinfo, anchors_by_url, intents):
    ga, gb = graphinfo.get(a, {}), graphinfo.get(b, {})
    ina, inb = ga.get("content_inlinks", 0), gb.get("content_inlinks", 0)
    anc_a = {x.lower().strip() for x in anchors_by_url.get(a, []) if len(x.strip()) > 2}
    anc_b = {x.lower().strip() for x in anchors_by_url.get(b, []) if len(x.strip()) > 2}
    return {
        "topic_sim": round(sim, 4),
        "intent_match": 1 if intents.get(a) == intents.get(b) else 0,
        "content_overlap": round(ov, 4),
        "title_overlap": round(jac(kw(pa), kw(pb)), 4),
        "anchor_conflict": round(jac(anc_a, anc_b), 4),
        "link_split": round(1 - abs(ina - inb) / (ina + inb), 4) if (ina + inb) else 0.0,
        "shallow": 1 if 0 <= ga.get("click_depth", 99) <= 3 and 0 <= gb.get("click_depth", 99) <= 3 else 0,
    }


def severity(f):
    ts = max(0.0, (f["topic_sim"] - SIM_MIN) / (1 - SIM_MIN))
    s = (35 * ts + 20 * f["intent_match"] + 15 * f["content_overlap"] + 10 * f["title_overlap"]
         + 10 * f["anchor_conflict"] + 5 * f["link_split"] + 5 * f["shallow"])
    return round(min(100.0, s), 2)


def _group(pairs, graphinfo):
    strong = [p for p in pairs if p["severity"] >= GROUP_MIN_SEVERITY and
              (p["intent_match"] or (p["topic_sim"] >= 0.85 and p["title_overlap"] >= 0.4))]
    parent = {}
    size = defaultdict(lambda: 1)

    def find(x):
        while parent.setdefault(x, x) != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for p in sorted(strong, key=lambda p: (-p["severity"], p["url_a"], p["url_b"])):
        ra, rb = find(p["url_a"]), find(p["url_b"])
        if ra != rb and size[ra] + size[rb] <= 8:
            lo, hi = min(ra, rb), max(ra, rb)
            parent[hi] = lo
            size[lo] += size[hi]
    members = defaultdict(set)
    gpairs = defaultdict(list)
    for p in strong:
        r = find(p["url_a"])
        if r == find(p["url_b"]):
            members[r].update((p["url_a"], p["url_b"]))
            gpairs[r].append(p)
    groups = []
    for root, urls in members.items():
        urls = sorted(urls)
        gp = gpairs[root]
        primary = max(urls, key=lambda u: (graphinfo.get(u, {}).get("pagerank", 0),
                                           graphinfo.get(u, {}).get("content_inlinks", 0), -len(u), u))
        groups.append({"urls": urls, "primary": primary,
                       "severity": round(max(p["severity"] for p in gp), 2),
                       "mean_severity": round(float(np.mean([p["severity"] for p in gp])), 2),
                       "pairs": sorted(gp, key=lambda p: (-p["severity"], p["url_a"], p["url_b"]))})
    groups.sort(key=lambda g: (-g["severity"], -len(g["urls"]), g["urls"][0]))
    for i, g in enumerate(groups):
        g["id"] = f"G{i + 1}"
    return groups
