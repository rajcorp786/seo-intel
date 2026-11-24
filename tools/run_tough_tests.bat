@echo off
REM ==========================================================================
REM  seo-intel tough-site test batch (Windows CMD)
REM  Runs every pair below one after another, 20 minutes each, then zips all
REM  results into tough-results.zip. Set ANTHROPIC_API_KEY first if you want
REM  the AI writer; without it the tool still runs (template prose).
REM  Usage:   tools\run_tough_tests.bat
REM ==========================================================================
setlocal
cd /d %~dp0..
if not exist tough-runs mkdir tough-runs

REM name                target                      competitor                     what it stresses
call :run news   techcrunch.com             venturebeat.com               "big WordPress news: date archives, tags, pagination, scale"
call :run spa    vercel.com                 netlify.com                   "JavaScript-heavy Next.js sites: render decisions"
call :run hindi  amarujala.com              livehindustan.com             "Hindi Devanagari news: tokenizer, bot protection"
call :run i18n   duolingo.com               babbel.com                    "many languages: hreflang, locale redirects"
call :run docs   docs.djangoproject.com     flask.palletsprojects.com     "subdomain scope, versioned docs = near-duplicates"
call :run shop   pepperfry.com              urbanladder.com               "non-Shopify e-commerce: facets, product + reviews"
call :run health healthline.com             medicalnewstoday.com          "huge content sites: heavy cannibalization"

python tools\collect_results.py tough-runs tough-results.zip
echo.
echo Done. Send tough-results.zip
goto :eof

:run
echo.
echo ==== %1: %2 vs %3  (%~4) ====
seo-intel %2 --competitor %3 --out tough-runs\%1 --time-budget 20 --fresh
if errorlevel 1 echo [%1] FAILED with errorlevel %errorlevel% >> tough-runs\failures.txt
goto :eof
