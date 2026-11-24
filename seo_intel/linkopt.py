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

REL_MIN = 0.40
ANCHOR_DF_MAX = 0.25  # a distinctive anchor word appears in the body of at most 25% of the site's pages
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


def greedy(W, pri_idx, cands, K, max_per_source=MAX_PER_SOURCE, top_m=TOP_M, exact_all=False,
           max_per_target=None):
    """cands: list of (s, t, meta). Returns chosen list of (s,t,meta, gain)."""
    chosen, per_src, per_tgt, used = [], {}, {}, set()
    cur = W.copy().tocsr()
    base = priority_value(cur, pri_idx)
    for _ in range(K):
        avail = [c for c in cands if (c[0], c[1]) not in used and per_src.get(c[0], 0) < max_per_source
                 and cur[c[0], c[1]] == 0 and (max_per_target is None or per_tgt.get(c[1], 0) < max_per_target)]
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
        per_tgt[best[1]] = per_tgt.get(best[1], 0) + 1
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


END_PUNCT = re.compile(r"[.!?।:;\"”’)]\s*$")


def gram_set(text, lo=2, hi=6):
    toks = [m.group(0).lower() for m in TOKEN_I.finditer(text or "")]
    return {tuple(toks[i:i + L]) for L in range(lo, hi + 1) for i in range(len(toks) - L + 1)}


def ambiguous_grams(needed, pages):
    """needed: {gram: target_url}. A gram is ambiguous for its target when the title/H1 of ANY other page
    of the site contains it too: such an anchor would point readers (and Google) at two pages at once,
    i.e. create the anchor-text cannibalization this tool reports. Returns the set of ambiguous grams."""
    bad = set()
    if not needed:
        return bad
    for p in pages:
        gs = gram_set(f"{p.get('title') or ''} | {p.get('h1') or ''}")
        for g in gs & needed.keys():
            if needed[g] != p["url"]:
                bad.add(g)
    return bad


CONJ = {"and", "or", "&", "but", "nor", "aur", "ya", "और", "या"}


def anchor_ok(g, whole, title_words):
    """A natural anchor names one thing: no 'and'/'or' inside unless it is a whole title/H1 segment
    ('space and installation', 'quality, parts and service' are lists, not names), and its distinctive
    word must be one the target's title/H1 actually owns (title_words already excludes words common in
    the site's titles and in its body text)."""
    t = tuple(g)
    if any(x in CONJ for x in t) and t not in whole:
        return False
    return any(x in title_words for x in t)


def find_anchor(main_text, phrases, title_words, existing_anchors=(), source_heads=(), banned=frozenset(),
                rewrite_phrases=()):
    """Return (sentence, anchor, score) or None.
    * anchor = exact substring of the sentence, >= 2 real words (no digits-only tokens), at least one
      of them a DISTINCTIVE word of the target's title/H1 (site-wide template words excluded)
    * sentence must be body text: ends like a sentence, is not the source page's own title/H1,
      and holds no existing link of the source page"""
    best = None
    existing = [a.strip().lower() for a in existing_anchors if a and len(a.strip()) >= 3]
    heads = {h.strip().lower() for h in source_heads if h}
    grams = set()
    whole = {tuple(seq) for seq in phrases[:4]}
    for seq in phrases:
        for L in range(2, 7):
            for i in range(len(seq) - L + 1):
                g = tuple(seq[i:i + L])
                if g[0] in STOP or g[-1] in STOP or g in banned:
                    continue
                grams.add(g)
    for si, sent in enumerate(sentences(main_text)):
        if len(sent) > 400 or len(sent.split()) < 6:
            continue
        low = sent.lower()
        if any(a in low for a in existing):
            continue
        if not END_PUNCT.search(sent) or low in heads or any(low and low in h for h in heads):
            continue  # headings / list labels / the page's own title are not places for a link
        ms = list(TOKEN_I.finditer(sent))
        toks = [m.group(0).lower() for m in ms]
        cand = None
        for L in range(min(6, len(toks)), 1, -1):
            for i in range(len(toks) - L + 1):
                g = toks[i:i + L]
                words = [x for x in g if x not in STOP and not x.isdigit() and len(x) >= 2]
                if tuple(g) in grams and len(words) >= 2 and anchor_ok(g, whole, title_words):
                    cand = (ms[i].start(), ms[i + L - 1].end(), len(words))
                    break
            if cand:
                break
        if cand and (best is None or cand[2] > best[2]):
            best = (sent, sent[cand[0]:cand[1]], cand[2])
            if cand[2] >= 3:
                break
    if best is not None or not rewrite_phrases:
        return best
    return _rewrite_anchor(main_text, rewrite_phrases, existing, heads)


def _rewrite_anchor(main_text, full_phrases, existing, heads):
    """No sentence holds an unambiguous anchor verbatim. Find a sentence that holds a PART of one
    (e.g. 'home lift') and propose widening that part to the full phrase that names only the target
    (e.g. 'home lift model 3'). Returns (sentence, matched_part, score, full_phrase, rewritten_sentence)."""
    best = None
    for sent in sentences(main_text):
        if len(sent) > 400 or len(sent.split()) < 6 or not END_PUNCT.search(sent):
            continue
        low = sent.lower()
        if any(a in low for a in existing) or low in heads or any(low and low in h for h in heads):
            continue
        ms = list(TOKEN_I.finditer(sent))
        toks = [m.group(0).lower() for m in ms]
        for rank, full in enumerate(full_phrases):  # phrases come best-first: the first one that fits wins
            fl = len(full)
            hit = None
            for L in range(min(fl - 1, len(toks)), 1, -1):
                for i in range(len(toks) - L + 1):
                    part = tuple(toks[i:i + L])
                    words = [x for x in part if x not in STOP and not x.isdigit()]
                    if len(words) < 2 or part[0] in STOP or part[-1] in STOP:
                        continue
                    if any(full[j:j + L] == part for j in range(fl - L + 1)):
                        hit = (i, L)
                        break
                if hit:
                    break
            if hit:
                i, L = hit
                a, b = ms[i].start(), ms[i + L - 1].end()
                phrase = " ".join(full)
                if sent[a].isupper():
                    phrase = phrase[0].upper() + phrase[1:]
                score = (-rank, L / fl)
                if best is None or score > best[2]:
                    best = (sent, sent[a:b], score, phrase, sent[:a] + phrase + sent[b:])
                break
        if best is not None and best[2] >= (0, 0.75):
            break
    if best is not None:
        best = best[:2] + (round(best[2][1], 4),) + best[3:]
    return best
