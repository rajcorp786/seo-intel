# DECISIONS

For each part: what we use, at least one approach that failed, and the numbers that made us drop it.
Everything below was measured during development on the synthetic dev pair in `devsite/`
(`python devsite/server.py`) and the analysis scale test (`python devsite/scale_test.py N DIR`).

## Part A — Crawler

**Spider traps: decided before fetching, order-independent.**
* *Failed:* calendar traps caught only by the "template with ≥ 80% identical pages after 12 fetches" rule.
  On the dev site, **7.38%** of all fetches went to trap pages, above the **5%** limit.
* *Now:* date-like URLs are tracked per date template. A URL discovered ≥ 3 link levels deeper than the
  shallowest URL of its template is a next/prev chain, so it is a trap.
  Result: **3.68%** of fetches went to trap pages (`report/crawl_summary.json → trap_budget_share`).

**Facet explosions.**
* *Failed:* allow 25 query combinations per path, then block. Which 25 got in depended on the async fetch
  order, so two runs could crawl different pages.
* *Now:* ≥ 3 filter parameters means trap. 2-parameter combinations are kept 1-in-4 by URL hash.

**Rendering decisions.**
* *Failed:* probe the first 2 script-bearing pages of each template during the crawl. After a kill and
  resume, the rendered set was different (`render_reasons` low_text_js 5 vs 6, probe 2 vs 1), so the
  outputs were not byte-identical.
* *Now:* only content signals trigger renders during the crawl. These are low visible text with
  scripts/SPA markers, or `<noscript>` asking for JavaScript, and they are persisted as `pending:`
  in the store. Template probes run after the crawl, on the 2 lowest-hash pages per template.
  Kill at 6 s / 14 s / 22 s / 30 s, then resume: the reports were **byte-identical** to an
  uninterrupted run in **4 of 4** cases.

**Resume of a whole run.**
* *Failed:* a site whose crawl finished before the kill was re-crawled in "incremental" mode on resume
  (wrong mode, wrong change report).
* *Now:* a run-level `run_state` flag. On resume, sites that had already completed are kept as they are.

## Part B — Extraction & duplicates

**MinHash hash family.**
* *Failed:* `(a·x + b) mod (2^61−1)` with 31-bit `a`. It is almost linear in `x`, so it is not min-wise.
  Synthetic check: true Jaccard 0.333 gave an estimate of **0.039**. Planted-duplicate recall was
  **0.73** (32×4 bands) and **0.77** after re-banding.
* *Now:* 64-bit multiply-shift hashing. Mean absolute estimate error is **0.029** on 200 planted pairs,
  and planted recall is **1.00** (42 bands × 3 rows, threshold 0.35).

**Linking anchors to template blocks.**
* *Failed:* identifying `<a>` elements with Python `id()` of lxml proxies. The proxies are not stable,
  so footer links (Privacy/Terms/Products) were tagged *content* and article links were tagged *nav*.
* *Now:* every anchor gets a `data-si` index before segmentation.

## Part C — Cannibalization & gaps

**Topic vectors.**
* *Failed:* exact `TfidfVectorizer` with unigrams + bigrams. Peak RAM was **2,539 MB** at 10k pages per
  site, above the 2 GB limit.
* *Now:* hashed TF-IDF (2^20 buckets, streamed in chunks of 2,000), df-pruned to 60k features, then
  128-dim LSA. **505 MB** at 10k pages per site.

**Finding similar pages without all pairs.**
* *Failed:* random-hyperplane LSH with 12 tables × 12 bits. Its analytic recall is about **41%** at
  cosine 0.75 and **19%** at 0.62.
* *Now:* inverted-file ANN (k-means cells ≈ 4√n, 3 probes, exact cosine inside cells).
  Measured recall on 6,000 clustered vectors: **0.9988** at cosine ≥ 0.62 and **1.00** at ≥ 0.75.
* *Failed (memory):* keeping every pair above the threshold. At 50k pages per site this gave
  **4.14 M** pairs and **4,062 MB** peak. *Now:* top-12 neighbours per page (kNN graph).

**Gaps.**
* *Failed:* clustering target + competitor together (k = √(N/2) = 9). Only **1 of 2** planted
  competitor topics was found, because the other was merged with unrelated target pages.
* *Now:* cluster competitor pages only, in the shared space, and measure target coverage by cosine to
  each competitor centroid. **2 of 2** found.

**Grouping rule.** Requiring the same intent missed pairs such as "price guide" (informational) vs
"best price" (commercial). A pair now also groups when topic similarity ≥ 0.85 and title overlap ≥ 0.4.

## Part D — Internal-link optimiser

**Choice method.**
* *Failed:* pure first-order greedy (rank by the linearised gain `d·PR(s)/(out+1)·(h(t) − mean h(out(s)))`).
  On 10 random 30-node graphs against brute-force optimum it reached **98.69%** of the optimal gain on
  average, **91.41%** in the worst case.
* *Now:* the estimate shortlists the 12 best candidates, and an exact PageRank recompute picks among them,
  K times. **99.55%** on average, **95.46%** worst case (`link_plan.json → greedy_vs_optimal_small_graphs`).

**Anchors.**
* *Failed:* anchors from top TF-IDF bigrams of the target. They produced unnatural anchors such as
  "pit fewer" and "because price".
* *Now:* the anchor must be an exact n-gram of the sentence that matches the target's title, H1 or an
  existing anchor, and it must include a title/H1 word. Sentences that already hold a link are skipped.

## Part E — Claims & agent

* *Failed:* our own template prose wrote "301-redirect". The prose linter rejected it, because digits
  outside claim references are not allowed. Templates were reworded ("redirect permanently").
* Verified with a scripted fake LLM (`devsite/mock_agent_check.py`). On the first attempt it returns
  prose with a raw number plus an invented claim. **16 rejections** were logged, all actions were
  regenerated, and **0** invented claims and **0** raw facts reached the output.

## Severity score (Part C), defended

Severity uses only signals available without Search Console (weights sum to 100):
`35·topic_sim + 20·intent_match + 15·content_overlap + 10·title_overlap + 10·anchor_conflict + 5·link_split + 5·shallow`.
* Topic similarity and the same intent are what make two pages answer the same query, so together they
  carry 55 points.
* Content overlap separates "duplicate, merge it" from "same query, different page".
* Shared titles and shared incoming anchors are how Google receives conflicting relevance hints.
* An even internal-link split and both pages being shallow mean neither page clearly wins.
Use `seo-intel-eval ablation` to measure what each term adds on labelled pairs.
