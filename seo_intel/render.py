"""Selective headless rendering.

Decision without rendering everything first:
 1. Cheap raw-HTML signals per page (visible-text length, script count,
    SPA framework mount markers, <noscript> "enable JavaScript").
 2. Template probes (after the crawl, so the choice never depends on fetch order): for every
    site section (path with the last segment -> '*') the PROBES_PER_TEMPLATE script-bearing pages with the
    lowest URL hash are rendered. If rendering adds > DIFF_THRESHOLD of the visible tokens, the
    template is JS-dependent and its other pages are rendered (hash order) within budget.
 3. Hard budget: renders <= RENDER_SHARE * pages fetched, enforced continuously
    (pages wait in a priority queue until budget allows).
All sub-resource requests are routed through our own rate-limited, robots-aware
fetcher, so rendering never breaks politeness.
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import re
from collections import defaultdict

from .urlnorm import host_of, section_template as template

RENDER_SHARE = 0.15
PROBES_PER_TEMPLATE = 2
DIFF_THRESHOLD = 0.20
FIXED_EPOCH = _dt.datetime(2026, 1, 1, tzinfo=_dt.timezone.utc)  # the page's "now" in every render
SETTLE_MS = 5000                 # simulated time given to page timers after load
DETERMINISTIC_JS = """(() => {
  let s = 0x2545F491;
  Math.random = function () { s ^= s << 13; s ^= s >>> 17; s ^= s << 5; return ((s >>> 0) % 1e9) / 1e9; };
  if (window.crypto && crypto.getRandomValues) {
    crypto.getRandomValues = function (a) { for (let i = 0; i < a.length; i++) a[i] = Math.floor(Math.random() * 256); return a; };
  }
})();"""
TOK = re.compile(r"\w+", re.U)
CACHE_MAX = 150 * 1024 * 1024
TRACKERS = re.compile(r"google-analytics\.com|googletagmanager\.com|doubleclick\.net|googlesyndication\.com|"
                      r"facebook\.(?:net|com)/(?:tr|signals|en_US/fbevents)|connect\.facebook\.net|hotjar\.com|"
                      r"clarity\.ms|segment\.(?:io|com)|mixpanel\.com|intercom\.io|hubspot\.com|hs-scripts\.com|"
                      r"hs-analytics\.net|linkedin\.com/px|snap\.licdn\.com|tiktok\.com/i18n/pixel|bat\.bing\.com|"
                      r"newrelic\.com|nr-data\.net|sentry\.io|fullstory\.com|optimizely\.com|adroll\.com|"
                      r"taboola\.com|outbrain\.com|criteo\.(?:com|net)|amazon-adsystem\.com|cookielaw\.org|"
                      r"onetrust\.com|cookiebot\.com|youtube\.com/(?:embed|iframe_api)|vimeo\.com", re.I)


_NUM = re.compile(r"\d+")


def token_gain(raw_text: str, rendered_text: str) -> float:
    """Share of distinct rendered words not in the raw HTML. Numbers count as one word ("0"): clocks,
    counters and ids change on every request and must not move a page across the render threshold."""
    r = set(_NUM.sub("0", t) for t in TOK.findall((raw_text or "").lower()))
    n = set(_NUM.sub("0", t) for t in TOK.findall((rendered_text or "").lower()))
    if not n:
        return 0.0
    return len(n - r) / len(n)


class RenderPolicy:
    def __init__(self):
        self.probes = defaultdict(int)
        self.template_js = {}  # tpl -> bool
        self.template_scores = defaultdict(list)

    def reason(self, url, signals):
        tl, sc = signals.get("text_len", 0), signals.get("scripts", 0)
        fw, ns = signals.get("framework"), signals.get("noscript_js")
        if tl < 300 and (fw or sc >= 5 or ns):
            return "low_text_js", 3
        if fw and tl < 1500:
            return "framework_shell", 3
        return None

    def learn(self, url, gain):
        tpl = template(url)
        self.template_scores[tpl].append(gain)
        sc = self.template_scores[tpl]
        if len(sc) >= PROBES_PER_TEMPLATE or max(sc) > DIFF_THRESHOLD:
            self.template_js[tpl] = max(sc) > DIFF_THRESHOLD


class Renderer:
    def __init__(self, fetcher, robots_allowed, pages=3):
        self.fetcher = fetcher
        self.cache = {}          # sub-resource cache: the same JS bundle is fetched once, not per page
        self.cache_bytes = 0
        self.robots_allowed = robots_allowed
        self.n = pages
        self.sem = asyncio.Semaphore(pages)
        self._pw = self._browser = None
        self.available = True
        self.error = None

    async def start(self):
        if self._browser or not self.available:
            return
        try:
            from playwright.async_api import async_playwright
            self._pw = await async_playwright().start()
            try:
                self._browser = await self._pw.chromium.launch(args=["--disable-gpu", "--no-sandbox"])
            except Exception:
                import subprocess
                import sys
                subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"],
                               capture_output=True, timeout=600)
                self._browser = await self._pw.chromium.launch(args=["--disable-gpu", "--no-sandbox"])
        except Exception as e:  # rendering is optional; crawl continues without it
            self.available = False
            self.error = f"{type(e).__name__}: {e}"[:300]

    async def close(self):
        try:
            if self._browser:
                await self._browser.close()
            if self._pw:
                await self._pw.stop()
        except Exception:
            pass

    async def render(self, url: str, html: str, timeout_ms=10000, idle_ms=2500, hard_s=25.0):
        """Return (rendered_html, js_navigation_target_or_None) or (None, None). Never runs longer than hard_s."""
        await self.start()
        if not self.available:
            return None, None
        try:
            return await asyncio.wait_for(self._render(url, html, timeout_ms, idle_ms), timeout=hard_s)
        except (asyncio.TimeoutError, Exception):
            return None, None

    # A cold render fetches every script of the page for the first time (no cache yet); on a heavy WordPress
    # page that alone can pass 10 s, so the FIRST render of a page fails while every later one succeeds. That
    # made one run store raw HTML and the other the rendered DOM. render_sure retries with longer limits
    # (scripts fetched during a failed try are cached, so later tries are fast) before giving up.
    RETRY_PLAN = ((10000, 25.0), (30000, 60.0), (30000, 60.0))

    async def render_sure(self, url: str, html: str):
        for i, (t_ms, hard) in enumerate(self.RETRY_PLAN):
            out, nav = await self.render(url, html, timeout_ms=t_ms, hard_s=hard)
            if out:
                return out, nav
            if not self.available:
                break
            await asyncio.sleep(2 * (i + 1))
        return None, None

    async def _render(self, url, html, timeout_ms, idle_ms):
        async with self.sem:
            ctx = await self._browser.new_context(user_agent="Mozilla/5.0 (compatible; seo-intel/1.0; +https://github.com/rajcorp786/seo-intel)",
                                                  java_script_enabled=True)
            # Same page in, same DOM out: a fixed clock (Date, timers) and a seeded Math.random, so a widget that
            # appears "after 0-3 s" or shows "updated 07:41" renders identically in every run
            await ctx.add_init_script(DETERMINISTIC_JS)
            page = await ctx.new_page()
            fake_clock = False
            try:
                await page.clock.install(time=FIXED_EPOCH)
                await page.clock.pause_at(FIXED_EPOCH + _dt.timedelta(seconds=1))  # time moves only via run_for
                fake_clock = True
            except Exception:
                fake_clock = False  # older Playwright: real clock (still works, less repeatable)
            nav = {"target": None, "main_done": False}

            async def handle(route, request):
                rtype = request.resource_type
                rurl = request.url
                if request.is_navigation_request() and request.frame == page.main_frame:
                    if not nav["main_done"]:
                        nav["main_done"] = True
                        await route.fulfill(status=200, body=html, content_type="text/html; charset=utf-8")
                        return
                    nav["target"] = rurl
                    await route.abort()
                    return
                if rtype in ("image", "media", "font", "stylesheet", "websocket", "manifest", "other") \
                        or not rurl.startswith("http") or TRACKERS.search(rurl):
                    await route.abort()
                    return
                if not self.robots_allowed(rurl):
                    await route.abort()
                    return
                hit = self.cache.get(rurl) if rtype == "script" else None
                if hit is None and self.fetcher.limiter.is_paused(host_of(rurl)):
                    await route.abort()  # host is throttling us: don't queue behind its back-off
                    return
                if hit is None:
                    r = await self.fetcher.get_once(rurl, any_body=True)
                    if r.get("status") != 200:
                        await route.abort()
                        return
                    hit = (r["body"], r["headers"].get("content-type", "application/octet-stream"))
                    if rtype == "script" and self.cache_bytes + len(hit[0]) < CACHE_MAX:
                        self.cache[rurl] = hit
                        self.cache_bytes += len(hit[0])
                await route.fulfill(status=200, body=hit[0], content_type=hit[1])

            await page.route("**/*", handle)
            out = None
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
                for _ in range(2):
                    if fake_clock:
                        await page.clock.run_for(SETTLE_MS)  # every timer due in the first seconds fires, in order
                    try:
                        await page.wait_for_load_state("networkidle", timeout=idle_ms)
                    except Exception:
                        pass
                if fake_clock:
                    await page.clock.run_for(SETTLE_MS)
                out = await page.content()
            except Exception:
                out = None
            finally:
                await ctx.close()
            return out, nav["target"]
