# Changelog

`seo-intel --version` prints the installed version. Every run logs it in `logs/run.log` and `logs/run_stats.json`.

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
