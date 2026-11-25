# Contributing to seo-intel

## Set up
```
git clone https://github.com/rajcorp786/seo-intel.git
cd seo-intel
python -m venv .venv
.venv\Scripts\activate            (Mac/Linux: source .venv/bin/activate)
python -m pip install -e . && python -m playwright install chromium
```
`-e` installs in editable mode: code changes take effect without reinstalling.

## Before you open a pull request
1. `python -m pytest tests`: all unit tests and the 23 Google robots.txt tests must pass.
2. If you touched the crawler, the analysis or the report, also run the end-to-end test. It starts the
   synthetic sites, runs the tool twice and checks the two outputs are byte-identical:
   `set SEO_INTEL_E2E=1` (Mac/Linux: `export SEO_INTEL_E2E=1`), then `python -m pytest tests/test_e2e.py`
   (~2–4 min).
   For anything that touches fetching, rendering or extraction, also run the live-site randomness test:
   `set SEO_INTEL_CHAOS=1` together with `SEO_INTEL_E2E=1` (~7 min for both tests).
   If you touched fetching, rendering or extraction, also run the live-site randomness test: set
   `SEO_INTEL_CHAOS=1` as well as `SEO_INTEL_E2E=1` (~7 min for both end-to-end tests).
3. If you touched `agent.py`, `claims.py` or `report.py`: run `python devsite/mock_agent_check.py RUN_DIR`
   on a finished run. It must print `MOCK AGENT CHECK PASSED` (no invented fact reaches the plan).
4. Add a line to `CHANGELOG.md`. If you replaced an approach, add the failed version and its numbers to
   `DECISIONS.md`.

## Rules the code keeps
* **Same input, same output.** Never let timing decide what is crawled or written: sort before output,
  use fixed seeds, keep timings in `logs/` only.
* **Every fact in the plan is a claim the code can re-check.** New facts need a claim kind in `claims.py`
  (resolver) and `report.py` (rendering).
* **Polite crawling.** At most 6 requests/second per host, robots.txt as Google reads it, back off on 429/503.
* **No made-up numbers** in BENCHMARKS.md: write "not measured" until a run produced the number.

## Layout
| Folder | What is in it |
|---|---|
| `seo_intel/` | the tool (crawler, extraction, analysis, link plan, agent, report) |
| `tests/` | unit, robots and end-to-end tests |
| `devsite/` | synthetic target/competitor sites and checks used by the tests |
| `tools/` | Windows helper scripts (big run, labelling kit, result collector) |

## Reporting a bug
Open an issue with the command you ran, `seo-intel --version`, and `logs/run.log` plus
`logs/run_stats.json` from the run folder. Remove any API key before pasting.
