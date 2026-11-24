@echo off
REM Full-size check: one 90-minute run on a large site pair, then (next day) the incremental re-run.
REM Day 1:  tools\big_run.bat
REM Day 2:  tools\big_run.bat   (same command; it finds the previous crawl and runs incrementally)
cd /d %~dp0..
seo-intel techcrunch.com --competitor venturebeat.com --out big-run --time-budget 90
echo.
echo Send big-run\logs\run_stats.json and big-run\logs\run.log
