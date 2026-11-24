"""Zip the small, useful files of every run folder (no crawl database, no page HTML).

usage: python tools/collect_results.py RUNS_DIR OUT.zip
"""
import json
import sys
import zipfile
from pathlib import Path

KEEP = ["logs/run_stats.json", "logs/run.log", "logs/rejections.jsonl",
        "report/crawl_summary.json", "report/analysis_summary.json", "report/action_plan.md",
        "report/link_plan.json", "report/gaps.json", "report/change_report.json"]


def main():
    runs, out = Path(sys.argv[1]), Path(sys.argv[2])
    summary = {}
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for run in sorted(p for p in runs.iterdir() if p.is_dir()):
            for rel in KEEP:
                f = run / rel
                if f.exists():
                    z.write(f, f"{run.name}/{rel}")
            st = run / "logs" / "run_stats.json"
            if st.exists():
                s = json.loads(st.read_text(encoding="utf-8"))
                summary[run.name] = {k: s.get(k) for k in ("total_seconds", "peak_ram_mb", "actions", "claims")}
                summary[run.name]["llm_cost"] = (s.get("llm") or {}).get("cost_usd")
            else:
                summary[run.name] = "no run_stats.json (run failed early - see run.log)"
        fl = runs / "failures.txt"
        if fl.exists():
            z.write(fl, "failures.txt")
        z.writestr("summary.json", json.dumps(summary, indent=1))
    print(json.dumps(summary, indent=1))
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
