"""Competitor topic gaps in a shared topic space.

Competitor pages are clustered in the SHARED topic space (MiniBatchKMeans on the joint LSA
vectors, k = clamp(1.5*sqrt(N_competitor), 4, 300), fixed seed). Clustering only the competitor
keeps target pages from diluting competitor topics (an earlier joint clustering merged a
competitor topic with unrelated target pages - DECISIONS.md). A cluster is a GAP when

  competitor covers it IN DEPTH:
      >= DEPTH_PAGES competitor pages, median main-content words >= DEPTH_MEDIAN_WORDS,
      total competitor words >= DEPTH_TOTAL_WORDS, cluster cohesion >= MIN_COHESION
  and the target MISSES it:
      target pages with cosine >= MISS_MAX_SIM to the competitor centroid <= MISS_MAX_PAGES, their
      words < MISS_WORD_SHARE x competitor words, and the best target cosine < MISS_MAX_SIM.
Gap score = competitor_words * (1 - best_target_sim) * (1 + competitor_pagerank_share * 10).
"""
from __future__ import annotations

import numpy as np
from sklearn.cluster import MiniBatchKMeans

DEPTH_PAGES = 5
DEPTH_MEDIAN_WORDS = 400
DEPTH_TOTAL_WORDS = 3000
MIN_COHESION = 0.35
MISS_MAX_PAGES = 1
MISS_WORD_SHARE = 0.2
MISS_MAX_SIM = 0.55


def find_gaps(Z, rows, space, seed, pr_share):
    """rows: list of (zi, site, url, word_count). pr_share: url -> pagerank (within its site)."""
    comp_rows = [r for r in rows if r[1] == "competitor"]
    targ_rows = [r for r in rows if r[1] == "target"]
    nc = len(comp_rows)
    if nc < DEPTH_PAGES:
        return [], {"k": 0, "clusters_with_competitor_depth": 0}
    k = int(min(300, max(4, round(1.5 * nc ** 0.5))))
    k = min(k, nc - 1)
    km = MiniBatchKMeans(n_clusters=k, random_state=seed, n_init=3, batch_size=2048, max_iter=200)
    labels = km.fit_predict(Z[[r[0] for r in comp_rows]])
    tz = Z[[r[0] for r in targ_rows]] if targ_rows else np.zeros((0, Z.shape[1]))
    gaps = []
    summary = {"k": k, "clusters_with_competitor_depth": 0, "method": "competitor clusters in shared space"}
    for c in range(k):
        comp = [comp_rows[i] for i in range(nc) if labels[i] == c]
        if len(comp) < DEPTH_PAGES:
            continue
        cw = [r[3] or 0 for r in comp]
        comp_z = Z[[r[0] for r in comp]]
        ccent = comp_z.mean(axis=0)
        ccent /= max(np.linalg.norm(ccent), 1e-12)
        cohesion = float(np.mean(comp_z @ ccent))
        if not (np.median(cw) >= DEPTH_MEDIAN_WORDS and sum(cw) >= DEPTH_TOTAL_WORDS and cohesion >= MIN_COHESION):
            continue
        summary["clusters_with_competitor_depth"] += 1
        best_sim, best_url, covering, tw = -1.0, None, [], 0
        if len(targ_rows):
            sims = tz @ ccent
            j = int(np.argmax(sims))
            best_sim, best_url = float(sims[j]), targ_rows[j][2]
            covering = [targ_rows[i] for i in np.where(sims >= MISS_MAX_SIM)[0]]
            tw = sum(r[3] or 0 for r in covering)
        if len(covering) > MISS_MAX_PAGES or tw >= MISS_WORD_SHARE * sum(cw) or best_sim >= MISS_MAX_SIM:
            continue
        cpr = sum(pr_share.get(r[2], 0.0) for r in comp)
        score = sum(cw) * (1 - max(0.0, best_sim)) * (1 + 10 * cpr)
        comp_sorted = sorted(comp, key=lambda r: (-(pr_share.get(r[2], 0.0)), r[2]))
        gaps.append({
            "cluster": int(c), "topic_terms": space.centroid_terms([r[0] for r in comp]),
            "competitor_pages": len(comp), "competitor_words_total": int(sum(cw)),
            "competitor_words_median": int(np.median(cw)), "cohesion": round(cohesion, 4),
            "target_pages": len(covering), "target_words_total": int(tw),
            "best_target_url": best_url, "best_target_similarity": round(best_sim, 4),
            "competitor_example_urls": [r[2] for r in comp_sorted[:5]],
            "competitor_urls": sorted(r[2] for r in comp),
            "score": round(score, 2),
        })
    gaps.sort(key=lambda g: (-g["score"], g["cluster"]))
    for i, g in enumerate(gaps):
        g["id"] = f"T{i + 1}"
    return gaps, summary
