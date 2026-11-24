"""Polite HTTP fetching: strict per-host rate limit, manual redirect chains."""
from __future__ import annotations

import asyncio
import time
from collections import defaultdict, deque

import httpx

from .urlnorm import host_of, normalize

UA = "Mozilla/5.0 (compatible; seo-intel/0.1; +https://github.com/seo-intel)"
MAX_BYTES = 5 * 1024 * 1024
MAX_HOPS = 10


class HostLimiter:
    """At most `rate` requests in ANY sliding 1-second window, per host.

    Uses a sliding log (not a token bucket) so the 6 req/s guarantee is exact,
    plus a tiny safety margin. Records per-second peaks for BENCHMARKS.
    """

    def __init__(self, rate: float = 6.0):
        self.rate = int(rate)
        self.logs = defaultdict(deque)
        self.locks = defaultdict(asyncio.Lock)
        self.total = defaultdict(int)
        self.peak = defaultdict(int)

    async def acquire(self, host: str):
        async with self.locks[host]:
            log = self.logs[host]
            while True:
                now = time.monotonic()
                while log and now - log[0] >= 1.02:
                    log.popleft()
                if len(log) < self.rate:
                    log.append(now)
                    self.total[host] += 1
                    self.peak[host] = max(self.peak[host], len(log))
                    return
                await asyncio.sleep(1.02 - (now - log[0]) + 0.001)


class Fetcher:
    def __init__(self, limiter: HostLimiter, timeout=20.0, concurrency=32):
        self.limiter = limiter
        self.client = httpx.AsyncClient(
            headers={"User-Agent": UA, "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.5",
                     "Accept-Encoding": "gzip, deflate", "Accept-Language": "en,hi;q=0.8"},
            timeout=httpx.Timeout(timeout, connect=10.0), follow_redirects=False,
            limits=httpx.Limits(max_connections=concurrency, max_keepalive_connections=concurrency))

    async def close(self):
        await self.client.aclose()

    async def get_once(self, url, headers=None, any_body=False):
        """Single request (no redirects). Returns dict or error dict."""
        await self.limiter.acquire(host_of(url))
        t0 = time.monotonic()
        try:
            async with self.client.stream("GET", url, headers=headers or {}) as r:
                body = b""
                ctype = r.headers.get("content-type", "")
                if r.status_code == 200 and (any_body or "html" in ctype or "xml" in ctype
                                             or "text/plain" in ctype or not ctype):
                    chunks = []
                    size = 0
                    async for c in r.aiter_bytes():
                        chunks.append(c)
                        size += len(c)
                        if size > MAX_BYTES:
                            break
                    body = b"".join(chunks)
                return {"status": r.status_code, "headers": {k.lower(): v for k, v in r.headers.items()},
                        "body": body, "encoding": r.encoding, "elapsed": time.monotonic() - t0}
        except (httpx.HTTPError, OSError, ValueError) as e:
            return {"status": None, "error": type(e).__name__, "headers": {}, "body": b""}

    async def fetch(self, url, cond_headers=None, same_site=None, any_body=False):
        """Follow 3xx manually. Returns final response + chain [(url,status)], loop flag."""
        chain = []
        seen = set()
        cur = url
        for _ in range(MAX_HOPS + 1):
            if cur in seen:
                return {"status": "loop", "chain": chain + [(cur, "loop")], "final_url": cur,
                        "headers": {}, "body": b""}
            seen.add(cur)
            r = await self.get_once(cur, cond_headers if cur == url else None, any_body=any_body)
            st = r["status"]
            if st in (301, 302, 303, 307, 308) and r["headers"].get("location"):
                nxt, _ = normalize(r["headers"]["location"], cur)
                chain.append((cur, st))
                if nxt is None:
                    break
                if same_site and not same_site(nxt):
                    r["chain"] = chain + [(nxt, "offsite")]
                    r["final_url"] = nxt
                    r["status"] = "offsite"
                    return r
                cur = nxt
                continue
            r["chain"] = chain
            r["final_url"] = cur
            return r
        return {"status": "too_many_redirects", "chain": chain, "final_url": cur, "headers": {}, "body": b""}


def decode(body: bytes, encoding: str | None) -> str:
    if not body:
        return ""
    for enc in (encoding, "utf-8"):
        if enc:
            try:
                return body.decode(enc)
            except (LookupError, UnicodeDecodeError):
                pass
    return body.decode("utf-8", "replace")
