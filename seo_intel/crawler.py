"""Site crawler: robots, sitemaps, traps, redirects, selective rendering, incremental."""
from __future__ import annotations

import asyncio
import hashlib
import heapq
import json
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

from . import parse as P
from .fetcher import decode
from .render import RENDER_SHARE, RenderPolicy, token_gain
from .robots import Robots
from .sitemap import discover
from .traps import TrapDetector
from .urlnorm import host_of, is_asset, netloc_of, normalize, reg_domain


def sha(s) -> str:
    if isinstance(s, str):
        s = s.encode("utf-8", "replace")
    return hashlib.sha1(s).hexdigest()


def _parse_date(s):
    if not s:
        return None
    try:
        if "T" in s or "-" in s:
            d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        else:
            d = parsedate_to_datetime(s)
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d.timestamp()
    except (ValueError, TypeError):
        return None


class SiteCrawl:
    def __init__(self, store, role, domain, cfg, fetcher, renderer, log):
        self.store, self.role, self.cfg, self.f, self.renderer, self.log = store, role, cfg, fetcher, renderer, log
        self.start_url = domain if "://" in domain else f"https://{domain.strip('/')}/"
        self.start_url = normalize(self.start_url)[0]
        self.domain = reg_domain(host_of(self.start_url))
        self.robots = {}
        self.traps = TrapDetector()
        self.policy = RenderPolicy()
        self.render_q = []
        self.render_seq = 0
        self.inflight = set()
        self.n_html = self.n_rendered = self.n_requests_pages = 0
        self.trap_fetches = 0
        self.trap_avoided = 0
        self.run = 0
        self.mode = "fresh"

    # ------------------------------------------------------------ helpers
    def same_site(self, url):
        h = host_of(url)
        return h == self.domain or h.endswith("." + self.domain)

    async def robots_for(self, url):
        host = netloc_of(url)
        if host in self.robots:
            return self.robots[host]
        self.robots[host] = None  # placeholder while fetching
        for attempt in range(3):
            r = await self.f.fetch(f"{host}/robots.txt", any_body=True)
            st = r.get("status")
            if st is not None:
                break
            await asyncio.sleep(2 * (attempt + 1))
        if st == "offsite" or st == "loop" or st == "too_many_redirects":
            st = 404  # Google: unresolvable redirect chains => treat as not found
        rb = Robots(r.get("body") or b"", st if isinstance(st, int) else None, agent="seo-intel")
        self.robots[host] = rb
        self.store.stat(self.role, f"robots_status:{host}", st if isinstance(st, int) else -1)
        return rb

    def allowed_sync(self, url):
        rb = self.robots.get(netloc_of(url))
        return True if rb is None else rb.allowed(url)

    def enqueue(self, items, src):
        """items: [(url, depth, priority, lastmod)] already normalized."""
        rows = []
        for u, d, pr, lm in items:
            if not u or not self.same_site(u) or is_asset(u):
                continue
            trap = self.traps.check(u, d)
            if trap:
                self.store.add_trap(self.role, trap[0], trap[1], u)
                self.trap_avoided += 1
                continue
            rows.append((u, self.role, host_of(u), d, pr, src, lm))
        if rows:
            self.store.add_urls(rows, self.run)

    def deadline_ok(self):
        return time.monotonic() < self.deadline

    # ------------------------------------------------------------ main
    async def run_crawl(self, deadline, resume_run=False):
        self.deadline = deadline
        st = self.store
        state = st.get_meta(f"state:{self.role}")
        if resume_run and state == "complete":
            self.log(f"[{self.role}] crawl already complete before the interruption - keeping it")
            return
        self.run = st.get_meta(f"run:{self.role}", 0)
        if state == "running":
            self.mode = "resume"
        elif state == "complete":
            self.mode = "incremental"
            self.run += 1
            st.snapshot_previous(self.role)
            first = st.get_meta(f"first_run_seconds:{self.role}")
            if first:
                self.deadline = min(self.deadline, time.monotonic() + max(30.0, 0.08 * first))
        else:
            self.run = 1
        st.set_meta(f"run:{self.role}", self.run)
        st.set_meta(f"state:{self.role}", "running")
        st.set_meta(f"mode:{self.role}", self.mode)
        st.set_meta(f"start_url:{self.role}", self.start_url)
        t0 = time.monotonic()
        self.log(f"[{self.role}] {self.mode} crawl of {self.start_url} (run {self.run})")

        if self.mode == "resume":
            self.replay_traps()
            self.restore_render_queue()
        start_host = netloc_of(self.start_url)
        await self.robots_for(self.start_url)

        if self.mode != "resume" or not st.get_meta(f"seeded:{self.role}:{self.run}"):
            self.enqueue([(self.start_url, 0, 1000.0, None)], "start")
            rb = self.robots[start_host]
            sms = list(rb.sitemaps) + [f"{self.start_url.rstrip('/')}/sitemap.xml"]
            sms = [normalize(s, self.start_url)[0] for s in sms]
            sms = [s for s in sms if s and self.same_site(s)]
            sm_urls, files = await discover(self.f, sms, self.cfg.max_pages * 2, self.deadline_ok)
            items = []
            for loc, lm in sm_urls:
                u, _ = normalize(loc)
                if u:
                    items.append((u, 1, 60.0, lm))
            self.enqueue(items, "sitemap")
            st.stat(self.role, "sitemap_files", files)
            st.stat(self.role, "sitemap_urls", len(items))
            await self.probe_404()
            st.set_meta(f"seeded:{self.role}:{self.run}", True)

        t_seed = time.monotonic()
        await self.frontier_loop()
        t_front = time.monotonic()
        await self.drain_renders(final=True)
        await self.template_probe_phase()
        self.log(f"[{self.role}] phases: seed {t_seed - t0:.1f}s, frontier {t_front - t_seed:.1f}s, "
                 f"render {time.monotonic() - t_front:.1f}s")

        if self.mode == "incremental":
            st.drop_unseen(self.role, self.run)
        elapsed = time.monotonic() - t0
        prev_elapsed = st.get_meta(f"crawl_seconds:{self.role}", 0) if self.mode == "resume" else 0
        st.set_meta(f"crawl_seconds:{self.role}", prev_elapsed + elapsed)
        self._save_stats()
        st.set_meta(f"state:{self.role}", "complete")
        self.log(f"[{self.role}] done: {self.n_html} html pages, {self.n_rendered} rendered, "
                 f"{self.trap_avoided} trap URLs avoided, {elapsed:.0f}s")

    def restore_render_queue(self):
        for u, rr in self.store.db.execute("SELECT url, render_reason FROM pages WHERE site=? AND rendered=0 AND "
                                           "render_reason LIKE 'pending:%' ORDER BY url", (self.role,)):
            _, name, pr = rr.split(":")
            self.render_seq += 1
            heapq.heappush(self.render_q, (-int(pr), u, self.render_seq, name))
        self.n_html = self.store.count("SELECT COUNT(*) FROM pages WHERE site=? AND status=200 AND html IS NOT NULL "
                                       "AND run=?", (self.role, self.run))
        self.n_rendered = self.store.count("SELECT COUNT(*) FROM pages WHERE site=? AND rendered=1 AND run=?",
                                           (self.role, self.run))

    def replay_traps(self):
        """Rebuild trap-detector memory after a crash (same decisions as an uninterrupted run)."""
        for u, d in self.store.db.execute("SELECT url, depth FROM urls WHERE site=? ORDER BY depth, url", (self.role,)):
            self.traps.check(u, d)
        for u, h in self.store.db.execute("SELECT url, vhash FROM pages WHERE site=? AND vhash IS NOT NULL "
                                          "ORDER BY fetched_at, url", (self.role,)):
            self.traps.observe(u, h)
        for u, h in self.store.db.execute("SELECT url, depth FROM urls WHERE site=? AND state='queued' ORDER BY url",
                                          (self.role,)):
            t = self.traps.check(u, h)
            if t:
                self.store.mark(u, "trap", t[0])

    def _save_stats(self):
        st = self.store
        add = lambda k, v: st.stat(self.role, k, st.get_stat(self.role, k) + v if self.mode == "resume" else v)
        st.stat(self.role, "html_pages_fetched", st.count(
            "SELECT COUNT(*) FROM pages WHERE site=? AND status=200 AND html IS NOT NULL AND run=?", (self.role, self.run)))
        st.stat(self.role, "rendered", st.count("SELECT COUNT(*) FROM pages WHERE site=? AND rendered=1", (self.role,)))
        add("page_requests", self.n_requests_pages)
        add("trap_fetches", self.trap_fetches)
        add("trap_urls_avoided", self.trap_avoided)
        st.stat(self.role, "render_available", 1 if self.renderer.available else 0)

    async def probe_404(self):
        """Fetch a deterministic non-existent URL; if it returns 200, keep its fingerprint."""
        probe = f"{self.start_url.rstrip('/')}/seo-intel-probe-{sha(self.domain)[:10]}-does-not-exist"
        if not self.allowed_sync(probe):
            return
        r = await self.f.fetch(probe, same_site=self.same_site)
        st = r.get("status")
        self.store.set_meta(f"probe404:{self.role}", {"url": probe, "status": st if isinstance(st, int) else str(st),
                                                    "html": decode(r.get("body") or b"", r.get("encoding"))[:300000]
                                                    if st == 200 else None})

    async def frontier_loop(self):
        st = self.store
        workers = self.cfg.concurrency
        pending = set()
        render_task = None
        while True:
            done_pages = st.count("SELECT COUNT(*) FROM pages WHERE site=? AND run=?", (self.role, self.run))
            if not self.deadline_ok() or done_pages + len(pending) >= self.cfg.max_pages:
                break
            room = workers - len(pending)
            batch = st.next_batch(self.role, room, self.run, self.inflight) if room > 0 else []
            for row in batch:
                self.inflight.add(row[0])
                pending.add(asyncio.ensure_future(self.process(row[0], row[1], row[3])))
            if render_task is None or render_task.done():
                if self.render_q and self.render_budget() > 0:
                    render_task = asyncio.ensure_future(self.drain_renders())
            if not pending:
                if render_task and not render_task.done():
                    await render_task
                    continue
                break
            done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED,
                                               timeout=max(0.1, self.deadline - time.monotonic()))
            for d in done:
                exc = d.exception()
                if exc:
                    self.log(f"[{self.role}] worker error: {type(exc).__name__}: {exc}")
        if pending:
            # deadline: cancel in-flight; their URLs stay 'queued' => no lost work on resume
            for p in pending:
                p.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
        if render_task and not render_task.done():
            await asyncio.gather(render_task, return_exceptions=True)

    # ------------------------------------------------------------ one URL
    async def process(self, url, depth, lastmod):
        st = self.store
        try:
            host = netloc_of(url)
            rb = await self.robots_for(url)
            while rb is None:
                await asyncio.sleep(0.05)
                rb = self.robots.get(host)
            if not rb.allowed(url):
                st.mark(url, "blocked", "robots.txt")
                return
            trap = self.traps.check(url)
            if trap:
                st.add_trap(self.role, trap[0], trap[1], url)
                st.mark(url, "trap", trap[0])
                self.trap_avoided += 1
                return
            prev = st.prev(url) if self.mode == "incremental" else None
            if prev is not None and self.can_skip(prev, depth, lastmod):
                self.carry_over(url, depth)
                return
            cond = {}
            if prev is not None:
                if prev["etag"]:
                    cond["If-None-Match"] = prev["etag"]
                if prev["last_modified"]:
                    cond["If-Modified-Since"] = prev["last_modified"]
            r = await self.f.fetch(url, cond_headers=cond, same_site=self.same_site)
            self.n_requests_pages += 1 + len(r.get("chain", []))
            if r.get("status") == 304 and prev is not None:
                self.carry_over(url, depth)
                st.update_page(url, etag=r["headers"].get("etag") or prev["etag"])
                return
            await self.handle_response(url, depth, lastmod, r)
        finally:
            self.inflight.discard(url)

    def can_skip(self, prev, depth, lastmod):
        if prev["status"] != 200:
            return False
        if depth is not None and depth <= 1:
            return False
        lm = _parse_date(lastmod)
        if lm is not None:
            row = self.store.db.execute("SELECT fetched_at FROM pages WHERE url=?", (prev["url"],)).fetchone()
            return bool(row and row[0] and lm <= row[0])
        return True

    def carry_over(self, url, depth):
        st = self.store
        st.db.execute("BEGIN")
        st.db.execute("UPDATE pages SET run=?, unchanged=1 WHERE url=?", (self.run, url))
        st.db.execute("UPDATE urls SET state='done' WHERE url=?", (url,))
        st.db.execute("COMMIT")
        dsts = [r[0] for r in st.db.execute("SELECT DISTINCT dst FROM links WHERE src=? ORDER BY dst", (url,))]
        self.enqueue([(d, (depth or 0) + 1, 100.0 - 10 * ((depth or 0) + 1), None) for d in dsts], url)

    async def handle_response(self, url, depth, lastmod, r):
        st = self.store
        status = r.get("status")
        chain = r.get("chain", [])
        final = r.get("final_url", url)
        headers = r.get("headers", {})
        row = {"url": url, "site": self.role, "status": None, "final_url": final,
               "chain": json.dumps(chain), "ctype": headers.get("content-type"),
               "etag": headers.get("etag"), "last_modified": headers.get("last-modified"),
               "x_robots": headers.get("x-robots-tag"), "depth": depth, "fetched_at": int(time.time()),
               "run": self.run, "lastmod": lastmod, "unchanged": 0}
        if chain:
            row["status"] = chain[0][1]
            row["html"] = None
            st.save_page(row, [])
            if isinstance(status, int) and final != url and self.same_site(final):
                # the redirect target response is already in hand: store it once
                known = st.db.execute("SELECT state FROM urls WHERE url=?", (final,)).fetchone()
                self.enqueue([(final, depth, 90.0, None)], url)
                if known is None or known[0] == "queued":
                    ok = st.db.execute("SELECT 1 FROM urls WHERE url=?", (final,)).fetchone()
                    if ok and self.allowed_sync(final) and final not in self.inflight:
                        self.inflight.add(final)
                        try:
                            r2 = dict(r)
                            r2["chain"] = []
                            await self.handle_response(final, depth, None, r2)
                        finally:
                            self.inflight.discard(final)
            return
        row["status"] = status if isinstance(status, int) else None
        if status is None or not isinstance(status, int):
            row["status"] = -1
            row["render_reason"] = str(r.get("error") or status)
            st.save_page(row, [])
            return
        ctype = (headers.get("content-type") or "").lower()
        if status != 200 or ("html" not in ctype and ctype):
            row["html"] = None
            st.save_page(row, [])
            return
        html = decode(r["body"], r.get("encoding"))
        self.n_html += 1
        prev = st.prev(url) if self.mode == "incremental" else None
        row["raw_hash"] = sha(html)
        if prev is not None and prev["raw_hash"] == row["raw_hash"]:
            row["unchanged"] = 1
        fields, _ = P.parse_page(html, url)
        links = self.apply_fields(row, fields, url)
        row["html"] = html
        row["scripts"] = fields["signals"]["scripts"]
        reason = self.policy.reason(url, fields["signals"]) if self.renderer.available else None
        st.save_page(row, links)
        self.enqueue_links(url, depth, links, row)
        tpl_hash = sha(P.visible_text(P.to_tree(html))[:20000]) if fields["signals"]["text_len"] else row["raw_hash"]
        st.db.execute("UPDATE pages SET vhash=? WHERE url=?", (tpl_hash, url))
        trap = self.traps.observe(url, tpl_hash)
        if trap:
            st.add_trap(self.role, trap[0], trap[1], url)
            self.trap_fetches += max(0, trap[2] - 1)
        if reason:
            name, pr = reason
            st.db.execute("UPDATE pages SET render_reason=? WHERE url=?", (f"pending:{name}:{pr}", url))
            self.render_seq += 1
            heapq.heappush(self.render_q, (-pr, url, self.render_seq, name))

    def apply_fields(self, row, fields, url):
        row["title"] = fields["title"]
        row["h1"] = fields["h1"]
        row["meta_robots"] = fields["meta_robots"]
        row["lang"] = fields["lang"]
        row["canonical"] = normalize(fields["canonical"], url)[0] if fields["canonical"] else None
        row["meta_refresh"] = normalize(fields["meta_refresh"], url)[0] if fields["meta_refresh"] else None
        row["js_redirect"] = normalize(fields["js_redirect"], url)[0] if fields["js_redirect"] else None
        row["hreflang"] = json.dumps([(l, normalize(h, url)[0]) for l, h in fields["hreflang"]])
        links = []
        for i, (href, anchor, nofollow) in enumerate(fields["links"]):
            u, sess = normalize(href, url)
            if sess:
                self.store.add_trap(self.role, "session", f"session-id parameter '{sess}' stripped", href[:300])
            if u and not is_asset(u):
                links.append((url, u, anchor, "nofollow" if nofollow else "", i))
        return links

    def enqueue_links(self, url, depth, links, row):
        robots_meta = ((row.get("meta_robots") or "") + "," + (row.get("x_robots") or "")).lower()
        follow = "nofollow" not in robots_meta and "none" not in robots_meta
        d = (depth or 0) + 1
        items = []
        if follow:
            items = [(l[1], d, 100.0 - 10 * d, None) for l in links if l[3] != "nofollow"]
        for extra in (row.get("canonical"), row.get("meta_refresh"), row.get("js_redirect")):
            if extra:
                items.append((extra, depth or 0, 95.0, None))
        for _, h in json.loads(row.get("hreflang") or "[]"):
            if h:
                items.append((h, d, 70.0, None))
        self.enqueue(items, url)

    # ------------------------------------------------------------ rendering
    def render_budget(self):
        return int(RENDER_SHARE * self.n_html) - self.n_rendered

    async def drain_renders(self, final=False):
        while self.render_q and self.render_budget() > 0 and self.deadline_ok():
            _, url, _, reason = heapq.heappop(self.render_q)
            await self.render_one(url, reason)
            if not final:
                await asyncio.sleep(0)

    async def template_probe_phase(self):
        """Deterministic per-template probing after the crawl (see render.py)."""
        if not self.renderer.available:
            return
        from collections import defaultdict
        from .urlnorm import template
        by_tpl = defaultdict(list)
        for (u,) in self.store.db.execute(
                "SELECT url FROM pages WHERE site=? AND status=200 AND html IS NOT NULL AND rendered=0 AND scripts>0 "
                "AND run=? ORDER BY url", (self.role, self.run)):
            by_tpl[template(u)].append(u)
        from .render import PROBES_PER_TEMPLATE
        for tpl in sorted(by_tpl):
            urls = sorted(by_tpl[tpl], key=lambda u: sha(u))
            for u in urls[:PROBES_PER_TEMPLATE]:
                if self.render_budget() <= 0 or not self.deadline_ok():
                    return
                g = await self.render_one(u, "probe")
                if g is not None:
                    self.policy.learn(u, g)
            if self.policy.template_js.get(tpl):
                for u in urls[PROBES_PER_TEMPLATE:]:
                    if self.render_budget() <= 0 or not self.deadline_ok():
                        return
                    await self.render_one(u, "template_js")

    async def render_one(self, url, reason):
        """Render one stored page; update fields/links if rendering changed it. Returns token gain."""
        html = self.store.html(url)
        if not html:
            return None
        self.n_rendered += 1
        rendered, nav_target = await self.renderer.render(url, html)
        if not rendered:
            return None
        raw_vt = P.visible_text(P.to_tree(html))
        ren_vt = P.visible_text(P.to_tree(rendered))
        gain = token_gain(raw_vt, ren_vt)
        fields, _ = P.parse_page(rendered, url)
        row = {}
        links = self.apply_fields(row, fields, url)
        if nav_target:
            row["js_redirect"] = normalize(nav_target, url)[0]
        upd = {k: row[k] for k in ("title", "h1", "canonical", "meta_robots", "hreflang", "js_redirect")
               if row.get(k) is not None}
        if gain > 0.05 or nav_target:
            import zlib
            upd["html"] = zlib.compress(rendered.encode("utf-8", "replace"), 6)
        st = self.store
        st.db.execute("BEGIN")
        sets = ",".join(f"{k}=?" for k in upd) + ("," if upd else "") + "rendered=1, render_reason=?"
        st.db.execute(f"UPDATE pages SET {sets} WHERE url=?", list(upd.values()) + [f"{reason}:gain={gain:.2f}", url])
        if gain > 0.05:
            st.db.execute("DELETE FROM links WHERE src=?", (url,))
            st.db.executemany("INSERT OR IGNORE INTO links VALUES(?,?,?,?,?)", links)
        st.db.execute("COMMIT")
        if gain > 0.05:
            depth = st.db.execute("SELECT depth FROM pages WHERE url=?", (url,)).fetchone()[0]
            self.enqueue_links(url, depth, links, {**row, "x_robots": None})
        return gain
