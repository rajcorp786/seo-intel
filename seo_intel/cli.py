"""seo-intel <domain> --competitor <domain>"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import psutil

from .store import Store


@dataclass
class Config:
    target: str
    competitor: str
    out: str
    seed: int = 42
    max_pages: int = 50000
    time_budget_min: float = 90.0
    crawl_share: float = 0.78
    concurrency: int = 16
    rate: float = 6.0
    k: int = 40
    priority_urls: list = field(default_factory=list)
    render: bool = True
    use_llm: bool = True
    workers: int = 0


class RamMonitor(threading.Thread):
    """Peak RSS of this process + all children (headless browser, worker pool)."""

    def __init__(self):
        super().__init__(daemon=True)
        self.peak = 0
        self.stop = False

    def run(self):
        me = psutil.Process()
        while not self.stop:
            try:
                rss = me.memory_info().rss
                for c in me.children(recursive=True):
                    try:
                        rss += c.memory_info().rss
                    except psutil.Error:
                        pass
                self.peak = max(self.peak, rss)
            except psutil.Error:
                pass
            time.sleep(0.5)


def make_logger(path):
    f = open(path, "a", encoding="utf-8")

    def log(msg):
        line = f"{time.strftime('%H:%M:%S')} {msg}"
        try:
            print(line, flush=True)
        except UnicodeEncodeError:  # e.g. Windows CMD code page and Hindi text
            print(line.encode("ascii", "replace").decode("ascii"), flush=True)
        f.write(line + "\n")
        f.flush()
    return log


async def crawl_both(store, cfg, log, deadline, resume_run=False):
    from .crawler import SiteCrawl
    from .fetcher import Fetcher, HostLimiter
    from .render import Renderer
    limiter = HostLimiter(cfg.rate)
    fetcher = Fetcher(limiter, concurrency=cfg.concurrency * 2 + 8)
    crawls = {}

    def robots_allowed(url):
        for c in crawls.values():
            if c.same_site(url):
                return c.allowed_sync(url)
        return True  # third-party sub-resource: allowed unless its robots says otherwise (fetched lazily)

    renderer = Renderer(fetcher, robots_allowed, pages=2)
    if not cfg.render:
        renderer.available = False
    else:
        await renderer.start()
        if not renderer.available:
            log("WARNING: headless browser not available, JavaScript pages will not be rendered. "
                f"Fix: python -m playwright install chromium   ({renderer.error})")
    for role, dom in (("target", cfg.target), ("competitor", cfg.competitor)):
        crawls[role] = SiteCrawl(store, role, dom, cfg, fetcher, renderer, log)
    try:
        await asyncio.gather(*(c.run_crawl(deadline, resume_run) for c in crawls.values()))
    finally:
        await renderer.close()
        await fetcher.close()
    return {"requests_per_host": dict(sorted(limiter.total.items())),
            "peak_requests_in_any_1s_window": dict(sorted(limiter.peak.items())),
            "renderer_error": renderer.error}


def main(argv=None):
    ap = argparse.ArgumentParser(prog="seo-intel", description="Crawl a site and a competitor and build a "
                                 "claim-verified SEO action plan.")
    ap.add_argument("domain")
    ap.add_argument("--competitor", required=True)
    ap.add_argument("--out", default=None, help="output folder (default: ./seo-intel-<domain>)")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--max-pages", type=int, default=50000)
    ap.add_argument("--time-budget", type=float, default=90.0, help="minutes for the whole run")
    ap.add_argument("--priority-urls", default=None, help="text file, one URL per line (Part D)")
    ap.add_argument("--k", type=int, default=40, help="number of new internal links (Part D)")
    ap.add_argument("--fresh", action="store_true", help="delete previous crawl data in --out first")
    ap.add_argument("--no-render", action="store_true")
    ap.add_argument("--no-llm", action="store_true")
    ap.add_argument("--rate", type=float, default=6.0, help="max requests/second per host (<= 6)")
    ap.add_argument("--skip-crawl", action="store_true", help="re-run analysis on the existing crawl only")
    a = ap.parse_args(argv)
    out = Path(a.out or f"seo-intel-{a.domain.replace('/', '_').replace(':', '_')}")
    if a.fresh and out.exists():
        shutil.rmtree(out)
    (out / "logs").mkdir(parents=True, exist_ok=True)
    pri = []
    if a.priority_urls:
        pri = [l.strip() for l in Path(a.priority_urls).read_text(encoding="utf-8").splitlines()
               if l.strip() and not l.startswith("#")]
    cfg = Config(target=a.domain, competitor=a.competitor, out=str(out), seed=a.seed, max_pages=a.max_pages,
                 time_budget_min=a.time_budget, k=a.k, priority_urls=pri, render=not a.no_render,
                 use_llm=not a.no_llm, rate=min(6.0, a.rate))
    log = make_logger(out / "logs" / "run.log")
    ram = RamMonitor()
    ram.start()
    t0 = time.monotonic()
    budget = cfg.time_budget_min * 60
    store = Store(out / "crawl.db")
    first_run = store.get_meta("first_run_seconds:target") is None
    crawl_deadline = t0 + budget * cfg.crawl_share
    timings = {}
    log(f"seo-intel: target={cfg.target} competitor={cfg.competitor} out={out}")
    resume_run = store.get_meta("run_state") == "running"
    if resume_run:
        log("previous run was interrupted: resuming")
    store.set_meta("run_state", "running")
    net = {} if a.skip_crawl else asyncio.run(crawl_both(store, cfg, log, crawl_deadline, resume_run))
    timings["crawl_s"] = round(time.monotonic() - t0, 1)

    from .analysis import run_all
    from .llm import LLM
    llm = LLM(seed=cfg.seed, enabled=cfg.use_llm, log_path=out / "logs" / "llm_calls.jsonl")
    t1 = time.monotonic()
    ctx = run_all(store, cfg, log, llm)
    timings["analysis_s"] = round(time.monotonic() - t1, 1)
    t2 = time.monotonic()
    from .agent import Agent
    agent = Agent(ctx, log)
    actions, claims, rejections, trace = agent.run()
    timings["agent_s"] = round(time.monotonic() - t2, 1)
    from .report import write_all
    write_all(ctx, out, actions, claims, rejections)
    total = time.monotonic() - t0
    if first_run:
        for s in ("target", "competitor"):
            store.set_meta(f"first_run_seconds:{s}", total)
    store.set_meta("run_state", "complete")
    ram.stop = True
    stats = {"total_seconds": round(total, 1), "timings": timings, "peak_ram_mb": round(ram.peak / 2 ** 20, 1),
             "llm": llm.stats(), "network": net, "first_run": first_run,
             "modes": {s: store.get_meta(f"mode:{s}") for s in ("target", "competitor")},
             "actions": len(actions), "claims": len(claims), "rejected_claims": len(rejections),
             "agent_trace": trace}
    (out / "logs" / "run_stats.json").write_text(json.dumps(stats, indent=1, sort_keys=True), encoding="utf-8")
    log(f"done in {total:.0f}s, peak RAM {stats['peak_ram_mb']} MB, LLM ${llm.cost:.4f}, "
        f"{len(actions)} actions -> {out / 'report'}")
    store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
