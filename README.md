# seo-intel

`seo-intel` compares **your website** with **a competitor's website** and gives you an SEO action plan.
Every fact in the plan is checked by code against the crawl before it is written out.

## 1. Install

You need **Python 3.10 or newer** and **git**. Check with `python --version` (on Windows you can also use `py --version`).

**Windows (CMD)**
```
git clone https://github.com/rajcorp786/seo-intel.git
cd seo-intel
python -m venv .venv
.venv\Scripts\activate
python -m pip install . && python -m playwright install chromium
```

**Mac / Linux**
```
git clone https://github.com/rajcorp786/seo-intel.git
cd seo-intel
python3 -m venv .venv
source .venv/bin/activate
python -m pip install . && python -m playwright install chromium
```

Check it works: `seo-intel --version`, `python -m pytest tests` (49 tests, ~10 s).

**Quick install without cloning** (inside an activated venv):
`python -m pip install git+https://github.com/rajcorp786/seo-intel.git && python -m playwright install chromium`
(add `@v1.0.8` after `.git` for a fixed version).

* Always use `python -m pip`, not plain `pip`: on many machines `pip` is not on the PATH.
* The second half of the install line downloads the headless browser used for JavaScript pages. If you skip
  it, the tool tries to download the browser on first use. If that also fails, it prints a WARNING and
  continues without rendering.
* Linux servers without a desktop may also need the browser's system libraries:
  `python -m playwright install --with-deps chromium` (needs sudo).
* Next time, only `activate` the `.venv` again (step 4) before running.

## 2. (Optional) Turn on the AI writer

The plan is written by an AI model when an API key is set. Without a key, the tool still works
and writes the plan from built-in templates.

```
set ANTHROPIC_API_KEY=your-key          (Windows CMD)
export ANTHROPIC_API_KEY=your-key       (Mac / Linux)
```

Cost is capped at US$0.45 per run, and answers are cached, so repeating a run costs nothing.

## 3. Run it

```
seo-intel yoursite.com --competitor theirsite.com
```

Useful options:

| Option | What it does |
|---|---|
| `--out FOLDER` | where results go (default `seo-intel-yoursite.com`) |
| `--priority-urls file.txt` | pages you want to push with internal links (one URL per line) |
| `--k 40` | how many new internal links to plan |
| `--time-budget 90` | total minutes allowed (crawl stops in time, analysis always finishes) |
| `--fresh` | forget the previous crawl and start over |

**Running it again later** (for example the next day) re-uses the previous crawl. The tool only
re-downloads pages that may have changed, and it writes a change report.
**If the run is interrupted** (power cut, Ctrl+C), run the same command again and it continues where it stopped.

## 4. Read the results

Open the folder `seo-intel-yoursite.com/report/`:

| File | What's inside |
|---|---|
| `action_plan.md` | **Start here.** Top 25 actions, ranked, each with its evidence |
| `crawl_summary.json` | status codes, redirects, canonical problems, soft 404s, spider traps |
| `change_report.json` | what changed since the last run (new / removed pages, title or canonical changes, new redirects) |
| `cannibalization.json` | groups of your pages competing for the same search intent |
| `gaps.json` | topics your competitor covers in depth that you don't |
| `link_plan.json` | the exact new internal links: source page, sentence, anchor text, PageRank before/after |
| `claims.json` | every fact used in the plan, in machine-checkable form |
| `pages_target.csv` | one row per page of your site (open in Excel) |

Timing, memory and AI cost for the run are in `logs/run_stats.json`.

## 5. If something goes wrong

| Message | Fix |
|---|---|
| `'pip' is not recognized` | use `python -m pip ...` (see Install) |
| `'python' is not recognized` (Windows) | use `py` instead of `python` for the venv step, or install Python from python.org with "Add to PATH" ticked |
| `'seo-intel' is not recognized` | activate the venv first: `.venv\Scripts\activate` (Windows) / `source .venv/bin/activate` |
| `WARNING: headless browser not available` | `python -m playwright install chromium` |
| `ERROR: robots.txt ... could not be read` | the site is down or blocks bots; the tool does not crawl it (Google behaves the same) |
| Very few pages, many `429` | the site rate-limits bots; the tool backs off as Googlebot does |

## 6. Good to know

* The crawler obeys `robots.txt` the way Google reads it and never sends more than 6 requests per second to one site.
* Same input + same `--seed` gives byte-identical files in `report/`.
* Measurement commands for every target are in `BENCHMARKS.md`, design decisions are in `DECISIONS.md`, and version history is in `CHANGELOG.md`.
* `tools\run_tough_tests.bat` runs several hard real-site pairs in one go, and `tools/collect_results.py` zips their results.
* `tools\make_label_kit.bat RUN…` opens a click-through page for the human labels (main content, recommendations,
  cannibalization pairs). `tools\compute_label_scores.bat RUN…` turns those labels into F1, Cohen's kappa and the ablation table.
* `tools\big_run.bat` does a full 90-minute run on a large pair. Run it again the next day for the incremental test.
* Developers: `python -m pytest tests` runs the test suite. `python devsite/server.py` starts the synthetic test sites.

Want to change the code? See [CONTRIBUTING.md](CONTRIBUTING.md).
