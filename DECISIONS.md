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

**Crawl order: deterministic BFS (v1.0.1).**
* *Failed:* a priority queue fed by whichever fetch finished first. With random server latency
  (`server.py --jitter`) the page-capped crawl fetched a different set of pages in 2 of 3 runs.
* *Now:* one link depth at a time, URLs of a level in fixed URL-hash order, a page cap fixed from the time
  budget (rate × crawl time × 0.75), template-trap verdicts taken on the first 12 dispatched samples of each
  template (sorted by URL), and renders drained in heap order at each level boundary. 3 capped and 4 full
  jittered runs, and the e2e test with 429s, gave identical outputs.
* *Bug found on the way:* a 429-retried URL of a template whose 12 samples were already dispatched waited for
  its own verdict, so the level ended early (12 URLs left unfetched, traps missing). Retried samples now pass
  the gate, and a template whose samples all finished gets its verdict instead of waiting.

**Incremental re-runs (v1.0.1).**
* *Failed:* re-check every depth ≤ 1 page with a conditional GET. Sitemap URLs are depth 1, so the second run
  re-requested 134 of 159 URLs and took 23 s of a 27 s crawl. Unchanged pages also enqueued their stored links
  only, so canonical targets and session-id records were lost (a 404 canonical target went missing).
* *Now:* re-fetch only the start page, the 2% most-linked pages, changed `<lastmod>` (compared with the value
  stored at the last fetch, not with the clock, since many sitemaps carry old dates), new URLs and a rotating
  1-in-20 sample. Reused pages are re-parsed from the stored HTML, so links, nofollow rules, canonicals,
  session-id records and trap samples come out exactly as from a fetch. Result: incremental output equals a
  fresh crawl except `change_report.json`; all four planted next-day changes are detected.

**Repeatability on a live pair (v1.0.7–1.0.8).**
* *Failed:* store a timeout as the page's result. Two fresh runs on hybonelevators.com differed in 11 of 12
  files; `diff-runs` showed only 2 target pages differed (network error in one run, 200 in the other) and the
  competitor had edited its markup between runs. Two changed pages move the whole topic space.
* *Now:* transient failures are retried 3 times with back-off; JSON-LD with trailing commas is read.
* *Limit that remains:* if a site really changes between two runs, the output changes too. That is correct
  behaviour, but it means two runs should be made back-to-back.

**Running out of time (v1.0.2, found on a real throttling site).**
* *Failed:* crawl until the clock says stop. On daffodilsw.com the site answered 429/503 57 times, the
  rate kept climbing back to the rejected rate, and the crawl was cut at 929 pages with 1,528 URLs left and
  no time to render. Where the clock cuts is different in every run, so outputs would differ.
* *Now:* (1) the rate settles one step below the rate the server rejected; (2) the crawl grows in fixed
  steps (200, 300, 450 … pages) and continues to the next step only if 1.6× its expected time plus a render
  reserve fits. A slow site therefore ends at a fixed page count. Slow dev site (4 s/request + jitter, tight
  budget): **3 of 3** runs byte-identical, each stopping at the same step.
* *Limit that remains:* if two runs differ so much in speed that one fits a step and the other does not,
  they stop at different steps. The hard deadline is still enforced, so time limits hold either way.

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

**Render speed (first real run, backlinko.com vs seo.com, 20-min budget).**
* *Failed:* each render re-downloaded every JS bundle through the 6 req/s limiter, waited up to 5 s for
  network idle, and post-crawl probes ran one at a time. **66 renders took 731 s (~11 s each)**, and the
  run took **1,319 s** against a 1,200 s budget.
* *Now:* scripts are cached across renders (150 MB cap), analytics/ads/consent trackers are blocked,
  3 pages render in parallel (batches in a fixed order, so runs stay repeatable), and the idle wait is 2.5 s.
  The agent also switches to template prose 60 s before the run deadline.

**Redirect hops (first real run).**
* *Failed:* the "offsite" marker in a chain was counted as a hop. A single 301 from backlinko.com to
  searchengineland.com was reported as "2 hops", with the final URL listed twice, and the LLM then
  guessed at a loop. *Now:* only 3xx responses count as hops. Single redirects that still receive
  ≥ 10 internal links become "point internal links at the final page". A `redirect_leaves_site` claim
  records off-site moves.

**Render probes on slug URLs (first real run).** Templates replaced digits only, so on a slug-only site
every page was its own template and probes used the whole 15% budget (77 of 77 renders were probes).
Probes now group by site section (last path segment → `*`).

**Rate-limited servers (real run, beardo.in vs bombayshavingcompany.com, both Shopify).**
* *Failed:* a fixed 6 req/s with no reaction to throttling. Shopify answered **429** to **932 of 1,013**
  (beardo.in) and **817 of 908** (bombayshavingcompany.com) page requests, so ~90% of each site was lost.
* *Now:* like Googlebot. A 429/503 halves that host's rate (minimum 1 req/s), pauses for `Retry-After`
  (2–60 s, default 10 s) and re-queues the URL, up to 4 retries. The rate recovers one step per 30 s
  without a penalty. Dev site: every product page answers 429 once, and the final crawl has **0** pages
  left at 429. Throttle counts per host are in `logs/run_stats.json`.

**Hard time limits (real run r04, same Shopify pair).**
* *Failed:* the crawl phase overran its deadline by ~270 s (frontier 1,205 s vs a 936 s deadline; whole run
  1,228 s vs a 1,200 s budget). At shutdown the crawler waited without a limit for an in-flight render,
  and a render's sub-resource requests queued behind the throttled host's back-off.
* *Now:* every wait is bounded. Each render has a 25 s hard cap. Sub-resources from a paused host are
  aborted, not queued. At the deadline, in-flight work gets ≤ 10 s to cancel. The whole crawl phase has
  a watchdog. Back-off penalties from one 429 burst count once (5 s debounce), and the pause cap is 30 s.
  Test: a JS file that hangs for 200 s and a 120 s budget gave a total run of **99 s**.

**JS-redirect false positives (same run).**
* *Failed:* (1) any `location.href='…'` in a small inline script, including click handlers; (2) any
  navigation seen while rendering. bombayshavingcompany.com got **~85** "JS redirects", mostly a
  third-party SSO script (`/url?q=…sso_continue…`) and a click handler sending to `/`. Because "redirected"
  pages are left out of the topic space, cannibalization found only 2 groups.
* *Now:* static detection skips scripts with handlers, conditions or named functions. Render-time
  navigations to login/SSO/consent paths, or with `return_to`/`continue`/`sso*` params, are ignored.

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

**Gaps on sites with one page per city (v1.0.3, real run).**
* *Failed:* topic-cluster all competitor pages. eliteelevators.com has ~1,350 pages built from ~27 templates
  ("home lifts in <city>", "stairlifts in <city>" …). K-means split them by city, so the report listed
  **48** gaps whose topic terms were city names (bathinda, amritsar …): one finding repeated 48 times.
* *Now:* URL series are found first (a path prefix of ≥ 2 words shared by ≥ 8 pages that differ only in the
  last 1–3 words). Each series is one gap candidate; the remaining pages are clustered by topic. On the same
  1,359 URLs this gives **27** series (home-elevators-in 72 pages, stairlifts-in 58 …) and 15 other pages.

**Gap families (v1.0.4, real run).** After v1.0.3, 27 of 28 gaps were still one strategy: the same
~405 place names under 27 keyword variants. Series that share ≥ 3 endings and whose content centroids have
cosine ≥ 0.75 are now one gap ("N URL patterns, e.g. …"); different topics with the same endings (cosine
below 0.75) stay apart (unit test).

**Family threshold calibrated (v1.0.6).** With 0.75, 22 series stayed apart; the report's
`closest_unmerged_series` showed the keyword variants at cosine 0.51–0.73, while different topics on the dev
site are at ≤ 0.04. The threshold is now 0.50.


**Intent signals (v1.0.1).**
* *Failed:* the navigational title rule `^(home|about|…)\b` matched "Home lift price basics", so 7 blog
  posts were labelled navigational. The link-density signal was always 0 (never computed).
* *Now:* the word must be the whole title or be followed by a separator ("Home | Brand"). schema.org types
  (JSON-LD incl. `@graph`, microdata) and `og:type` add weight: Product/Offer → transactional,
  Article/BlogPosting/HowTo/QAPage → informational, AboutPage/ContactPage → navigational, Review → commercial
  (not when the page is a Product). Site-wide types (WebSite, Organization, BreadcrumbList) add nothing.
  Link density = in-content links per 100 words. On the dev pair the 7 posts became informational.


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

**Same page type, different entities (first real run, backlinko.com).**
* *Failed:* topic similarity + intent only. Pairs such as "Ahrefs Review" vs "Serpstat Review",
  "Claude Statistics" vs "Signal Statistics", and two different free-tool pages scored severity 52–69,
  because their shared page template dominates the vectors (topic similarity up to 0.997, content
  overlap 0.01–0.23).
* *Now:* words present in ≥ 3% of the site's titles/H1s (≥ 5 pages) are template words. If the two titles'
  remaining words don't overlap, the pair is excluded as "same page type, different entities". Pairs with
  content overlap ≥ 0.7 are never excluded, because they are real duplicates.

**Grouping rule.** Requiring the same intent missed pairs such as "price guide" (informational) vs
"best price" (commercial). A pair now also groups when topic similarity ≥ 0.85 and title overlap ≥ 0.4.

## Part D — Internal-link optimiser

**Choice method.**
* *Failed:* pure first-order greedy (rank by the linearised gain `d·PR(s)/(out+1)·(h(t) − mean h(out(s)))`).
  On 10 random 30-node graphs against brute-force optimum it reached **98.69%** of the optimal gain on
  average, **91.41%** in the worst case.
* *Now:* the estimate shortlists the 12 best candidates, and an exact PageRank recompute picks among them,
  K times. **99.55%** on average, **95.46%** worst case (`link_plan.json → greedy_vs_optimal_small_graphs`).

**Too strict anchors (v1.0.5 → v1.0.6, real run).**
* *Failed:* require one of the target's two rarest title words in every exact anchor. On hybonelevators.com
  only 3 links were left (priority PageRank ×1000 39.2 → 42.9, before 23 links reached 121).
* *Now:* such anchors are preferred, and the same anchor text may point at the same page at most twice.

**Generic anchors (v1.0.4, real run).**
* *Failed:* "distinctive" meant "not common in the site's titles". On hybonelevators.com that still allowed
  "spare parts" (8 links), "waiting time" (4), "right one", and lists such as "space and installation".
* *Now:* the anchor's distinctive word must also be rare in the site's body text (≤ 25% of pages), and an
  anchor with "and"/"or" must be a whole title/H1 segment. Fewer links can be found; the plan says so.

**One page must not take the whole budget (v1.0.3, real run).**
* *Failed:* maximise total priority PageRank with only the per-source limit. On hybonelevators.com 27 of 33
  links went to one page, each with the anchor "spare parts" (its PageRank ×1000 went 3.9 → 86.1, the other
  nine pages barely moved). Mathematically best, but 27 identical exact-match anchors look manipulative.
* *Now:* at most K/5 links (min 3) per target page; paginated listings and archives are not auto-priority.

**Anchors that name only the target (v1.0.1).**
* *Failed (3rd version):* "home lift" was proposed as the anchor to 5 different product pages, because every
  product title contains it. That creates the anchor-text cannibalization Part C reports.
* *Now:* an anchor may not occur in the title/H1 of any other page, nor in the phrases of another priority
  page; a number counts as distinctive ("model 3"). One sentence carries at most one new link. When no
  sentence holds such an anchor verbatim, the closest partial phrase is **widened** to a title phrase
  ("home lift" → "home lift model 3"); the plan marks these `anchor_mode: widen` with `replaces` and
  `suggested_sentence`, and both the original part (in the sentence) and the new phrase (in the target's
  title/H1) are verified claims.

**Anchors (first real run).**
* *Failed (2nd version):* any title word could be the anchor, including single words. Real anchors
  were "competition", "6 Best", "Software", "Worth", and some sat in the source page's own H1.
* *Now:* ≥ 2 real words, at least one of them a distinctive (non-template) title word of the target.
  The sentence must end like a sentence and must not be the source's title/H1. Relevance floor raised
  0.30 → 0.40.

**Anchors (synthetic).**
* *Failed:* anchors from top TF-IDF bigrams of the target. They produced unnatural anchors such as
  "pit fewer" and "because price".
* *Now:* the anchor must be an exact n-gram of the sentence that matches the target's title, H1 or an
  existing anchor, and it must include a title/H1 word. Sentences that already hold a link are skipped.

## Part E — Claims & agent

* *Failed:* our own template prose wrote "301-redirect". The prose linter rejected it, because digits
  outside claim references are not allowed. Templates were reworded ("redirect permanently").
* *Failed (first real run):* a short "facts only as [Cn]" instruction. Claude Haiku still wrote "301",
  "200" and "27 internal links" into prose, which caused **16 regenerations** in one run and several
  template fallbacks. *Now:* numbered hard rules plus a good/bad example in the system prompt
  ("never write a digit; say 'permanent redirect' and cite the claim"). Group/gap names (G1, T2) are
  allowed in prose, because they are names, not facts.
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
