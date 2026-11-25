# Changelog

`seo-intel --version` prints the installed version. Every run logs it in `logs/run.log` and `logs/run_stats.json`.

## 1.1.2 (2026-11-25)
Live pair re-run with 1.1.1: competitor (1,600 pages) **fully identical**; target identical except 1 of 118
pages (`?elementskit_template=new-header`), whose render gain sat on the 0.20 line: stored rendered in one run,
raw in the other.

| Area | What changed |
|---|---|
| Render | A gain between 0.10 and 0.35 is checked with a second render; the rendered DOM is used only if both renders agree it changes the page |

## 1.1.1 (2026-11-25)
Live pair re-run with 1.1.0 (hybonelevators.com vs eliteelevators.com, two fresh runs): same pages, same stop
step, **0 pages with different main text, 0 status changes**. Only the "rendered" flag of 8 pages differed:
renders that changed nothing (gain 0.00–0.09) succeeded in one run and failed/timed out in the other.

| Area | What changed |
|---|---|
| Render | A render that fails twice is recorded as an attempt (counts toward the 15% cap, raw HTML kept), the same as a render that changed nothing, so reports no longer depend on browser timeouts |
| Errors | A site that gives no HTML page says why (unreachable, robots, off-site redirect, 401/403 …) in the log and in `crawl_summary.json → warning` |
| Tests | End-to-end tests fail on an empty crawl instead of passing as "identical" |

## 1.1.0 (2026-11-25): live-site repeatability, tested end to end
Instead of fixing one live-run difference at a time, a test site now behaves like a live one
(`devsite/server.py --chaos`): 4% 502s and dropped connections, per-request nonces and clocks, CSS variants,
a shuffled "related posts" widget, a footer clock and a JS widget with random content and a random delay.
Every difference found there was fixed:

| Area | What changed |
|---|---|
| Render | Fixed browser clock (2026-01-01, time moves only in fixed steps) and seeded `Math.random` / `crypto.getRandomValues`: the same page renders to the same DOM |
| Render | The render gain ignores numbers (clocks, counters), so a page cannot cross the 20% threshold in one run only |
| Render cap | Incremental runs counted only new renders against the 15% cap; reused rendered pages now count too (one incremental run had reached 20%) |
| Eval | `determinism` / `diff-runs` now stop with an error on a wrong or unfinished folder instead of reporting "identical" |
| Tests | `SEO_INTEL_E2E=1 SEO_INTEL_CHAOS=1 pytest tests/test_e2e.py`: two fresh runs on the chaos site must be byte-identical with the same main text on every page |

Checked: 3 fresh chaos runs identical; crash + resume (2 kill points) on the chaos site identical to an
uninterrupted run; slow site (4 s/request) identical; hanging-JS run 107 s of a 120 s budget; mock agent 0 false
claims; 48 unit tests + 2 end-to-end tests.

## 1.0.9 (2026-11-25): rendering made repeatable
Second live diff-runs (v1.0.8): no network errors any more, same pages and stop step; the remaining
differences were 7 target pages rendered in one run only and 4 pages whose stored text was the rendered DOM in
one run and the raw HTML in the other, plus 1 competitor page the site served in a different variant.

| Area | What changed |
|---|---|
| Render | The rendered DOM replaces the stored page only when the visible text grows by more than the render threshold (20%); small, timing-dependent gains keep the raw HTML |
| Render | A failed or timed-out render is tried once more before giving up |
| diff-runs | Shows render reasons and gains of pages rendered differently, and the pages whose main text changed |

## 1.0.8 (2026-11-25): what diff-runs found on the live pair
`seo-intel-eval diff-runs` on two fresh runs (hybonelevators.com vs eliteelevators.com): same pages, same stop
step (2,277 competitor pages), and the same main text everywhere except (a) 2 target pages that failed with a
network error in one run only, and (b) the competitor site edited its JSON-LD between the runs (a trailing
comma). Everything downstream (topics, groups, plan) shifts when even two pages differ.

| Area | What changed |
|---|---|
| Crawl | Network errors / 5xx: 3 retries with 2, 4, 8 s back-off (was 2 × 1 s in 1.0.7, none before) |
| Parse | JSON-LD with trailing commas is read instead of dropped |

## 1.0.7 (2026-11-25): repeatability on live sites
Two fresh runs on hybonelevators.com vs eliteelevators.com gave different outputs (11 of 12 files).

| Area | What changed |
|---|---|
| Diagnosis | `seo-intel-eval diff-runs runA runB`: pages fetched in one run only, status changes, changed text with the first differing HTML |
| Crawl | Timeouts, resets and 5xx answers are retried twice instead of being stored as a different error in each run |
| Crawl | Stop steps grow ×2 (200, 400, 800 …) with a 2× time margin, so two runs at different speeds land on the same step more often |

## 1.0.6 (2026-11-24): calibrated on the real run
| Area | What changed | Why (v1.0.5 run, hybonelevators.com vs eliteelevators.com) |
|---|---|---|
| Gaps | Family threshold 0.75 → 0.50 | the closest unmerged keyword variants scored 0.51–0.73 (e.g. home-lifts-in / lift-for-homes-in 0.73); different topics on the dev site score ≤ 0.04 |
| Anchors | The key-word rule became a preference; instead the same anchor text may point at the same page at most twice | the strict rule left only 3 links (PR ×1000 39.2 → 42.9); the real problem was 5 identical "waiting time" links |
| Errors | A report file open in Excel gives a clear message instead of a traceback | real run on Windows |

## 1.0.5 (2026-11-24): anchors must name the target
Found by the v1.0.4 run on hybonelevators.com vs eliteelevators.com.

| Area | What changed | Why |
|---|---|---|
| Anchors | An exact anchor must contain one of the target's two rarest title words; no commas/brackets inside | "waiting time" ×5 pointed at the hospital-lift page; "quality, parts" |
| Gaps | `summary.closest_unmerged_series` lists the most similar series pairs that were not merged | to check the family threshold (cosine 0.75) on real sites; v1.0.4 merged 4 families, 22 series remain |

## 1.0.4 (2026-11-24): anchors and gap families
Found by the v1.0.3 run on hybonelevators.com vs eliteelevators.com.

| Area | What changed | Why |
|---|---|---|
| Gaps | URL series that are keyword variants of one page family (same entities in the endings, near-identical content) are merged into one gap | 27 of 28 gaps were the same city-page family under different keywords ("home-lifts-in", "house-lifts-in", "residential-lifts-in" …) |
| Anchors | No "and"/"or" inside an anchor unless it is a whole title/H1 segment | "space and installation", "quality, parts and service", "Escalators and moving" |
| Anchors | The distinctive word must appear in the body of at most 25% of the site's pages | generic anchors such as "spare parts", "waiting time", "right one" |

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
