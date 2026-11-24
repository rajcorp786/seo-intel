"""Soft-404 detection (200 responses that are really 'not found' or empty). Precision-first.

A page is soft-404 if ANY of:
  probe     - main content ~identical (token Jaccard >= 0.85) to the site's response for a
              deterministic random non-existent URL that returned 200
  phrase    - title or H1 (or first 200 chars of main text when it is short) contains a
              not-found phrase (en / hi / hinglish) AND main text < 150 tokens
  empty     - main content < 8 tokens and the page is not the homepage and has no
              meaningful headings
"""
from __future__ import annotations

import re

from .textnorm import tokens

PHRASES = re.compile(
    r"(page|post|product|item|article|content|url|file)s?\s+(you\s+(were\s+)?(are\s+)?(looking|searching)\s+for\s+)?"
    r"(was\s+|is\s+|could\s+)?(not\s+(be\s+)?found|doesn'?t\s+exist|does\s+not\s+exist|no\s+longer\s+(exists|available)|"
    r"(has\s+been\s+)?(removed|deleted|moved))|\b404\b|not\s+found|nothing\s+(was\s+)?found|"
    r"no\s+(results|products|posts)\s+found|oops|page\s+unavailable|"
    r"पेज\s*नहीं\s*मिला|पृष्ठ\s*नहीं\s*मिला|नहीं\s*मिला|उपलब्ध\s*नहीं|"
    r"page\s+nahi\s+mila|nahi\s+mila", re.I)


def jaccard(a, b):
    a, b = set(a), set(b)
    if not a and not b:
        return 1.0
    return len(a & b) / max(1, len(a | b))


def classify(page, probe_tokens, is_home):
    main = page["main_text"] or ""
    mt = tokens(main)
    title = page["title"] or ""
    h1 = page["h1"] or ""
    if probe_tokens is not None and len(mt) < 400 and jaccard(mt, probe_tokens) >= 0.85:
        return "probe: matches response for a non-existent URL"
    if len(mt) < 150:
        head = f"{title} | {h1}"
        if PHRASES.search(head) or (len(mt) < 60 and PHRASES.search(main[:200])):
            return "phrase: not-found wording in title/H1/content"
    if not is_home and len(mt) < 8 and not h1.strip():
        return "empty: < 8 main-content tokens"
    return None
