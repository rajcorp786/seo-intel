@echo off
REM Computes Part B F1, Part E judge kappa and Part C ablation from your labels.
REM Labels are read from your Downloads folder. Output: label-scores.txt (send this file).
REM Usage: tools\compute_label_scores.bat run-seo-r04 run-shop-r05
cd /d %~dp0..
set DL=%USERPROFILE%\Downloads
if "%~1"=="" (
  echo Usage: tools\compute_label_scores.bat RUN_FOLDER [RUN_FOLDER ...]
  goto :eof
)
echo seo-intel label scores > label-scores.txt
for %%R in (%*) do (
  echo. >> label-scores.txt
  echo ===== Part B: main-content F1, %%R ===== >> label-scores.txt
  if exist "%DL%\gold.json" seo-intel-eval extract-f1 --out %%R --gold "%DL%\gold.json" >> label-scores.txt 2>&1
)
echo. >> label-scores.txt
echo ===== Part E: LLM judge vs your labels (Cohen's kappa) ===== >> label-scores.txt
if exist "%DL%\labels.csv" seo-intel-eval judge --out %1 --labels "%DL%\labels.csv" >> label-scores.txt 2>&1
echo. >> label-scores.txt
echo ===== Part C: ablation, precision@20 (%1) ===== >> label-scores.txt
if exist "%DL%\pair_labels.csv" seo-intel-eval ablation --out %1 --labels "%DL%\pair_labels.csv" >> label-scores.txt 2>&1
type label-scores.txt
echo.
echo Send label-scores.txt
