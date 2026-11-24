# BENCHMARKS

Two kinds of numbers appear here:

* **Measured: synthetic dev pair.** Two generated sites (`devsite/server.py`) that contain every hard case
  in the brief: robots rules, sitemaps, template boilerplate, near-duplicates, cannibalizing pages, hreflang
  Hindi pages, Hinglish pages, pagination, product + reviews, calendar/facet/session/path traps, a 2-hop
  redirect chain, a redirect loop, meta refresh, a JS redirect, X-Robots-Tag, a canonical chain, a canonical
  to a 404, a canonical to a redirect, soft 404s, JS-only pages and 2 planted competitor topics. Small
  (~190 pages), so it checks correctness, not scale.
* **Measured: scale test.** 50,000 pages per site (100k total) written straight into the store, then the
  full analysis (Parts B–E) run on it. This checks time and RAM at the brief's scale.
* **TO FILL: real dev pairs.** Run the commands below on your three real site pairs and paste the numbers.
  None are invented here.

Machine for the measured rows: 2-core cloud container, 8 GB RAM, Python 3.13.

## Hard limits

| Limit | Target | Synthetic dev pair | Scale test (50k/site) | Real pair 1 | Real pair 2 | Real pair 3 |
|---|---|---|---|---|---|---|
| Time, full run | ≤ 90 min | 45.5 s | analysis 313–419 s* | TO FILL | TO FILL | TO FILL |
| Peak RAM (process + children) | ≤ 2 GB | 600 MB (incl. Chromium) | 1,355 MB | TO FILL | TO FILL | TO FILL |
| LLM cost | ≤ $0.50 | $0 (no key) / capped at $0.45 | – | TO FILL | TO FILL | TO FILL |
| Max requests in any 1 s window per host | ≤ 6 | 6 / 6 | – | TO FILL | TO FILL | TO FILL |
| Robots: Google parser tests | pass | 20/20 pass | – | – | – | – |
| Repeatability (2 fresh runs) | identical | 12/12 files identical | – | TO FILL | TO FILL | TO FILL |
| Crash + resume = uninterrupted | identical | 4/4 kill points identical | – | – | – | – |
| Incremental re-run | ≤ 10% of first | 41 s vs 45 s (see note) | – | TO FILL | TO FILL | TO FILL |

\* 313 s with the MinHash signature cache warm, 419 s cold. At 6 req/s, 70 min of crawling fetches at most
~25k pages per host, so real analysis input is about half of the scale test.

**Incremental note.** On the tiny synthetic site, fixed costs dominate the second run: robots, sitemaps,
conditional re-checks of the ~120 depth-≤1 hub pages at 6 req/s, and ~15 s of analysis. So it does not show
the 10% ratio. On a real site, the first run is ~80 min, and a re-run re-checks only hubs, sitemap-`lastmod`
changes and new URLs. It is time-capped at 8% of the first run's crawl time. The 10% target must be measured
on real pairs (row above).

Commands:
```
seo-intel SITE --competitor COMP --out runA --fresh            # time / RAM / cost: runA/logs/run_stats.json
seo-intel SITE --competitor COMP --out runB --fresh
seo-intel-eval determinism runA runB                           # repeatability
seo-intel SITE --competitor COMP --out runA                    # next day: incremental, compare total_seconds
python -m pytest tests/test_robots_google.py                   # robots tests
python devsite/scale_test.py 50000 scale50k                    # scale test
```

## Part A — Crawler

| Target | Synthetic dev pair | Real pairs | Command |
|---|---|---|---|
| Traps detected and reported with pattern | calendar, facet, session, path: all 4 | TO FILL | `report/crawl_summary.json → traps` |
| Trap budget ≤ 5% | 3.68% | TO FILL | `… → trap_budget_share` |
| Render ≤ 15% of pages | 5.3% (7/131) | TO FILL | `… → rendered_share` |
| Catch ≥ 95% of JS-different pages | est. recall 1.00 (0/60 missed) | TO FILL | `seo-intel-eval render-audit --out runA --n 100` |
| Redirect chain / loop / meta / JS / X-Robots / canonical chain, to 404, to redirect | all found | TO FILL | `crawl_summary.json` |
| Soft-404 precision ≥ 90% | 1/1 flagged correct | TO FILL | label `url,label` then `seo-intel-eval soft404 --out runA --labels soft.csv` |
| Change report | title, canonical, new, removed: all detected | TO FILL | `report/change_report.json` after a 2nd run |

## Part B — Extraction & duplicates

| Target | Synthetic dev pair | Real pairs | Command |
|---|---|---|---|
| Token F1 ≥ 0.90 (60 hand-marked pages) | 0.995 on 156 generated pages (generated gold, easier than real) | TO FILL | save `{url: main text}` JSON, then `seo-intel-eval extract-f1 --out runA --gold gold.json` |
| Near-dup recall on 200 planted pairs | 1.00 (Jaccard ≥ 0.35) | TO FILL | `report/duplicates.json → planted_recall` |
| MinHash estimate error | mean abs 0.029 | – | see DECISIONS.md |

## Part C — Cannibalization & gaps

| Target | Synthetic dev pair | Real pairs | Command |
|---|---|---|---|
| precision@20 groups ≥ 0.80 | needs SEO labels | TO FILL | `seo-intel-eval pair-template --out runA`, label, then `seo-intel-eval ablation …` |
| precision@20 gaps ≥ 0.75 | planted gaps 2/2 found, 0 false | TO FILL | `report/gaps.json` |
| Sub-quadratic | IVF ANN; recall 0.9988 vs exact on 6k vectors | – | DECISIONS.md |
| LLM on ≤ 2% of pages | 0% (no key); capped at 2% in code | TO FILL | `analysis_summary.json → intents.llm_share` |
| Healthy pairs excluded | paginated 4, canonicalised 1, parent/child 1 | TO FILL | `cannibalization.json → summary.excluded_healthy` |

### Ablation table (Part C)

Fill after labelling ~60 pairs on each real pair:
```
seo-intel-eval pair-template --out runA          # writes runA/pair_labels_template.csv
seo-intel-eval ablation --out runA --labels runA/pair_labels_template.csv
SEO_INTEL_ABLATE_TEMPLATE=1 seo-intel SITE --competitor COMP --out runA_notpl --fresh   # no template removal
```

| Signal removed | precision@20 pair 1 | pair 2 | pair 3 |
|---|---|---|---|
| none (full) | TO FILL | TO FILL | TO FILL |
| − embeddings (topic_sim) | TO FILL | TO FILL | TO FILL |
| − intent match | TO FILL | TO FILL | TO FILL |
| − content overlap | TO FILL | TO FILL | TO FILL |
| − title overlap | TO FILL | TO FILL | TO FILL |
| − anchors (anchor_conflict) | TO FILL | TO FILL | TO FILL |
| − internal link split | TO FILL | TO FILL | TO FILL |
| − template removal (re-run above) | TO FILL | TO FILL | TO FILL |

## Part D — Link optimiser

| Target | Synthetic dev pair | Command |
|---|---|---|
| K links, ≤ 3 per source, relevant sources only | 36 of K=40 (greedy stops when no remaining candidate raises priority PageRank) | `report/link_plan.json` |
| Predicted vs recomputed PageRank (priority pages, ×1000) | 19.26 → 260.01 predicted, 260.01 recomputed | `… → total_after_*` |
| Greedy vs better method (brute force, 10 small graphs) | 99.55% mean / 95.46% min of optimal gain | `… → greedy_vs_optimal_small_graphs` |

## Part E — Agent

| Target | Measured | Command |
|---|---|---|
| Every claim verified; false claims removed and logged | mock LLM: 16 rejections logged, 0 invented claims in output | `python devsite/mock_agent_check.py runA` |
| ≤ 8,000 input tokens per call | enforced in `llm.py` (call refused above the cap) | – |
| Two runs identical (temperature 0 + response cache) | identical (no-key path) | `seo-intel-eval determinism` |
| Judge: Cohen's kappa on 40 own labels + a judge-wrong case | TO FILL | `seo-intel-eval label-template --out runA`, label 40 rows, then `seo-intel-eval judge --out runA --labels labels.csv` |
