"""Internal-link optimiser: choose K new links maximising total PageRank of priority pages.

Constraints: <= MAX_PER_SOURCE new links per source page, source must be relevant to the
target (topic cosine >= REL_MIN), indexable, not soft-404, not already linking in-content,
and must contain a sentence with a natural anchor (>= 2 words shared with the target's
title/H1/key phrases; single strong keyword allowed as minimal edit).

Method ("estimate-then-exact lazy greedy"):
  1. h = (I - d P)^-1 1_P  ("how much rank injected at v ends up on priority pages")
  2. first-order gain of adding s->t:  d * PR(s)/(out(s)+1) * (h(t) - mean_{u in out(s)} h(u))
  3. each step: exact PageRank recompute for the top-M estimated candidates, take the best,
     update the graph, recompute h. Repeat K times.
Quality check vs a better method: on small graphs we brute-force the optimal K-subset and
report greedy/optimal (see compare_small()).
"""
from __future__ import annotations

import itertools
import re

import numpy as np
from scipy import sparse

from .graph import DAMPING, pagerank
from .textnorm import DEV, STOP, sentences

REL_MIN = 0.30
MAX_PER_SOURCE = 3
TOP_M = 12
SOURCES_PER_TARGET = 250
TOKEN_I = re.compile(rf"[{DEV}]+|[A-Za-z0-9]+(?:['’][A-Za-z]+)?")
TITLE_SPLIT = re.compile(r"\s+[|\-–—:·•]\s+")


def priority_value(W, pri_idx):
    return float(pagerank(W)[pri_idx].sum())


def h_vector(W, pri_idx, d=DAMPING, iters=200):
    n = W.shape[0]
    out = np.asarray(W.sum(axis=1)).ravel()
    inv = np.zeros(n)
    inv[out > 0] = 1.0 / out[out > 0]
    P = sparse.diags(inv) @ W
    e = np.zeros(n)
    e[pri_idx] = 1.0
    h = e.copy()
    for _ in range(iters):
        hn = e + d * (P @ h)
        hn[out == 0] = e[out == 0] + d * h.mean()
        if np.abs(hn - h).max() < 1e-12:
            h = hn
            break
        h = hn
    return h, out, P


def estimated_gains(W, pr, pri_idx, cands):
    h, out, P = h_vector(W, pri_idx)
    hbar = np.where(out > 0, P @ h, h.mean())
    s = np.array([c[0] for c in cands])
    t = np.array([c[1] for c in cands])
    return DAMPING * pr[s] / (out[s] + 1) * (h[t] - hbar[s])


def add_edge(W, s, t):
    return (W + sparse.csr_matrix(([1.0], ([s], [t])), shape=W.shape)).tocsr()


def greedy(W, pri_idx, cands, K, max_per_source=MAX_PER_SOURCE, top_m=TOP_M, exact_all=False):
    """cands: list of (s, t, meta). Returns chosen list of (s,t,meta, gain)."""
    chosen, per_src, used = [], {}, set()
    cur = W.copy().tocsr()
    base = priority_value(cur, pri_idx)
    for _ in range(K):
        avail = [c for c in cands if (c[0], c[1]) not in used and per_src.get(c[0], 0) < max_per_source
                 and cur[c[0], c[1]] == 0]
        if not avail:
            break
        pr = pagerank(cur)
        est = estimated_gains(cur, pr, pri_idx, avail)
        order = np.lexsort((np.arange(len(avail)), -est))
        pool = order if exact_all else order[:top_m]
        best, best_val = None, base
        for j in pool:
            s, t, _ = avail[j]
            v = priority_value(add_edge(cur, s, t), pri_idx)
            if v > best_val + 1e-15:
                best, best_val = avail[j], v
        if best is None:
            break
        cur = add_edge(cur, best[0], best[1])
        used.add((best[0], best[1]))
        per_src[best[0]] = per_src.get(best[0], 0) + 1
        chosen.append((best[0], best[1], best[2], best_val - base))
        base = best_val
    return chosen, cur


def brute_force(W, pri_idx, cands, K, max_per_source=MAX_PER_SOURCE):
    base = priority_value(W, pri_idx)
    best, best_set = base, ()
    for combo in itertools.combinations(range(len(cands)), K):
        srcs = [cands[i][0] for i in combo]
        if any(srcs.count(x) > max_per_source for x in set(srcs)):
            continue
        M = W
        for i in combo:
            M = add_edge(M, cands[i][0], cands[i][1])
        v = priority_value(M, pri_idx)
        if v > best:
            best, best_set = v, combo
    return best, best_set, base


def compare_small(seed=42, trials=20, n=30, n_cands=14, K=4):
    """Greedy (estimate+exact top-M) vs exhaustive optimum on random small graphs."""
    rng = np.random.RandomState(seed)
    res = []
    for t in range(trials):
        dens = 0.08 + 0.1 * rng.rand()
        A = (rng.rand(n, n) < dens).astype(float)
        np.fill_diagonal(A, 0)
        W = sparse.csr_matrix(A)
        pri = sorted(rng.choice(n, size=3, replace=False).tolist())
        pairs = [(i, j) for i in range(n) for j in pri if i != j and A[i, j] == 0]
        sel = rng.choice(len(pairs), size=min(n_cands, len(pairs)), replace=False)
        cands = [(pairs[k][0], pairs[k][1], None) for k in sorted(sel)]
        opt, _, base = brute_force(W, np.array(pri), cands, K)
        ch, Wg = greedy(W, np.array(pri), cands, K, top_m=TOP_M)
        g = priority_value(Wg, np.array(pri))
        ch2, Wn = greedy(W, np.array(pri), cands, K, top_m=1)
        naive = priority_value(Wn, np.array(pri))
        res.append({"trial": t, "base": base, "optimal": opt, "greedy": g, "estimate_only_greedy": naive,
                    "greedy_share_of_optimal_gain": (g - base) / (opt - base) if opt > base else 1.0,
                    "estimate_share_of_optimal_gain": (naive - base) / (opt - base) if opt > base else 1.0})
    shares = [r["greedy_share_of_optimal_gain"] for r in res]
    ns = [r["estimate_share_of_optimal_gain"] for r in res]
    return {"trials": trials, "nodes": n, "candidates": n_cands, "K": K,
            "greedy_mean_share_of_optimal_gain": round(float(np.mean(shares)), 6),
            "greedy_min_share_of_optimal_gain": round(float(np.min(shares)), 6),
            "estimate_only_mean_share": round(float(np.mean(ns)), 6),
            "estimate_only_min_share": round(float(np.min(ns)), 6),
            "details": [{k: (round(v, 8) if isinstance(v, float) else v) for k, v in r.items()} for r in res]}


# ------------------------------------------------------------- anchors
def target_phrases(page, top_terms, anchors):
    """Phrases a natural anchor may come from: title/H1 segments and existing descriptive anchors.
    Top TF-IDF terms are used only when every word is also in the title/H1."""
    out = []
    for field in (page.get("h1"), page.get("title")):
        if field:
            segs = TITLE_SPLIT.split(field)
            out.extend(segs[:2])
    out.extend(a for a in anchors[:20] if 2 <= len(a.split()) <= 6)
    tw = {m.group(0).lower() for f in (page.get("h1") or "", page.get("title") or "") for m in TOKEN_I.finditer(f)}
    out.extend(t for t in top_terms if all(w in tw or w in STOP for w in t.split()))
    seqs = []
    for p in out:
        toks = [m.group(0).lower() for m in TOKEN_I.finditer(p)]
        if toks:
            seqs.append(toks)
    return seqs


def find_anchor(main_text, phrases, title_words, existing_anchors=()):
    """Return (sentence, anchor, score) or None. Anchor is an exact substring of the sentence,
    contains at least one non-stop word of the target's title/H1, and the sentence holds no
    existing link of the source page."""
    best = None
    existing = [a.strip().lower() for a in existing_anchors if a and len(a.strip()) >= 3]
    grams = set()
    for seq in phrases:
        for L in range(2, 7):
            for i in range(len(seq) - L + 1):
                g = tuple(seq[i:i + L])
                if g[0] in STOP or g[-1] in STOP:
                    continue
                grams.add(g)
    for si, sent in enumerate(sentences(main_text)):
        if len(sent) > 400 or len(sent.split()) < 6:
            continue
        low = sent.lower()
        if any(a in low for a in existing):
            continue
        ms = list(TOKEN_I.finditer(sent))
        toks = [m.group(0).lower() for m in ms]
        cand = None
        for L in range(min(6, len(toks)), 1, -1):
            for i in range(len(toks) - L + 1):
                g = toks[i:i + L]
                if tuple(g) in grams and any(x in title_words and x not in STOP for x in g):
                    cand = (ms[i].start(), ms[i + L - 1].end(), sum(1 for x in g if x not in STOP))
                    break
            if cand:
                break
        if not cand:
            for i, tk in enumerate(toks):
                if tk in title_words and tk not in STOP and len(tk) >= 5:
                    cand = (ms[i].start(), ms[i].end(), 0.5)
                    break
        if cand and (best is None or cand[2] > best[2]):
            best = (sent, sent[cand[0]:cand[1]], cand[2])
            if cand[2] >= 3:
                break
    return best
