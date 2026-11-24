"""End-to-end on the synthetic dev sites (slow, ~2 min). Run with:  SEO_INTEL_E2E=1 pytest tests/test_e2e.py
Needs '127.0.0.1 target.test rival.test' in your hosts file."""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(os.environ.get("SEO_INTEL_E2E") != "1", reason="set SEO_INTEL_E2E=1")


def run(out):
    env = dict(os.environ, NO_PROXY="target.test,rival.test", no_proxy="target.test,rival.test")
    subprocess.run([sys.executable, "-m", "seo_intel.cli", "http://target.test:8001/", "--competitor",
                    "http://rival.test:8002/", "--out", str(out), "--no-llm", "--fresh"], check=True, env=env, cwd=ROOT)


def test_e2e(tmp_path):
    srv = subprocess.Popen([sys.executable, str(ROOT / "devsite" / "server.py")])
    time.sleep(2)
    try:
        run(tmp_path / "a")
        run(tmp_path / "b")
        r = subprocess.run([sys.executable, "-m", "seo_intel.evalcli", "determinism", str(tmp_path / "a"),
                            str(tmp_path / "b")], capture_output=True, text=True)
        assert json.loads(r.stdout)["identical"]
        cs = json.loads((tmp_path / "a" / "report" / "crawl_summary.json").read_text())["target"]
        kinds = {t["kind"] for t in cs["traps"]}
        assert {"calendar", "facet", "path", "session"} <= kinds
        assert cs["trap_budget_share"] <= 0.05
        assert cs["redirect_chains"] and cs["redirect_loops"] and cs["meta_refresh"] and cs["js_redirects"]
        assert {c["issue"] for c in cs["canonical_issues"]} >= {"canonical chain", "canonical points to a redirect",
                                                                  "canonical points to a 404/410"}
        assert cs["rendered_share"] <= 0.15
        assert "429" not in cs["pages_by_status"], cs["pages_by_status"]  # back-off + retry recovered them
        gaps = json.loads((tmp_path / "a" / "report" / "gaps.json").read_text())["gaps"]
        assert len(gaps) >= 2
    finally:
        srv.terminate()
