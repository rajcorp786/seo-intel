"""Site crawler: robots, sitemaps, traps, redirects, selective rendering, incremental."""
from __future__ import annotations

import asyncio
import hashlib
import heapq
import json
import os
import zlib
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

from . import parse as P
from .fetcher import decode
from .render import RENDER_SHARE, RenderPolicy, token_gain
from .robots import Robots
from .sitemap import discover
from .traps import TrapDetector
from .urlnorm import host_of, is_asset, netloc_of, normalize, reg_domain, site_scope


MAX_RETRIES = 4
TRANSIENT_RETRIES = 3
UNSURE_LO, UNSURE_HI = 0.10, 0.35   # render gains this close to the 0.20 line are checked twice
# pages per site = rate x crawl seconds x this; leaves room for redirects, robots, sitemaps, renders and
# back-off, so a normal site is fully crawled to the cap BEFORE the deadline (a deadline cut is timing-dependent)
PAGE_EFFICIENCY = 0.75
HUB_SHARE = 2          # % of known pages (by in-links) re-checked on every incremental run
HUB_MIN = 10
TRANCHE_FIRST = int(os.environ.get("SEO_INTEL_TRANCHE_FIRST", 200))    # the crawl grows in fixed steps (200, 300, 450 ... pages); see extend_tranche
TRANCHE_GROWTH = 2.0
RENDER_AUDIT_N = 4     # renders kept for the automatic render-recall audit
REFRESH_ROTATION = 20  # 1 in 20 other pages re-checked per incremental run (each page within ~20 runs)


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
        self.domain = site_scope(host_of(self.start_url))
        self.robots = {}
        self.traps = TrapDetector()
        self.policy = RenderPolicy()
        self.render_q = []
        self.render_seq = 0
        self.inflight = {}          # url -> depth
        self.observed = set()       # urls whose trap-sample observation is recorded
        self.retrying = set()       # urls re-queued after 429/503
        self.dispatched_urls = set()
        self.page_cap = cfg.max_pages
        self.n_html = self.n_rendered = self.n_requests_pages = 0
        self.trap_fetches = 0
        self.trap_avoided = 0
        self.run = 0
        self.mode = "fresh"
        self.refresh_set = set()
        self.retries = {}

    # ------------------------------------------------------------ helpers
    def same_site(self, url):
        h = host_of(url)
        return h == self.domain or h.endswith("." + self.domain) or h == "www." + self.domain

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
            self.refresh_set = self.pick_refresh_set()
            first = st.get_meta(f"first_run_seconds:{self.role}")
            if first:
                self.deadline = min(self.deadline, time.monotonic() + max(30.0, 0.08 * first))
        else:
            self.run = 1
        crawl_budget_s = self.cfg.time_budget_min * 60 * self.cfg.crawl_share
        self.page_cap = int(min(self.cfg.max_pages, max(50, self.cfg.rate * crawl_budget_s * PAGE_EFFICIENCY)))
        st.set_meta(f"run:{self.role}", self.run)
        st.set_meta(f"state:{self.role}", "running")
        st.set_meta(f"mode:{self.role}", self.mode)
        st.set_meta(f"start_url:{self.role}", self.start_url)
        t0 = time.monotonic()
        self.log(f"[{self.role}] {self.mode} crawl of {self.start_url} (run {self.run}, page cap {self.page_cap})")

        if self.mode == "resume":
            self.replay_traps()
            self.restore_render_queue()
        start_host = netloc_of(self.start_url)
        rb0 = await self.robots_for(self.start_url)
        if rb0.mode == "disallow_all":
            self.log(f"[{self.role}] ERROR: robots.txt of {start_host} could not be read (status "
                     f"{rb0.status if rb0.status is not None else 'network error'}). Google treats this as "
                     f"'do not crawl', so nothing will be fetched. Check that the site opens in a browser.")

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
        await self.render_audit()
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
        if self.n_html == 0:
            self.explain_empty()

    def explain_empty(self):
        """A site that gives no HTML page must say why, loudly (unreachable, robots, redirect elsewhere ...)."""
        st = self.store
        row = st.db.execute("SELECT status, final_url FROM pages WHERE url=?", (self.start_url,)).fetchone()
        blocked = st.count("SELECT COUNT(*) FROM urls WHERE site=? AND state='blocked'", (self.role,))
        if row is None:
            why = ("robots.txt blocks the start page" if blocked else
                   "the start page was never fetched (DNS / connection / TLS error, or no time left)")
        elif not isinstance(row[0], int) or row[0] < 0:
            why = "the start page could not be fetched (network error after retries)"
        elif row[0] in (301, 302, 303, 307, 308):
            why = f"the start page redirects to {row[1]}, outside this site; run again with that address"
        elif row[0] in (401, 403):
            why = f"the site refused the crawler (HTTP {row[0]}); it blocks bots"
        else:
            why = f"the start page answered HTTP {row[0]}"
        msg = f"ERROR: no HTML pages from {self.start_url}: {why}"
        self.log(f"[{self.role}] {msg}")
        st.set_meta(f"empty_reason:{self.role}", msg)

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
        # probe B has a different shape (deeper path, .html): the soft-404 self-test checks that the
        # fingerprint learned from probe A recognises it
        probe_b = f"{self.start_url.rstrip('/')}/seo-intel-check/{sha(self.domain)[10:20]}-missing-page.html"
        for key, u in ((f"probe404:{self.role}", probe), (f"probe404b:{self.role}", probe_b)):
            if not self.allowed_sync(u):
                continue
            r = await self.f.fetch(u, same_site=self.same_site)
            st = r.get("status")
            self.store.set_meta(key, {"url": u, "status": st if isinstance(st, int) else str(st),
                                      "html": decode(r.get("body") or b"", r.get("encoding"))[:300000]
                                      if st == 200 else None})

    async def frontier_loop(self):
        """Deterministic BFS: one depth level at a time, URLs of a level in a fixed hash order, a page cap
        fixed from the time budget, trap and render decisions taken on fixed samples. Two runs on the same
        site therefore fetch the same pages in the same order unless the deadline itself cuts the crawl."""
        st = self.store
        workers = self.cfg.concurrency
        pending = set()
        level = None
        self.t_frontier = time.monotonic()
        self.tranche_cap = min(self.page_cap, TRANCHE_FIRST)
        while self.deadline_ok():
            qmin = st.min_queued_depth(self.role, self.run)
            if not pending:
                if level is not None and (qmin is None or qmin > level):
                    await self.level_boundary()
                    qmin = st.min_queued_depth(self.role, self.run)
                if qmin is None:
                    break
                level = qmin
            done_pages = st.count("SELECT COUNT(*) FROM pages WHERE site=? AND run=?", (self.role, self.run))
            cap_left = self.tranche_cap - done_pages - 2 * len(pending)
            room = min(workers - len(pending), cap_left)
            if room > 0 and qmin == level:
                rows = st.next_batch(self.role, room * 4 + 16, self.run, self.inflight, depth=level)
                for row in rows:
                    if room <= 0:
                        break
                    u = row[0]
                    g = self.traps.gate(u)
                    if g == "wait" and u in self.dispatched_urls:
                        g = "ok"  # one of the template's own samples coming back after a 429 retry
                    if g == "wait":
                        continue  # its template's samples are still being judged
                    if g != "ok":
                        st.add_trap(self.role, g[0], g[1], u)
                        st.mark(u, "trap", g[0])
                        self.trap_avoided += 1
                        continue
                    if u not in self.dispatched_urls:
                        self.dispatched_urls.add(u)
                        self.traps.dispatched(u)
                    self.inflight[u] = row[1]
                    pending.add(asyncio.ensure_future(self.process(u, row[1], row[3])))
                    room -= 1
            if not pending:
                if cap_left > 0 and qmin == level and self.traps.force_judgement():
                    continue  # every sample of a waiting template is done: verdict taken, go on
                if cap_left <= 0 and qmin is not None and self.tranche_cap < self.page_cap:
                    if self.extend_tranche(done_pages):
                        continue
                    break
                if cap_left <= 0 or qmin is None or qmin == level:
                    break  # page cap reached, or nothing dispatchable at this level
                continue
            done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED,
                                               timeout=max(0.1, self.deadline - time.monotonic()))
            for d in done:
                if not d.cancelled() and d.exception():
                    exc = d.exception()
                    self.log(f"[{self.role}] worker error: {type(exc).__name__}: {exc}")
        if self.deadline_ok() and not pending:
            await self.level_boundary()
        stragglers = {t for t in pending if not t.done()}
        if stragglers:
            # deadline: cancel in-flight work; their URLs stay 'queued' => no lost work on resume.
            # Wait at most 10 s for cancellation, never indefinitely.
            for t in stragglers:
                t.cancel()
            await asyncio.wait(stragglers, timeout=10)

    def extend_tranche(self, done_pages):
        """The crawl stops only at fixed page counts (200, 300, 450 ...), never wherever the clock happens
        to cut it: at each step it continues only if the next step clearly fits in the time left (1.6x
        the time it should need, plus a reserve for rendering). On a slow or throttling site two runs
        therefore stop at the same page count and fetch the same pages, instead of 'whatever was done
        when the deadline hit'."""
        now = time.monotonic()
        elapsed = max(1.0, now - self.t_frontier)
        nxt = min(self.page_cap, max(done_pages + 1, int(self.tranche_cap * TRANCHE_GROWTH)))
        need = 2.0 * elapsed * (nxt - done_pages) / max(1, done_pages)
        reserve = max(20.0, 0.12 * (self.deadline - self.t_frontier))
        if self.deadline - now - reserve >= need:
            self.tranche_cap = nxt
            return True
        self.log(f"[{self.role}] stopping at {done_pages} pages: the next step ({nxt}) does not safely fit "
                 f"in the time left ({self.deadline - now:.0f}s; {elapsed:.0f}s used so far)")
        self.store.stat(self.role, "stopped_at_step", done_pages)
        return False

    async def level_boundary(self):
        """End of a BFS level: render the queued pages in a fixed order within the 15% budget."""
        await self.drain_renders(final=True)

    # ------------------------------------------------------------ one URL
    def _observe(self, url, h):
        if url in self.observed:
            return
        self.observed.add(url)
        trap = self.traps.observe(url, h)
        if trap:
            self.store.add_trap(self.role, trap[0], trap[1], url)
            self.trap_fetches += max(0, trap[2] - 1)

    async def process(self, url, depth, lastmod):
        outcome = "done"
        try:
            await self._process(url, depth, lastmod)
        except asyncio.CancelledError:
            outcome = "cancelled"
            raise
        finally:
            self.inflight.pop(url, None)
            if url in self.retrying:
                self.retrying.discard(url)
            elif outcome == "done":
                self._observe(url, None)  # non-HTML outcomes still count as a (unique) template sample

    async def _process(self, url, depth, lastmod):
        st = self.store
        if True:
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
            stv = r.get("status")
            transient = (not isinstance(stv, int)) or stv in (500, 502, 504, 520, 521, 522, 523, 524, 525)
            if transient and not r.get("chain") and self.retries.get(url, 0) < TRANSIENT_RETRIES:
                # a timeout / reset / 5xx is usually momentary: fetch once more (later in the same level)
                # instead of storing a different error in each run
                self.retries[url] = self.retries.get(url, 0) + 1
                self.retrying.add(url)
                await asyncio.sleep(2.0 * 2 ** (self.retries[url] - 1))  # 2 s, 4 s, 8 s
                return
            if r.get("status") in (429, 503) and not r.get("chain"):
                n = self.retries.get(url, 0)
                self.f.limiter.penalize(host_of(url), (r.get("headers") or {}).get("retry-after"))
                if n < MAX_RETRIES:
                    self.retries[url] = n + 1
                    self.retrying.add(url)
                    return  # stays 'queued': fetched again after the host's back-off
            
            if r.get("status") == 304 and prev is not None:
                self.carry_over(url, depth)
                st.update_page(url, etag=r["headers"].get("etag") or prev["etag"])
                return
            await self.handle_response(url, depth, lastmod, r)

    def pick_refresh_set(self):
        """Pages an incremental run re-checks even without a newer sitemap <lastmod>: the start page, the
        most-linked pages (hubs / nav pages: where new links show up), and a rotating 1-in-REFRESH_ROTATION
        sample keyed on the run number, so every page is re-checked within REFRESH_ROTATION runs."""
        st = self.store
        n = st.count("SELECT COUNT(*) FROM pages WHERE site=? AND status=200", (self.role,))
        k = max(HUB_MIN, -(-n * HUB_SHARE // 100))
        hubs = [r[0] for r in st.db.execute(
            """SELECT l.dst, COUNT(DISTINCT l.src) c FROM links l JOIN pages p ON p.url=l.dst
               WHERE p.site=? AND p.status=200 GROUP BY l.dst ORDER BY c DESC, l.dst LIMIT ?""", (self.role, k))]
        rot = {u for (u,) in st.db.execute("SELECT url FROM pages WHERE site=? ORDER BY url", (self.role,))
               if int(sha(f"{self.run}:{u}")[:8], 16) % REFRESH_ROTATION == 0}
        return {self.start_url} | set(hubs) | rot

    def can_skip(self, prev, depth, lastmod):
        if prev["status"] != 200:
            return False
        if depth == 0 or prev["url"] in self.refresh_set:
            return False
        row = self.store.db.execute("SELECT fetched_at, lastmod FROM pages WHERE url=?", (prev["url"],)).fetchone()
        if not row:
            return False
        if (lastmod or None) != (row[1] or None):
            return False  # sitemap <lastmod> changed since the last fetch (or appeared / disappeared)
        lm = _parse_date(lastmod)
        return not (lm is not None and row[0] and lm > row[0])

    def carry_over(self, url, depth):
        """An unchanged page: keep the stored copy, but replay exactly what a fetch would have done with it
        (outlinks incl. nofollow / meta-robots rules, canonical / refresh targets, session-id records,
        the template-trap sample) so an incremental run decides the same as a fresh crawl."""
        st = self.store
        st.db.execute("BEGIN")
        st.db.execute("UPDATE pages SET run=?, unchanged=1 WHERE url=?", (self.run, url))
        st.db.execute("UPDATE urls SET state='done' WHERE url=?", (url,))
        st.db.execute("COMMIT")
        r = st.db.execute("SELECT html, x_robots, vhash, raw_hash, rendered FROM pages WHERE url=?", (url,)).fetchone()
        if not r or r[0] is None:
            return
        html = zlib.decompress(r[0]).decode("utf-8", "replace")
        self.n_html += 1
        self.n_rendered += 1 if r[4] else 0  # a reused rendered page still counts against the 15% render cap
        fields, _ = P.parse_page(html, url)
        row = {"x_robots": r[1]}
        links = self.apply_fields(row, fields, url)
        st.db.execute("UPDATE pages SET schema_types=? WHERE url=?", (row["schema_types"], url))
        self.enqueue_links(url, depth, links, row)
        self._observe(url, r[2] or r[3])

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
                # the target is fetched in its own turn of the fixed crawl order (storing it here would
                # depend on which task happened to finish first)
                self.enqueue([(final, (depth or 0) + 1, 90.0, None)], url)  # next level: a level's URL set is fixed
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
        self._observe(url, tpl_hash)
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
        row["schema_types"] = ",".join(fields.get("schema") or []) or None
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
                items.append((extra, d, 95.0, None))
        for _, h in json.loads(row.get("hreflang") or "[]"):
            if h:
                items.append((h, d, 70.0, None))
        self.enqueue(items, url)

    # ------------------------------------------------------------ rendering
    def render_budget(self, audit=False):
        """Renders still allowed (15% cap). RENDER_AUDIT_N of them are kept for the recall audit."""
        return int(RENDER_SHARE * self.n_html) - self.n_rendered - (0 if audit else RENDER_AUDIT_N)

    async def render_audit(self):
        """Automatic render-recall check: render a fixed hash-ordered sample of script-bearing pages the
        policy chose NOT to render. A sampled page whose text grows by > DIFF_THRESHOLD is a missed render;
        the miss rate on the sample estimates how many such pages the rest of the site holds.
        Resumable: pages audited before a crash stay in the sample and are not rendered twice."""
        if not self.renderer.available:
            return
        from .render import DIFF_THRESHOLD
        st = self.store
        inc = int(self.mode == "incremental")
        rows = st.db.execute(
            "SELECT url, rendered, render_reason FROM pages WHERE site=? AND status=200 AND html IS NOT NULL AND "
            "scripts>0 AND run=? AND (unchanged=0 OR ?=0) AND (rendered=0 OR render_reason LIKE 'audit:%')",
            (self.role, self.run, inc)).fetchall()
        if inc and not rows:
            return  # nothing new to audit: the last run's audit still describes the stored pages
        done_before = {r[0] for r in rows if r[1]}
        pool = sorted((r[0] for r in rows), key=sha)
        n = max(len(done_before), min(RENDER_AUDIT_N, self.render_budget(audit=True) + len(done_before)))
        sample = pool[:max(0, n)]
        todo = [u for u in sample if u not in done_before]
        n_par = max(1, self.renderer.n)
        for i in range(0, len(todo), n_par):
            if not self.deadline_ok():
                break
            await asyncio.gather(*(self.render_one(u, "audit") for u in todo[i:i + n_par]))
        sampled = missed = 0
        for (rr,) in st.db.execute("SELECT render_reason FROM pages WHERE site=? AND run=? AND rendered=1 AND "
                                   "render_reason LIKE 'audit:%'", (self.role, self.run)):
            sampled += 1
            try:
                missed += float(rr.rsplit("gain=", 1)[1]) > DIFF_THRESHOLD
            except (IndexError, ValueError):
                pass
        st.stat(self.role, "render_audit_pool", len(pool))
        st.stat(self.role, "render_audit_sampled", sampled)
        st.stat(self.role, "render_audit_missed", missed)

    async def drain_renders(self, final=False):
        n_par = max(1, self.renderer.n)
        while self.render_q and self.render_budget() > 0 and self.deadline_ok():
            take = min(n_par, self.render_budget(), len(self.render_q))
            part = [heapq.heappop(self.render_q) for _ in range(take)]
            await asyncio.gather(*(self.render_one(url, reason) for _, url, _, reason in part))

    async def template_probe_phase(self):
        """Deterministic per-template probing after the crawl (see render.py)."""
        if not self.renderer.available:
            return
        from collections import defaultdict
        from .urlnorm import section_template as template
        by_tpl = defaultdict(list)
        inc = self.mode == "incremental"
        known = self.store.get_meta(f"template_js:{self.role}", {}) if inc else {}
        for (u,) in self.store.db.execute(
                "SELECT url FROM pages WHERE site=? AND status=200 AND html IS NOT NULL AND rendered=0 AND scripts>0 "
                "AND run=? AND (unchanged=0 OR ?=0) ORDER BY url", (self.role, self.run, int(inc))):
            by_tpl[template(u)].append(u)
        # incremental: unchanged pages keep last run's render result; sections probed last time keep their
        # verdict (only pages fetched again are rendered when the section needs it)
        for tpl, js in known.items():
            self.policy.template_js.setdefault(tpl, js)
        from .render import PROBES_PER_TEMPLATE
        n_par = max(1, self.renderer.n)

        async def batch(urls, reason):
            """Render in parallel batches; the budget is checked per batch in a fixed order (repeatable)."""
            out = []
            for i in range(0, len(urls), n_par):
                take = min(n_par, self.render_budget())
                if take <= 0 or not self.deadline_ok():
                    return out, False
                part = urls[i:i + take]
                gains = await asyncio.gather(*(self.render_one(u, reason) for u in part))
                out.extend(zip(part, gains))
                if take < n_par and i + take < len(urls):
                    return out, False
            return out, True

        for tpl in sorted(by_tpl):
            urls = sorted(by_tpl[tpl], key=lambda u: sha(u))
            if tpl in known:
                if known[tpl]:
                    _, ok = await batch(urls, "template_js")
                    if not ok:
                        break
                continue
            res, ok = await batch(urls[:PROBES_PER_TEMPLATE], "probe")
            for u, g in res:
                if g is not None:
                    self.policy.learn(u, g)
            if not ok:
                break
            if self.policy.template_js.get(tpl):
                _, ok = await batch(urls[PROBES_PER_TEMPLATE:], "template_js")
                if not ok:
                    break
        self.store.set_meta(f"template_js:{self.role}", dict(sorted(self.policy.template_js.items())))

    async def render_one(self, url, reason):
        """Render one stored page; update fields/links if rendering changed it. Returns token gain."""
        html = self.store.html(url)
        if not html:
            return None
        self.n_rendered += 1
        rendered, nav_target = await self.renderer.render(url, html)
        if not rendered:  # a browser hiccup: one more try, so a timeout does not decide what is stored
            rendered, nav_target = await self.renderer.render(url, html)
        if not rendered:
            # Record the ATTEMPT, not the browser's luck: the page counts as rendered (it used render budget)
            # and keeps its raw HTML, exactly like a render that changed nothing. Otherwise a page whose render
            # timed out in one run only would be reported differently in the two runs.
            st = self.store
            st.db.execute("BEGIN")
            st.db.execute("UPDATE pages SET rendered=1, render_reason=? WHERE url=?", (f"{reason}:failed", url))
            st.db.execute("COMMIT")
            self.log(f"[{self.role}] render failed twice, raw HTML kept: {url}")
            return None
        raw_vt = P.visible_text(P.to_tree(html))
        ren_vt = P.visible_text(P.to_tree(rendered))
        gain = token_gain(raw_vt, ren_vt)
        fields, _ = P.parse_page(rendered, url)
        row = {}
        links = self.apply_fields(row, fields, url)
        if nav_target:
            from urllib.parse import urlsplit
            sp = urlsplit(nav_target)
            if P.AUTH_NAV.search(sp.path + ("?" + sp.query if sp.query else "")):
                nav_target = None  # login/SSO/consent scripts navigating a headless browser: not a redirect
            else:
                # a script navigation counts only if a second render navigates to the same place (timers,
                # consent banners and A/B scripts navigate in some loads only)
                _, nav2 = await self.renderer.render(url, html)
                if nav2 and normalize(nav2, url)[0] == normalize(nav_target, url)[0]:
                    row["js_redirect"] = normalize(nav_target, url)[0]
                else:
                    nav_target = None
        # Only a MEANINGFUL difference replaces the stored page. Small gains come from timing (a carousel
        # or a lazy widget that loaded in one run and not in the other); keeping the raw HTML for those makes
        # two runs store the same text. Same threshold as the render decision itself.
        from .render import DIFF_THRESHOLD
        meaningful = gain > DIFF_THRESHOLD
        if UNSURE_LO <= gain <= UNSURE_HI:
            # Close to the line: one render can land on either side (late XHR, lazy widgets). Render once
            # more and use the rendered DOM only if BOTH renders agree it changes the page; otherwise keep the
            # raw HTML. A page then flips only if two renders in a row disagree with the other run's two.
            again, _ = await self.renderer.render(url, html)
            if again:
                gain2 = token_gain(raw_vt, P.visible_text(P.to_tree(again)))
                if (gain2 > DIFF_THRESHOLD) != meaningful:
                    meaningful = False
                gain = min(gain, gain2)
        upd = {}
        if nav_target:
            upd["js_redirect"] = row["js_redirect"]
        if meaningful:
            upd = {k: row[k] for k in ("title", "h1", "canonical", "meta_robots", "hreflang", "js_redirect")
                   if row.get(k) is not None}
        if meaningful:  # a redirect alone does not replace the content: the raw HTML stays the page's text
            import zlib
            upd["html"] = zlib.compress(rendered.encode("utf-8", "replace"), 6)
        st = self.store
        st.db.execute("BEGIN")
        sets = ",".join(f"{k}=?" for k in upd) + ("," if upd else "") + "rendered=1, render_reason=?"
        st.db.execute(f"UPDATE pages SET {sets} WHERE url=?", list(upd.values()) + [f"{reason}:gain={gain:.2f}", url])
        if meaningful:
            st.db.execute("DELETE FROM links WHERE src=?", (url,))
            st.db.executemany("INSERT OR IGNORE INTO links VALUES(?,?,?,?,?)", links)
        st.db.execute("COMMIT")
        if meaningful:
            depth = st.db.execute("SELECT depth FROM pages WHERE url=?", (url,)).fetchone()[0]
            self.enqueue_links(url, depth, links, {**row, "x_robots": None})
        return gain
