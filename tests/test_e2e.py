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
        assert json.loads((tmp_path / "a" / "report" / "crawl_summary.json").read_text())["target"]["html_pages"] >= 100
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


@pytest.mark.skipif(os.environ.get("SEO_INTEL_CHAOS") != "1", reason="set SEO_INTEL_CHAOS=1 (slow, ~6 min)")
def test_e2e_live_site_randomness(tmp_path):
    """Like a live site: 4% 502s / dropped connections, per-request nonces and clocks, CSS variants, a shuffled
    widget and a JS widget with a random delay. Two fresh runs must still give byte-identical reports, and the
    stored main text of every page must be the same."""
    srv = subprocess.Popen([sys.executable, str(ROOT / "devsite" / "server.py"), "--chaos", "--jitter"])
    time.sleep(2)
    try:
        run(tmp_path / "a")
        run(tmp_path / "b")
        r = subprocess.run([sys.executable, "-m", "seo_intel.evalcli", "determinism", str(tmp_path / "a"),
                            str(tmp_path / "b")], capture_output=True, text=True)
        assert json.loads(r.stdout)["identical"], r.stdout
        d = subprocess.run([sys.executable, "-m", "seo_intel.evalcli", "diff-runs", str(tmp_path / "a"),
                            str(tmp_path / "b")], capture_output=True, text=True)
        diff = json.loads(d.stdout)
        for site in ("target", "competitor"):
            assert diff[site]["main_text_changed"] == 0 and diff[site]["status_changed"] == 0, diff[site]
        for site, cs in json.loads((tmp_path / "a" / "report" / "crawl_summary.json").read_text()).items():
            assert cs["rendered_share"] <= 0.15
            assert cs["html_pages"] >= 40, (site, cs["html_pages"])  # an empty crawl is not "repeatable"
    finally:
        srv.terminate()


def test_render_crowd():
    """36 renders queued on 2 browser pages while the site drops connections and hangs scripts: every render
    of a page must succeed and give the same result (queue time must not eat the render's time limit)."""
    import asyncio
    from seo_intel import parse as P
    from seo_intel.fetcher import Fetcher, HostLimiter, decode
    from seo_intel.render import Renderer, token_gain
    os.environ["NO_PROXY"] = os.environ["no_proxy"] = "target.test,rival.test"
    srv = subprocess.Popen([sys.executable, str(ROOT / "devsite" / "server.py"), "--chaos", "--jitter"])
    time.sleep(2)

    async def main():
        f = Fetcher(HostLimiter(6))
        r = Renderer(f, lambda u: True, pages=2)
        urls = [f"http://target.test:8001/app/feature-{i}/" for i in range(8)] + ["http://target.test:8001/js-moved"]
        res = {}

        async def one(u):
            for _ in range(6):
                resp = await f.get_once(u)
                if resp.get("status") == 200:
                    break
            html = decode(resp["body"], resp.get("encoding"))
            out, nav = await r.render_sure(u, html)
            g = round(token_gain(P.visible_text(P.to_tree(html)), P.visible_text(P.to_tree(out))), 3) if out else None
            res.setdefault(u, []).append((g, nav))
        await asyncio.gather(*(one(u) for _ in range(4) for u in urls))
        out, nav = await r.render_sure("http://target.test:8001/zz", "<html><head><script>location.replace('/blog/')"
                                       "</script></head><body><p>hello world</p></body></html>")
        await r.close()
        await f.close()
        return res, bool(out), nav
    try:
        res, ok, nav = asyncio.run(main())
    finally:
        srv.terminate()
    assert ok and nav and nav.endswith("/blog/")  # a redirect fired while loading is recorded, not a failure
    for u, v in res.items():
        assert len(set(v)) == 1 and v[0][0] is not None, (u, v)
