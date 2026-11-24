@echo off
REM Builds label_kit.html from your finished runs and opens it in the browser.
REM Usage: tools\make_label_kit.bat run-seo-r04 run-shop-r05
cd /d %~dp0..
if "%~1"=="" (
  echo Usage: tools\make_label_kit.bat RUN_FOLDER [RUN_FOLDER ...]
  goto :eof
)
seo-intel-eval label-kit %* --html label_kit.html
start "" label_kit.html
echo.
echo Label in the browser, then click the 3 download buttons.
echo The files land in your Downloads folder: gold.json, labels.csv, pair_labels.csv
echo Then run: tools\compute_label_scores.bat %*
