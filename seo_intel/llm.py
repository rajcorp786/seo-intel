"""LLM client with hard cost cap, input-token cap, and a persistent response cache.

Repeatability: temperature 0 + every response cached on disk keyed by
sha256(model, system, messages, tools, max_tokens, seed). A second identical run reads
the cache and is byte-identical. Cache dir: $SEO_INTEL_CACHE or ~/.cache/seo-intel/llm.
No API key => LLM disabled; every caller has a deterministic code fallback.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

MODEL = os.environ.get("SEO_INTEL_MODEL", "claude-haiku-5-5")
PRICE_IN = float(os.environ.get("SEO_INTEL_PRICE_IN", "0.10"))   # USD per 1M input tokens
PRICE_OUT = float(os.environ.get("SEO_INTEL_PRICE_OUT", "0.50"))  # USD per 1M output tokens
MAX_INPUT_TOKENS = 8000
BUDGET_USD = float(os.environ.get("SEO_INTEL_BUDGET_USD", "0.45"))  # under the 0.50 hard limit


def est_tokens(obj) -> int:
    """Conservative token estimate (bytes/3 over-counts English, ~right for Devanagari)."""
    s = obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False)
    return len(s.encode("utf-8")) // 3 + 16


class LLM:
    def __init__(self, seed=42, enabled=True, log_path=None):
        self.seed = seed
        self.key = os.environ.get("ANTHROPIC_API_KEY")
        self.enabled = bool(enabled and self.key)
        self.cache = Path(os.environ.get("SEO_INTEL_CACHE", Path.home() / ".cache" / "seo-intel" / "llm"))
        self.cache.mkdir(parents=True, exist_ok=True)
        self.cost = 0.0
        self.calls = self.cache_hits = 0
        self.in_tok = self.out_tok = 0
        self.log_path = log_path
        self.blocked_reason = None if self.enabled else ("no ANTHROPIC_API_KEY" if enabled else "disabled")
        self._client = None
        self._temp_ok = True

    def _c(self):
        if self._client is None:
            import anthropic
            self._client = anthropic.Anthropic(api_key=self.key, max_retries=3, timeout=60)
        return self._client

    def available(self, expected_in=3000, expected_out=600):
        if not self.enabled:
            return False
        nxt = expected_in / 1e6 * PRICE_IN + expected_out / 1e6 * PRICE_OUT
        return self.cost + nxt <= BUDGET_USD

    def call(self, system, messages, tools=None, max_tokens=800):
        payload = {"model": MODEL, "system": system, "messages": messages, "tools": tools or [],
                   "max_tokens": max_tokens, "seed": self.seed}
        key = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        f = self.cache / f"{key}.json"
        n_in = est_tokens({"s": system, "m": messages, "t": tools or []})
        if n_in > MAX_INPUT_TOKENS:
            raise ValueError(f"LLM input too large: ~{n_in} tokens > {MAX_INPUT_TOKENS}")
        if f.exists():
            self.cache_hits += 1
            return json.loads(f.read_text(encoding="utf-8"))
        if not self.available(n_in, max_tokens):
            return None
        kw = dict(model=MODEL, system=system, messages=messages, max_tokens=max_tokens)
        if tools:
            kw["tools"] = tools
        if self._temp_ok:
            kw["temperature"] = 0
        t0 = time.time()
        try:
            r = self._c().messages.create(**kw)
        except Exception as e:  # some models reject temperature; retry once without it
            if self._temp_ok and "temperature" in str(e).lower():
                self._temp_ok = False
                kw.pop("temperature", None)
                r = self._c().messages.create(**kw)
            else:
                self.blocked_reason = f"{type(e).__name__}: {str(e)[:200]}"
                return None
        u = r.usage
        cost = u.input_tokens / 1e6 * PRICE_IN + u.output_tokens / 1e6 * PRICE_OUT
        self.cost += cost
        self.calls += 1
        self.in_tok += u.input_tokens
        self.out_tok += u.output_tokens
        if u.input_tokens > MAX_INPUT_TOKENS:
            self.blocked_reason = f"call used {u.input_tokens} input tokens"
        out = {"content": [b.model_dump() for b in r.content], "stop_reason": r.stop_reason,
               "usage": {"input_tokens": u.input_tokens, "output_tokens": u.output_tokens}}
        f.write_text(json.dumps(out, ensure_ascii=False, sort_keys=True), encoding="utf-8")
        if self.log_path:
            with open(self.log_path, "a", encoding="utf-8") as lf:
                lf.write(json.dumps({"t": round(time.time() - t0, 2), "in": u.input_tokens,
                                     "out": u.output_tokens, "cost": round(cost, 6)}) + "\n")
        return out

    def stats(self):
        return {"model": MODEL, "calls": self.calls, "cache_hits": self.cache_hits,
                "input_tokens": self.in_tok, "output_tokens": self.out_tok,
                "cost_usd": round(self.cost, 5), "enabled": self.enabled, "note": self.blocked_reason}


def text_of(resp):
    if not resp:
        return ""
    return "".join(b.get("text", "") for b in resp["content"] if b.get("type") == "text")


def parse_json(text):
    s = text.strip()
    if "```" in s:
        parts = s.split("```")
        for p in parts:
            p = p.strip()
            if p.startswith("json"):
                p = p[4:]
            if p.strip().startswith(("{", "[")):
                s = p
                break
    i = min([x for x in (s.find("{"), s.find("[")) if x >= 0], default=-1)
    if i < 0:
        return None
    try:
        return json.loads(s[i:s.rfind("}" if s[i] == "{" else "]") + 1])
    except json.JSONDecodeError:
        return None
