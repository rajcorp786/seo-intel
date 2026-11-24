"""URL normalisation. Deterministic, site-agnostic."""
from __future__ import annotations

import re
from urllib.parse import parse_qsl, quote, unquote, urlencode, urljoin, urlsplit, urlunsplit

SESSION_PARAMS = re.compile(
    r"^(jsessionid|phpsessid|aspsessionid\w*|sessionid|session_id|sessid|sid|cfid|cftoken|zenid|osCsid|s)$",
    re.I)
TRACKING_PARAMS = re.compile(r"^(utm_\w+|gclid|fbclid|msclkid|mc_cid|mc_eid|_ga|yclid|igshid|ref_src)$", re.I)
TOKEN_VALUE = re.compile(r"^[0-9a-fA-F]{24,}$|^[A-Za-z0-9_\-]{32,}$")
PATH_SESSION = re.compile(r";(jsessionid|phpsessid|sid)=[^/?#]*", re.I)
SKIP_EXT = re.compile(
    r"\.(jpe?g|png|gif|webp|svg|ico|bmp|tiff?|avif|mp[34]|m4a|wav|ogg|webm|avi|mov|mkv|flv|wmv|"
    r"zip|rar|7z|gz|tgz|bz2|tar|exe|dmg|msi|apk|iso|bin|pdf|docx?|xlsx?|pptx?|odt|csv|"
    r"css|js|mjs|json|woff2?|ttf|otf|eot|rss|atom)$", re.I)


def host_of(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


def netloc_of(url: str) -> str:
    sp = urlsplit(url)
    return f"{sp.scheme}://{(sp.netloc or '').lower()}"


def reg_domain(host: str) -> str:
    """Rough registrable domain (last two labels, three for common 2nd-level ccTLDs)."""
    parts = host.lower().strip(".").split(".")
    if len(parts) >= 3 and parts[-2] in ("co", "com", "net", "org", "gov", "ac", "edu", "nic", "res", "gen", "firm", "ind"):
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def normalize(url: str, base: str | None = None) -> tuple[str | None, str | None]:
    """Return (normalized_url, session_param_found_or_None). None url = not crawlable."""
    if not url:
        return None, None
    url = url.strip()
    if base:
        try:
            url = urljoin(base, url)
        except ValueError:
            return None, None
    try:
        sp = urlsplit(url)
    except ValueError:
        return None, None
    if sp.scheme not in ("http", "https"):
        return None, None
    host = (sp.hostname or "").lower().rstrip(".")
    if not host:
        return None, None
    try:
        port = sp.port
    except ValueError:
        return None, None
    netloc = host
    if port and not ((sp.scheme == "http" and port == 80) or (sp.scheme == "https" and port == 443)):
        netloc = f"{host}:{port}"
    path = sp.path or "/"
    session = None
    m = PATH_SESSION.search(path)
    if m:
        session = m.group(1).lower()
        path = PATH_SESSION.sub("", path)
    # canonical percent-encoding of path (keeps reserved chars)
    path = quote(unquote(path), safe="/:@!$&'()*+,;=-._~%")
    path = re.sub(r"/{2,}", "/", path)
    q = []
    for k, v in parse_qsl(sp.query, keep_blank_values=True):
        if TRACKING_PARAMS.match(k):
            continue
        if SESSION_PARAMS.match(k) and (k.lower() != "s" or TOKEN_VALUE.match(v or "")):
            session = k.lower()
            continue
        if TOKEN_VALUE.match(v or "") and re.search(r"sess|token|sid", k, re.I):
            session = k.lower()
            continue
        q.append((k, v))
    query = urlencode(q, doseq=True, quote_via=quote, safe="/:@!$'()*,;-._~")
    return urlunsplit((sp.scheme.lower(), netloc, path, query, "")), session


def site_scope(host: str) -> str:
    """Crawl scope for the host the user gave:
    'example.com' / 'www.example.com' -> example.com and all its subdomains;
    'docs.example.com' (an explicit subdomain) -> only docs.example.com (and its own subdomains)."""
    h = host.lower().strip(".")
    if h.startswith("www."):
        h = h[4:]
    return h if h != reg_domain(h) else reg_domain(h)


def is_asset(url: str) -> bool:
    return bool(SKIP_EXT.search(urlsplit(url).path))


def template(url: str) -> str:
    """Path template: digit runs -> {n}, long hex -> {h}; query keys sorted."""
    sp = urlsplit(url)
    p = re.sub(r"[0-9a-f]{16,}", "{h}", sp.path, flags=re.I)
    p = re.sub(r"\d+", "{n}", p)
    keys = sorted({k for k, _ in parse_qsl(sp.query, keep_blank_values=True)})
    return p + ("?" + "&".join(keys) if keys else "")


def section_template(url: str) -> str:
    """Coarser template for render probing: last path segment -> '*' (slug sites have no digits,
    so template() would make every page its own template)."""
    sp = urlsplit(url)
    segs = [x for x in sp.path.split("/") if x]
    base = "/" + "/".join(re.sub(r"\d+", "{n}", x) for x in segs[:-1]) if len(segs) > 1 else ""
    keys = sorted({k for k, _ in parse_qsl(sp.query, keep_blank_values=True)})
    return (base + "/*") + ("?" + "&".join(keys) if keys else "")
