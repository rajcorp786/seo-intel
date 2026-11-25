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
* **Measured: real site pairs.** Crawled from a Windows 11 laptop with `--time-budget 20` (20-minute runs,
  not the full 90). All numbers are copied from each run's `logs/run_stats.json`, `logs/run.log` and
  `report/crawl_summary.json`. Where a number could not be measured it says so. Nothing is estimated.
  * Pair 1: backlinko.com vs seo.com (tool version r04)
  * Pair 2: beardo.in vs bombayshavingcompany.com (r05). Both are Shopify stores that rate-limit bots heavily (429).
  * Pair 3: not completed. The Hindi pair tried (hindigyapan.com vs hindime.net) was unusable: the target's
    robots.txt was unreachable, and the competitor is now a ~10-page tool site. The tool correctly crawled
    nothing on the target, because Google treats an unreadable robots.txt as "do not crawl".

Machine for the measured rows: 2-core cloud container, 8 GB RAM, Python 3.13.

## Hard limits

| Limit | Target | Synthetic dev pair | Scale test (50k/site) | Real pair 1 (backlinko vs seo.com) | Real pair 2 (beardo vs bombayshaving) |
|---|---|---|---|---|---|
| Time, full run | ≤ budget | 48.4 s | analysis 313–419 s* | **638 s** of a 1,200 s budget | **1,115 s** of a 1,200 s budget (crawl stopped at its 936 s deadline) |
| Peak RAM (process + children) | ≤ 2 GB | 663 MB (incl. Chromium) | 1,355 MB | **975 MB** | **313 MB** |
| LLM cost | ≤ $0.50 | $0 (no key) / capped at $0.45 | – | **$0.0255** | **$0.0183** |
| Max requests in any 1 s window per host | ≤ 6 | 6 / 6 | – | 6 / 6 (r02 run) | 6 / 6 (r02 run) |
| Robots: Google parser tests | pass | 23/23 pass | – | – | – |
| Repeatability (2 fresh runs) | identical | 12/12 files identical, also with 429 responses and 0–300 ms random server latency (`server.py --jitter`) | – | not measured (live sites change between runs) | not measured |
| Repeatability on a live-like site (`server.py --chaos`: 502s, dropped connections, nonces, clocks, shuffled and JS widgets) | identical | 3/3 fresh runs identical, 0 pages with different main text | – | live pair hybonelevators.com vs eliteelevators.com: v1.0.6 11/12 files differed; diff-runs traced it to network errors, rendering and one site edit (fixed in 1.0.7–1.1.0); re-run with 1.1.0: same pages, 0 main-text and 0 status differences; only the rendered flag of 8 zero-gain pages differed (fixed in 1.1.1); re-run with 1.1.1: competitor 1,600/1,600 pages identical, target 117/118 identical (1 page on the render threshold); re-run with 1.1.5: competitor 1,600/1,600 identical, target 117/118 identical (the same page: stored rendered in one run, unrendered in the other; renderer reworked in 1.1.6, not re-measured) | not measured |
| Repeatability when time runs out (slow site) | identical | 3/3 runs identical (4 s/request + jitter, 1.5 min budget; crawl stops at a fixed step) | – | not measured | not measured |
| Crash + resume = uninterrupted | identical | 4/4 kill points identical | – | – | – |
| Incremental re-run | ≤ 10% of first | 8.2 s vs 48.4 s = 17% (see note); output identical to a fresh crawl except `change_report.json` | – | not measured | not measured |

Real-run crawl facts:

| | Pair 1 target | Pair 1 competitor | Pair 2 target | Pair 2 competitor |
|---|---|---|---|---|
| HTML pages crawled | 556 | 601 | 35 | 293 |
| Rendered share (≤ 15%) | 9.9% (55) | 7.7% (46) | 0% | 0% |
| Spider-trap URLs avoided | 7,476 (`/?p=` shortlinks, 12/12 identical) | 0 | 0 | 463 (facet / auth-redirect params) |
| Trap budget share (≤ 5%) | 1.4% (r02 run) | 0% | 0% | 0% |
| Near-duplicates / planted recall | 124 / 0.995 | – | 0 / 1.0 | – |
| Cannibalization groups | 26 (647 "different entity" pairs excluded) | – | 0 | – |
| Link plan | 40 links, priority PR×1000 4.78 → 16.94 | – | 13 links, 43.48 → 70.56 | – |
| Actions in plan | 25 | | 19 | |

Pair 2 is limited by the sites, not the tool. Shopify answered 429 to ~90% of requests even at 6 req/s,
and the crawler backs off as Googlebot does (DECISIONS.md, Part A).

\* 313 s with the MinHash signature cache warm, 419 s cold. At 6 req/s, 70 min of crawling fetches at most
~25k pages per host, so real analysis input is about half of the scale test.

**Incremental note (v1.0.1).** A re-run re-fetches only: the start page, the 2% most-linked pages (min 10),
pages whose sitemap `<lastmod>` changed, new URLs, and a rotating 1-in-20 sample (so every page is re-checked
within ~20 runs). Every other page is reused from the store and replayed exactly as if fetched (links,
canonicals, trap samples), and render verdicts per site section are reused. On the 180-page dev pair this is
51 requests instead of 243, and 8.2 s instead of 48.4 s. About 4 s of that is fixed cost (start-up, robots,
sitemaps, two 404 probes, analysis), which is why a tiny site does not reach 10%. The variable part scales with
2% + 5% + changed pages. The 10% target has to be shown on a real pair (row above, not measured yet).
Known limit: a change to boilerplate only (e.g. a "related posts" sidebar) on a page whose `<lastmod>` did not
change is picked up by the rotation, not on the next run.

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
| Traps detected and reported with pattern | calendar, facet, session, path: all 4 | pair 1: template trap (`/?p=` shortlinks) + session id; pair 2: facet params | `report/crawl_summary.json → traps` |
| Trap budget ≤ 5% | 3.68% | pair 1: 1.4%; pair 2: 0% | `… → trap_budget_share` |
| Render ≤ 15% of pages | 14.5% (19/131, incl. 4 audit renders) | pair 1: 9.9% / 7.7%; pair 2: 0% | `… → rendered_share` |
| Catch ≥ 95% of JS-different pages | automatic audit in every run: 0 of 4 sampled unrendered pages changed when rendered, est. recall 1.00; gold audit 0/60 missed | not measured on real pairs | `crawl_summary.json → render_recall_audit`; `seo-intel-eval render-audit --out runA --n 100` |
| Redirect chain / loop / meta / JS / X-Robots / canonical chain, to 404, to redirect | all found | pair 1: 2-hop chains + single redirects to an external site (hop-count bug found and fixed, r03); pair 2: false JS redirects from an SSO script (fixed, r04) | `crawl_summary.json` |
| Soft-404 precision ≥ 90% | 1/1 flagged correct; automatic self-test (second missing URL of another shape) passes on both sites | pair 1: 0 flagged; precision needs labels (not done) | label `url,label` then `seo-intel-eval soft404 --out runA --labels soft.csv` |
| Change report | title, canonical, new, removed: all detected | not measured (no next-day run) | `report/change_report.json` after a 2nd run |

## Part B — Extraction & duplicates

| Target | Synthetic dev pair | Real pairs | Command |
|---|---|---|---|
| Token F1 ≥ 0.90 (60 hand-marked pages) | 0.995 on 156 generated pages (generated gold, easier than real) | needs 60 hand-marked pages (not done) | save `{url: main text}` JSON, then `seo-intel-eval extract-f1 --out runA --gold gold.json` |
| Near-dup recall on 200 planted pairs | 1.00 (Jaccard ≥ 0.35) | pair 1: 0.995; pair 2: 1.00 | `report/duplicates.json → planted_recall` |
| MinHash estimate error | mean abs 0.029 | – | see DECISIONS.md |

## Part C — Cannibalization & gaps

| Target | Synthetic dev pair | Real pairs | Command |
|---|---|---|---|
| precision@20 groups ≥ 0.80 | needs SEO labels | needs SEO labels (not done) | `seo-intel-eval pair-template --out runA`, label, then `seo-intel-eval ablation …` |
| precision@20 gaps ≥ 0.75 | planted gaps 2/2 found, 0 false | pair 1: 1 gap (competitor's city-directory pages), not judged | `report/gaps.json` |
| Sub-quadratic | IVF ANN; recall 0.9988 vs exact on 6k vectors | – | DECISIONS.md |
| LLM on ≤ 2% of pages | 0% (no key); capped at 2% in code | capped at 2% in code; per-run value in `analysis_summary.json` | `analysis_summary.json → intents.llm_share` |
| Healthy pairs excluded | paginated 4, canonicalised 1, parent/child 1 | pair 1: paginated 505, parent/child 16, different entities 647 | `cannibalization.json → summary.excluded_healthy` |

### Ablation table (Part C)

Not done yet: this needs ~60 labelled pairs per site pair. Commands:
```
seo-intel-eval pair-template --out runA          # writes runA/pair_labels_template.csv
seo-intel-eval ablation --out runA --labels runA/pair_labels_template.csv
SEO_INTEL_ABLATE_TEMPLATE=1 seo-intel SITE --competitor COMP --out runA_notpl --fresh   # no template removal
```

| Signal removed | precision@20 pair 1 | pair 2 | pair 3 |
|---|---|---|---|
| none (full) | not done | – | – |
| − embeddings (topic_sim) | not done | – | – |
| − intent match | not done | – | – |
| − content overlap | not done | – | – |
| − title overlap | not done | – | – |
| − anchors (anchor_conflict) | not done | – | – |
| − internal link split | not done | – | – |
| − template removal (re-run above) | not done | – | – |

## Part D — Link optimiser

| Target | Synthetic dev pair | Command |
|---|---|---|
| K links, ≤ 3 per source, relevant sources only | 13 of K=40: only anchors that name the target alone are allowed (v1.0.1), so fewer candidates exist | `report/link_plan.json` |
| Predicted vs recomputed PageRank (priority pages, ×1000) | 19.52 → 63.04 predicted, 63.04 recomputed | `… → total_after_*` |
| Greedy vs better method (brute force, 10 small graphs) | 99.55% mean / 95.46% min of optimal gain | `… → greedy_vs_optimal_small_graphs` |

## Part E — Agent

| Target | Measured | Command |
|---|---|---|
| Every claim verified; false claims removed and logged | mock LLM: 16 rejections logged, 0 invented claims in output | `python devsite/mock_agent_check.py runA` |
| ≤ 8,000 input tokens per call | enforced in `llm.py` (call refused above the cap) | – |
| Two runs identical (temperature 0 + response cache) | identical (no-key path) | `seo-intel-eval determinism` |
| Judge: Cohen's kappa on 40 own labels + a judge-wrong case | not done (needs 40 own labels) | `seo-intel-eval label-template --out runA`, label 40 rows, then `seo-intel-eval judge --out runA --labels labels.csv` |

## Real pair 3 (v1.0.1 run, 30-minute budget): daffodilsw.com vs capitalnumbers.com

| | Value |
|---|---|
| Total time | 1,527 s of 1,800 s |
| Peak RAM | 876 MB |
| LLM cost | $0 (no key set) |
| Max requests in any 1 s window | 6 on both hosts |
| HTML pages | target 929 (crawl cut by the deadline after 57 throttle responses; 1,528 URLs left), competitor 1,698 (complete) |
| Rendered share | target 0% (no time left), competitor 1.7% (29); render audit competitor: 0 of 4 missed |
| Traps | target: 15 HubSpot tracking-parameter patterns avoided; competitor: facet + template trap on `/book-an-appointment`, trap budget 0.67% |
| Found | target: 2 redirect loops (www ↔ insights), 12 redirect chains, 1 soft 404; competitor: 1 loop, 1 chain, 1 canonical → redirect |
| Claims | 299 verified, 0 rejected |
| Repeatability | not measured on this pair. The deadline cut seen here is what v1.0.2 changes (see DECISIONS.md, Part A) |
