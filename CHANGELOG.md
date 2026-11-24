# Changelog

`seo-intel --version` prints the installed version. Every run logs it in `logs/run.log` and `logs/run_stats.json`.

## 1.0.3 (2026-11-24): report quality
Found by a real run (hybonelevators.com vs eliteelevators.com).

| Area | What changed | Why |
|---|---|---|
| Gaps | One-page-per-place URL series (`/home-lifts-in-<city>/`) are detected and reported as ONE gap each; only the other pages are topic-clustered | 48 gaps were the same city-page templates split by city name; topic terms were city names |
| Link plan | At most K/5 new links per target page (min 3) | 27 of 33 links went to one page, all with the anchor "spare parts" |
| Priority pages | Paginated listings and category/tag archives are never chosen automatically | `/blogs/2/` … `/blogs/5/` were picked as priority pages |

## 1.0.2 (2026-11-24): slow and throttling sites
Found by a real run (daffodilsw.com vs capitalnumbers.com): 57 "slow down" (429/503) answers, the target crawl
was cut by the clock at 929 pages with 1,528 URLs left, so a second run could have ended elsewhere.

| Area | What changed | How it was checked |
|---|---|---|
| Repeatability on slow sites | The crawl grows in fixed steps (200, 300, 450 … pages) and stops only at a step, when the next one does not safely fit in the time left; redirect/canonical targets go to the next link level | slow dev site (4 s per request, random jitter, tight budget): 3 of 3 runs byte-identical; crash + resume at 4 kill points identical |
| Throttling | After a 429/503 the crawler never returns to the rate that was rejected (settles one step below) | unit test |
| Render audit | Resumable after a crash | crash test above |

## 1.0.1 (2026-11-24): toward the full brief
| Area | What changed | How it was checked |
|---|---|---|
| Repeatability | Deterministic BFS crawl order, fixed page cap, sample-based template-trap verdicts; fixed a retry/gate stall | 3 capped + 4 full runs with random server latency identical; e2e with 429s identical |
| Incremental | Re-fetch only start page, hubs, changed `<lastmod>`, new URLs, 1-in-20 rotation; reused pages replayed exactly; render verdicts reused | output = fresh crawl except change report; 8.2 s vs 48.4 s on dev pair; 4/4 planted changes found |
| Robots | `parse_report()` (valid/unknown lines, line numbers) + 3 more Google reporting tests | 23/23 |
| Intent | schema.org / og:type signals, link density, fixed "Home …" title false positive | unit tests; 7 posts relabelled correctly |
| Link plan | Anchors that name only the target; one link per sentence; "widen" suggestions with verified claims | unit tests; mock-agent check 0 false claims |
| Self-checks | Render-recall audit and soft-404 self-test in every run | `crawl_summary.json → render_recall_audit`, `analysis_summary.json → soft404_self_test_*` |
| Store | Old `crawl.db` files are migrated (new columns added) | – |
| Docs | CONTRIBUTING.md; `pip install git+…` line in README | – |

## 1.0.0 (2026-11-24): first submission
Development builds r01–r09, each tested on the synthetic dev sites and, from r02 on, on real sites.

| Build | What changed | Found by |
|---|---|---|
| r01 | Full pipeline: Google-compatible robots.txt, polite crawler, traps, redirects, selective rendering, crash-safe incremental store; template-learning extraction, MinHash-LSH dedup, Hindi/Hinglish tokenizer; topics, intent, cannibalization, gaps; PageRank link optimiser; claim-verified LLM agent; tests and docs | – |
| r02 | Rendering ~2.5× faster (script cache, tracker blocking, 3 parallel pages); agent stops using the LLM before the run deadline; stricter LLM prompt | real run: backlinko.com vs seo.com |
| r03 | Redirect hops count only 3xx responses; anchors need ≥ 2 words incl. a distinctive title word, never in headings; "same page type, different entities" exclusion; render probes per site section; free-tool intent | same run |
| r04 | 429/503 back-off and retry like Googlebot; JS-redirect false positives from SSO/click handlers removed; shorter LLM output; clear error when robots.txt is unreachable | real run: beardo.in vs bombayshavingcompany.com |
| r05 | Every wait bounded: 25 s render cap, aborted sub-resources on throttled hosts, ≤ 10 s shutdown, crawl watchdog | the r04 run overran its time budget |
| r06 | An explicit subdomain (docs.example.com) limits the crawl to that subdomain; tough-site batch + results collector | review |
| r07 | BENCHMARKS filled with real-run numbers; run folders git-ignored | – |
| r08 | Click-through labelling kit, label scoring script, 90-minute big-run script | – |
| r09 | Install steps that work on any laptop (`python -m pip`, venv), troubleshooting table, pytest included | clean-machine install test |
