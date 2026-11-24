"""Selective headless rendering.

Decision without rendering everything first:
 1. Cheap raw-HTML signals per page (visible-text length, script count,
    SPA framework mount markers, <noscript> "enable JavaScript").
 2. Template probes (after the crawl, so the choice never depends on fetch order): for every
    URL template (path with digits -> {n}) the PROBES_PER_TEMPLATE script-bearing pages with the
    lowest URL hash are rendered. If rendering adds > DIFF_THRESHOLD of the visible tokens, the
    template is JS-dependent and its other pages are rendered (hash order) within budget.
 3. Hard budget: renders <= RENDER_SHARE * pages fetched, enforced continuously
    (pages wait in a priority queue until budget allows).
All sub-resource requests are routed through our own rate-limited, robots-aware
fetcher, so rendering never breaks politeness.
"""
from __future__ import annotations

import asyncio
import re
from collections import defaultdict

from .urlnorm import host_of, template

RENDER_SHARE = 0.15
PROBES_PER_TEMPLATE = 2
DIFF_THRESHOLD = 0.20
TOK = re.compile(r"\w+", re.U)


def token_gain(raw_text: str, rendered_text: str) -> float:
    r = set(TOK.findall((raw_text or "").lower()))
    n = set(TOK.findall((rendered_text or "").lower()))
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
    def __init__(self, fetcher, robots_allowed, pages=2):
        self.fetcher = fetcher
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

    async def render(self, url: str, html: str, timeout_ms=10000):
        """Return (rendered_html, js_navigation_target_or_None) or (None, None)."""
        await self.start()
        if not self.available:
            return None, None
        async with self.sem:
            ctx = await self._browser.new_context(user_agent="Mozilla/5.0 (compatible; seo-intel/0.1)",
                                                  java_script_enabled=True)
            page = await ctx.new_page()
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
                if rtype in ("image", "media", "font", "stylesheet") or not rurl.startswith("http"):
                    await route.abort()
                    return
                if not self.robots_allowed(rurl):
                    await route.abort()
                    return
                r = await self.fetcher.get_once(rurl, any_body=True)
                if r.get("status") != 200:
                    await route.abort()
                    return
                ctype = r["headers"].get("content-type", "application/octet-stream")
                await route.fulfill(status=200, body=r["body"], content_type=ctype)

            await page.route("**/*", handle)
            out = None
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
                try:
                    await page.wait_for_load_state("networkidle", timeout=timeout_ms // 2)
                except Exception:
                    pass
                out = await page.content()
            except Exception:
                out = None
            finally:
                await ctx.close()
            return out, nav["target"]
