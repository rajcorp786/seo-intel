"""seo-intel-eval: measurement commands used for BENCHMARKS.md.

  seo-intel-eval extract-f1  --out DIR --gold GOLD.json     token-level F1 of main content
  seo-intel-eval render-audit --out DIR [--n 100]           render recall estimate on NON-rendered pages
  seo-intel-eval soft404     --out DIR --labels FILE.csv    precision vs hand labels (url,label 0/1)
  seo-intel-eval judge       --out DIR --labels FILE.csv    LLM judge vs your 40 labels: Cohen's kappa
  seo-intel-eval label-template --out DIR                   writes labels_template.csv to fill in
  seo-intel-eval determinism DIR_A DIR_B                     byte-compare report/ folders
  seo-intel-eval pair-template --out DIR                    pair_labels_template.csv for Part C labels
  seo-intel-eval ablation --out DIR --labels pairs.csv      Part C signal ablation (precision@20)
  seo-intel-eval label-kit RUN_DIR [RUN_DIR...]             one HTML page for all three human-label tasks
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import json
import random
import sys
from collections import Counter
from pathlib import Path

from .store import Store
from .textnorm import tokens


def f1(pred, gold):
    p, g = Counter(tokens(pred)), Counter(tokens(gold))
    tp = sum((p & g).values())
    if not p or not g:
        return 0.0, 0.0, 0.0
    prec, rec = tp / sum(p.values()), tp / sum(g.values())
    return prec, rec, (2 * prec * rec / (prec + rec) if tp else 0.0)


def cmd_extract_f1(a):
    st = Store(Path(a.out) / "crawl.db")
    gold = json.loads(Path(a.gold).read_text(encoding="utf-8"))
    rows = []
    for u, g in sorted(gold.items()):
        r = st.db.execute("SELECT main_text FROM pages WHERE url=?", (u,)).fetchone()
        if r is None or r[0] is None:
            continue
        rows.append((u,) + f1(r[0], g))
    if a.sample and len(rows) > a.sample:
        rows = random.Random(42).sample(rows, a.sample)
    mean = lambda i: sum(r[i] for r in rows) / len(rows)
    res = {"pages": len(rows), "precision": round(mean(1), 4), "recall": round(mean(2), 4), "f1": round(mean(3), 4),
           "worst": sorted([(round(r[3], 3), r[0]) for r in rows])[:5]}
    print(json.dumps(res, indent=1, ensure_ascii=False))


def cmd_render_audit(a):
    """Render a random sample of pages that were NOT rendered; count how many differ meaningfully.
    recall estimate = rendered_needed / (rendered_needed + missed_in_sample * (unrendered/sample))."""
    from . import parse as P
    from .fetcher import Fetcher, HostLimiter
    from .render import Renderer, token_gain
    st = Store(Path(a.out) / "crawl.db")
    unr = [r[0] for r in st.db.execute("SELECT url FROM pages WHERE status=200 AND html IS NOT NULL AND rendered=0 "
                                       "ORDER BY url")]
    ren = [r for r in st.db.execute("SELECT url, render_reason FROM pages WHERE rendered=1")]
    needed = sum(1 for _, rr in ren if rr and float(rr.split("gain=")[-1]) > 0.2)
    sample = random.Random(42).sample(unr, min(a.n, len(unr)))

    async def go():
        f = Fetcher(HostLimiter(6))
        r = Renderer(f, lambda u: True)
        missed = []
        for u in sample:
            html = st.html(u)
            out, _ = await r.render(u, html)
            if out:
                g = token_gain(P.visible_text(P.to_tree(html)), P.visible_text(P.to_tree(out)))
                if g > 0.2:
                    missed.append((u, round(g, 3)))
        await r.close()
        await f.close()
        return missed
    missed = asyncio.run(go())
    est_missed = len(missed) * (len(unr) / max(1, len(sample)))
    recall = needed / (needed + est_missed) if (needed + est_missed) else 1.0
    print(json.dumps({"rendered": len(ren), "rendered_meaningful": needed, "unrendered": len(unr),
                      "sample": len(sample), "missed_in_sample": missed, "estimated_recall": round(recall, 4),
                      "render_share": round(len(ren) / max(1, len(ren) + len(unr)), 4)}, indent=1))


def cmd_soft404(a):
    st = Store(Path(a.out) / "crawl.db")
    lab = {r["url"]: int(r["label"]) for r in csv.DictReader(open(a.labels, encoding="utf-8"))}
    pred = {u: s for u, s in st.db.execute("SELECT url, soft404 FROM pages")}
    tp = sum(1 for u, l in lab.items() if l == 1 and pred.get(u) == 1)
    fp = sum(1 for u, l in lab.items() if l == 0 and pred.get(u) == 1)
    fn = sum(1 for u, l in lab.items() if l == 1 and pred.get(u) != 1)
    print(json.dumps({"labelled": len(lab), "tp": tp, "fp": fp, "fn": fn,
                      "precision": round(tp / (tp + fp), 4) if tp + fp else None,
                      "recall": round(tp / (tp + fn), 4) if tp + fn else None}, indent=1))


def cmd_label_template(a):
    acts = json.loads((Path(a.out) / "report" / "actions.json").read_text(encoding="utf-8"))
    p = Path(a.out) / "labels_template.csv"
    with open(p, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["rank", "type", "title", "label_good_1_bad_0", "note"])
        for x in acts:
            w.writerow([x["rank"], x["type"], x["title"], "", ""])
    print(f"wrote {p} - fill label_good_1_bad_0 for each row (aim for 40 rows across your dev runs)")


def kappa(a, b):
    n = len(a)
    po = sum(1 for x, y in zip(a, b) if x == y) / n
    cats = set(a) | set(b)
    pe = sum((a.count(c) / n) * (b.count(c) / n) for c in cats)
    return (po - pe) / (1 - pe) if pe < 1 else 1.0, po


def cmd_judge(a):
    """Deterministic checks + LLM judge, calibrated against human labels (Cohen's kappa)."""
    from .claims import Verifier
    from .llm import LLM, parse_json, text_of
    rows = list(csv.DictReader(open(a.labels, encoding="utf-8")))
    llm = LLM(seed=42)
    results = []
    for r in rows:
        out = Path(r.get("out_dir") or a.out)
        acts = {x["rank"]: x for x in json.loads((out / "report" / "actions.json").read_text(encoding="utf-8"))}
        claims = json.loads((out / "report" / "claims.json").read_text(encoding="utf-8"))
        act = acts[int(r["rank"])]
        det_ok = all(c in claims for c in act["claim_ids"]) and bool(act["title"])
        cl = [claims[c] for c in act["claim_ids"]][:25]
        system = ("You are a senior SEO reviewing one recommendation. Given the action and its verified facts, answer "
                  "JSON {\"good\": 1 or 0, \"reason\": str}. good=1 only if the action is correct, specific and worth "
                  "doing for this site.")
        resp = llm.call(system, [{"role": "user", "content": json.dumps({"action": act, "facts": cl},
                                                                         ensure_ascii=False)[:18000]}], max_tokens=200)
        d = parse_json(text_of(resp)) if resp else None
        j = int(d.get("good", 0)) if isinstance(d, dict) else (1 if det_ok else 0)
        jl = j if det_ok else 0
        results.append({"rank": r["rank"], "human": int(r["label_good_1_bad_0"]), "judge": jl,
                        "judge_reason": (d or {}).get("reason") if isinstance(d, dict) else "no llm",
                        "deterministic_ok": det_ok, "title": act["title"], "out": str(out)})
    h = [x["human"] for x in results]
    j = [x["judge"] for x in results]
    k, po = kappa(h, j)
    wrong = [x for x in results if x["human"] != x["judge"]]
    print(json.dumps({"n": len(results), "agreement": round(po, 4), "cohens_kappa": round(k, 4),
                      "judge_wrong_examples": wrong[:5], "llm": llm.stats()}, indent=1, ensure_ascii=False))


def cmd_pair_template(a):
    d = json.loads((Path(a.out) / "report" / "cannibalization.json").read_text(encoding="utf-8"))
    p = Path(a.out) / "pair_labels_template.csv"
    with open(p, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["url_a", "url_b", "severity", "label_cannibal_1_not_0"])
        for x in d["top_pairs"][:a.n]:
            w.writerow([x["url_a"], x["url_b"], x["severity"], ""])
    print(f"wrote {p}: label each pair 1 (real cannibalization) or 0")


def cmd_ablation(a):
    """Part C ablation: drop one signal at a time, re-score + re-group stored pairs, precision@20 vs labels.
    Template-removal and embedding ablations need a re-run (see BENCHMARKS.md for the commands)."""
    from . import cannibal as C
    d = json.loads((Path(a.out) / "report" / "cannibalization.json").read_text(encoding="utf-8"))
    lab = {}
    for r in csv.DictReader(open(a.labels, encoding="utf-8")):
        if r.get("label_cannibal_1_not_0", "").strip() in ("0", "1"):
            lab[tuple(sorted((r["url_a"], r["url_b"])))] = int(r["label_cannibal_1_not_0"])
    pairs = d["top_pairs"]
    W = {"topic_sim": 35, "intent_match": 20, "content_overlap": 15, "title_overlap": 10, "anchor_conflict": 10,
         "link_split": 5, "shallow": 5}

    def sev(p, drop):
        ts = max(0.0, (p["topic_sim"] - C.SIM_MIN) / (1 - C.SIM_MIN))
        vals = dict(p, topic_sim=ts)
        return sum(W[k] * vals[k] for k in W if k != drop)

    out = {}
    for drop in [None] + list(W):
        ranked = sorted(pairs, key=lambda p: (-sev(p, drop), p["url_a"], p["url_b"]))
        top = [tuple(sorted((p["url_a"], p["url_b"]))) for p in ranked if tuple(sorted((p["url_a"], p["url_b"]))) in lab][:20]
        prec = sum(lab[t] for t in top) / len(top) if top else None
        out["full" if drop is None else f"-{drop}"] = {"precision_at_20": None if prec is None else round(prec, 4),
                                                        "labelled_in_top": len(top)}
    print(json.dumps(out, indent=1))


def cmd_label_kit(a):
    from .labelkit import build
    print(json.dumps(build(a.runs, a.html, a.pages, a.pairs), indent=1))


def cmd_determinism(a):
    def digest(d):
        out = {}
        for p in sorted(Path(d, "report").rglob("*")):
            if p.is_file():
                out[str(p.relative_to(d))] = hashlib.sha256(p.read_bytes()).hexdigest()
        return out
    da, db_ = digest(a.a), digest(a.b)
    diff = sorted(k for k in set(da) | set(db_) if da.get(k) != db_.get(k))
    print(json.dumps({"files": len(da), "identical": not diff, "different_files": diff}, indent=1))
    return 0 if not diff else 1


def cmd_diff_runs(a):
    """Explain WHY two runs differ: which pages were fetched in one run only, which changed status, and
    which pages' text changed (with the first differing words). Works on any two finished run folders."""
    import sqlite3
    import zlib
    from pathlib import Path

    def load(run):
        db = sqlite3.connect(str(Path(run) / "crawl.db"))
        pages = {r[0]: r[1:] for r in db.execute(
            "SELECT url, site, status, raw_hash, main_hash, title, rendered, html, render_reason FROM pages")}
        stats = {(s_, k): v for s_, k, v in db.execute("SELECT site, k, v FROM stats")}
        return pages, stats

    pa, sa = load(a.a)
    pb, sb = load(a.b)
    out = {}
    for site in ("target", "competitor"):
        A = {u: v for u, v in pa.items() if v[0] == site}
        B = {u: v for u, v in pb.items() if v[0] == site}
        only_a, only_b = sorted(set(A) - set(B)), sorted(set(B) - set(A))
        both = sorted(set(A) & set(B))
        status = [(u, A[u][1], B[u][1]) for u in both if A[u][1] != B[u][1]]
        raw = [u for u in both if A[u][1] == 200 and A[u][2] != B[u][2]]
        main = [u for u in both if A[u][3] != B[u][3]]
        rend = [u for u in both if A[u][5] != B[u][5]]
        examples = []
        for u in main[:5]:
            ta, tb = A[u][6], B[u][6]
            try:
                ta = zlib.decompress(ta).decode("utf-8", "replace") if ta else ""
                tb = zlib.decompress(tb).decode("utf-8", "replace") if tb else ""
            except zlib.error:
                ta, tb = str(ta or ""), str(tb or "")
            i = next((k for k in range(min(len(ta), len(tb))) if ta[k] != tb[k]), min(len(ta), len(tb)))
            examples.append({"url": u, "html_a": ta[max(0, i - 80):i + 120], "html_b": tb[max(0, i - 80):i + 120]})
        out[site] = {
            "pages_a": len(A), "pages_b": len(B),
            "fetched_only_in_a": len(only_a), "fetched_only_in_b": len(only_b),
            "examples_only_a": only_a[:8], "examples_only_b": only_b[:8],
            "status_changed": len(status), "examples_status": status[:8],
            "raw_html_changed": len(raw), "main_text_changed": len(main), "render_differs": len(rend),
            "examples_render": [(u, A[u][7], B[u][7]) for u in rend[:8]],
            "examples_main_text": main[:8],
            "stopped_at_step": [sa.get((site, "stopped_at_step")), sb.get((site, "stopped_at_step"))],
            "throttled": [sa.get((site, "throttled")), sb.get((site, "throttled"))],
            "first_html_difference": examples,
        }
    print(json.dumps(out, indent=1, ensure_ascii=False))


def main(argv=None):
    ap = argparse.ArgumentParser(prog="seo-intel-eval")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("extract-f1"); p.add_argument("--out", required=True); p.add_argument("--gold", required=True)
    p.add_argument("--sample", type=int, default=0)
    p = sub.add_parser("render-audit"); p.add_argument("--out", required=True); p.add_argument("--n", type=int, default=100)
    p = sub.add_parser("soft404"); p.add_argument("--out", required=True); p.add_argument("--labels", required=True)
    p = sub.add_parser("judge"); p.add_argument("--out", required=True); p.add_argument("--labels", required=True)
    p = sub.add_parser("label-template"); p.add_argument("--out", required=True)
    p = sub.add_parser("determinism"); p.add_argument("a"); p.add_argument("b")
    p = sub.add_parser("diff-runs"); p.add_argument("a"); p.add_argument("b")
    p = sub.add_parser("pair-template"); p.add_argument("--out", required=True); p.add_argument("--n", type=int, default=60)
    p = sub.add_parser("ablation"); p.add_argument("--out", required=True); p.add_argument("--labels", required=True)
    p = sub.add_parser("label-kit"); p.add_argument("runs", nargs="+"); p.add_argument("--html", default="label_kit.html")
    p.add_argument("--pages", type=int, default=60); p.add_argument("--pairs", type=int, default=60)
    a = ap.parse_args(argv)
    return {"extract-f1": cmd_extract_f1, "render-audit": cmd_render_audit, "soft404": cmd_soft404,
            "judge": cmd_judge, "label-template": cmd_label_template, "determinism": cmd_determinism,
            "pair-template": cmd_pair_template, "ablation": cmd_ablation, "label-kit": cmd_label_kit, "diff-runs": cmd_diff_runs}[a.cmd](a) or 0


if __name__ == "__main__":
    sys.exit(main())
