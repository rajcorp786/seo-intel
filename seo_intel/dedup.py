"""Near-duplicate detection with MinHash + banded LSH (no all-pairs comparison).

Shingles: 4-token windows of phonetic-normalised tokens (3-token for short docs),
hashed with crc32 (deterministic, unlike Python's hash()). 128 permutations,
42 bands x 3 rows => candidate probability ~94% at Jaccard 0.4, ~99.6% at 0.5.
Candidates are verified with the signature-estimated Jaccard; buckets larger than
BUCKET_CAP (mass-duplicate templates) are linked as a chain so cost stays linear.
"""
from __future__ import annotations

import zlib

import numpy as np

from .textnorm import shingle_tokens

NPERM = 128
BANDS = 42
ROWS = 3  # 42 bands x 3 rows (126 of 128 perms): P(candidate) 0.94 @ J=0.4, 0.996 @ J=0.5
MAX_SHINGLES = 3000
BUCKET_CAP = 60
OVERLAP_T = 0.35   # 'highly overlapping'
NEAR_DUP_T = 0.70  # 'near-duplicate'


class MinHasher:
    def __init__(self, seed=42):
        # multiply-shift hashing on 64-bit words (wrap-around arithmetic): h(x) = ((a*x + b) mod 2^64) >> 32
        # with random odd a. (An earlier (a*x+b) mod p with 31-bit a was almost linear in x and broke
        # the min-wise property: estimates were far from true Jaccard - see DECISIONS.md.)
        rng = np.random.RandomState(seed)
        hi = rng.randint(0, 2 ** 32, size=(2, NPERM), dtype=np.uint64)
        lo = rng.randint(0, 2 ** 32, size=(2, NPERM), dtype=np.uint64)
        self.a = (hi[0] << np.uint64(32)) | lo[0] | np.uint64(1)
        self.b = (hi[1] << np.uint64(32)) | lo[1]

    def shingles(self, text):
        toks = shingle_tokens(text)
        k = 4 if len(toks) >= 40 else 3
        if len(toks) < k:
            return set([" ".join(toks)]) if toks else set()
        return {" ".join(toks[i:i + k]) for i in range(len(toks) - k + 1)}

    def signature(self, text):
        sh = self.shingles(text)
        if not sh:
            return None
        hv = np.fromiter((_h64(s) for s in sorted(sh)[:MAX_SHINGLES] if s), dtype=np.uint64)
        if hv.size == 0:
            return None
        with np.errstate(over="ignore"):
            m = (np.outer(hv, self.a) + self.b) >> np.uint64(32)
        return m.min(axis=0).astype(np.uint64)


def _h64(s):
    b = s.encode("utf-8")
    return (zlib.crc32(b) << 32) | zlib.adler32(b)


def est_jaccard(s1, s2):
    return float(np.mean(s1 == s2))


def lsh_pairs(sigs: dict, threshold=0.5):
    """sigs: {id: signature}. Returns sorted [(id_a, id_b, est_jaccard)] with est >= threshold."""
    ids = sorted(sigs)
    cand = set()
    for b in range(BANDS):
        buckets = {}
        for i in ids:
            key = sigs[i][b * ROWS:(b + 1) * ROWS].tobytes()
            buckets.setdefault(key, []).append(i)
        for members in buckets.values():
            if len(members) < 2:
                continue
            if len(members) > BUCKET_CAP:
                for x, y in zip(members, members[1:]):
                    cand.add((x, y))
                continue
            for x in range(len(members)):
                for y in range(x + 1, len(members)):
                    cand.add((members[x], members[y]))
    out = []
    for a, b in sorted(cand):
        j = est_jaccard(sigs[a], sigs[b])
        if j >= threshold:
            out.append((a, b, round(j, 4)))
    return out, len(cand)


def plant_duplicates(texts: list[tuple[str, str]], n=200, seed=42):
    """Create n near-duplicate variants (2-10% token edits, sentence swap). Returns [(src_id, new_id, text)]."""
    rng = np.random.RandomState(seed)
    pool = [(u, t) for u, t in texts if len(t.split()) >= 60]
    out = []
    if not pool:
        return out
    for k in range(n):
        u, t = pool[rng.randint(len(pool))]
        words = t.split()
        rate = 0.02 + 0.08 * rng.rand()
        nw = []
        for w in words:
            r = rng.rand()
            if r < rate / 3:
                continue
            if r < 2 * rate / 3:
                nw.append(words[rng.randint(len(words))])
                continue
            nw.append(w)
            if r > 1 - rate / 3:
                nw.append(words[rng.randint(len(words))])
        paras = " ".join(nw).split(". ")
        if len(paras) > 3 and rng.rand() < 0.5:
            i = rng.randint(len(paras) - 1)
            paras[i], paras[i + 1] = paras[i + 1], paras[i]
        out.append((u, f"planted://{k}", ". ".join(paras)))
    return out
