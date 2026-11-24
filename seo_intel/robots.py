"""robots.txt matching exactly as Google reads it.

Line-by-line port of google/robotstxt (robots.cc):
  * longest-match wins (priority = pattern length), Allow wins ties
  * '*' and trailing '$' wildcards
  * consecutive user-agent lines form one group; sitemap/unknown lines do not
    break a group
  * most specific group wins; if a specific group exists, the global group is
    ignored entirely
  * "/index.htm*" allow rules also allow the directory ("/dir/$")
  * missing-colon lines accepted when exactly two tokens ("disallow /")
  * frequent typos accepted (useragent, dissallow, ...)
  * UTF-8 BOM skipped, lines cut at 2083*8-1 bytes, patterns percent-escaped
Fetch semantics (Google): 2xx parse, 4xx (not 429) allow-all, 5xx/429/error
disallow-all, body capped at 500 KiB.
"""
from __future__ import annotations

K_MAX_LINE_LEN = 2083 * 8
HEX = "0123456789ABCDEF"


def get_path_params_query(url: str) -> str:
    search_start = 2 if len(url) >= 2 and url[0] == "/" and url[1] == "/" else 0

    def find_first_of(s, chars, start):
        best = -1
        for i in range(start, len(s)):
            if s[i] in chars:
                return i
        return best

    early_path = find_first_of(url, "/?;", search_start)
    protocol_end = url.find("://", search_start)
    if early_path != -1 and (protocol_end == -1 or early_path < protocol_end):
        protocol_end = -1
    protocol_end = search_start if protocol_end == -1 else protocol_end + 3
    path_start = find_first_of(url, "/?;", protocol_end)
    if path_start != -1:
        hash_pos = url.find("#", search_start)
        if hash_pos != -1 and hash_pos < path_start:
            return "/"
        path_end = len(url) if hash_pos == -1 else hash_pos
        if url[path_start] != "/":
            return "/" + url[path_start:path_end]
        return url[path_start:path_end]
    return "/"


def maybe_escape_pattern(src: str) -> str:
    """Uppercase existing %xx escapes and percent-encode bytes >= 0x80."""
    b = src.encode("utf-8", "surrogateescape")
    out = []
    i = 0
    n = len(b)
    while i < n:
        c = b[i]
        if c == 0x25 and i + 2 < n and _ishex(b[i + 1]) and _ishex(b[i + 2]):
            out.append("%" + chr(b[i + 1]).upper() + chr(b[i + 2]).upper())
            i += 3
            continue
        if c >= 0x80:
            out.append("%" + HEX[c >> 4] + HEX[c & 0xF])
        else:
            out.append(chr(c))
        i += 1
    return "".join(out)


def _ishex(c):
    return 48 <= c <= 57 or 65 <= c <= 70 or 97 <= c <= 102


def matches(path: str, pattern: str) -> bool:
    pathlen = len(path)
    pos = [0] * (pathlen + 1)
    numpos = 1
    plen = len(pattern)
    for pi, ch in enumerate(pattern):
        if ch == "$" and pi + 1 == plen:
            return pos[numpos - 1] == pathlen
        if ch == "*":
            numpos = pathlen - pos[0] + 1
            for i in range(1, numpos):
                pos[i] = pos[i - 1] + 1
        else:
            newnumpos = 0
            for i in range(numpos):
                if pos[i] < pathlen and path[pos[i]] == ch:
                    pos[newnumpos] = pos[i] + 1
                    newnumpos += 1
            numpos = newnumpos
            if numpos == 0:
                return False
    return True


def _match_priority(path, pattern):
    return len(pattern) if matches(path, pattern) else -1


def _is_ua_char(c):
    return ("a" <= c <= "z") or ("A" <= c <= "Z") or c in "-_"


def extract_user_agent(ua: str) -> str:
    i = 0
    while i < len(ua) and _is_ua_char(ua[i]):
        i += 1
    return ua[:i]


def is_valid_user_agent_to_obey(ua: str) -> bool:
    return len(ua) > 0 and extract_user_agent(ua) == ua


# ---------------------------------------------------------------- parsing
def _split_lines(body: bytes | str):
    if isinstance(body, str):
        body = body.encode("utf-8", "surrogateescape")
    bom = b"\xef\xbb\xbf"
    bom_pos = 0
    line = bytearray()
    prev_cr = False
    i = 0
    n = len(body)
    while i < n:
        ch = body[i]
        i += 1
        if bom_pos < 3:
            if ch == bom[bom_pos]:
                bom_pos += 1
                continue
            bom_pos = 3
        if ch != 0x0A and ch != 0x0D:
            if len(line) < K_MAX_LINE_LEN - 1:
                line.append(ch)
        else:
            is_crlf_continuation = (len(line) == 0 and prev_cr and ch == 0x0A)
            if not is_crlf_continuation:
                yield bytes(line).decode("utf-8", "surrogateescape")
            line = bytearray()
            prev_cr = ch == 0x0D
    yield bytes(line).decode("utf-8", "surrogateescape")


def _key_value(line: str):
    h = line.find("#")
    if h != -1:
        line = line[:h]
    line = line.strip(" \t\v\f")
    if not line:
        return None
    sep = line.find(":")
    if sep == -1:
        # accept whitespace instead of colon iff exactly two tokens
        ws = -1
        for j, c in enumerate(line):
            if c in " \t":
                ws = j
                break
        if ws == -1:
            return None
        val = line[ws:].lstrip(" \t")
        if any(c in " \t" for c in val):
            return None
        sep = ws
    key = line[:sep].strip(" \t\v\f")
    val = line[sep + 1:].strip(" \t\v\f")
    if not key:
        return None
    return key, val


def _key_type(key: str):
    k = key.lower()
    if k.startswith("user-agent") or k.startswith("useragent") or k.startswith("user agent"):
        return "ua"
    if k.startswith("allow"):
        return "allow"
    if k.startswith("disallow") or any(k.startswith(t) for t in
                                       ("dissallow", "dissalow", "disalow", "diasllow", "disallaw")):
        return "disallow"
    if k.startswith("sitemap") or k.startswith("site-map"):
        return "sitemap"
    return "unknown"


def parse(body):
    """Returns list of (type, value) records in file order; values of allow/disallow escaped."""
    recs = []
    for line in _split_lines(body):
        kv = _key_value(line)
        if not kv:
            continue
        t = _key_type(kv[0])
        v = kv[1]
        if t in ("allow", "disallow"):
            v = maybe_escape_pattern(v)
        recs.append((t, v))
    return recs


def parse_report(body):
    """Mirror of google/robotstxt's RobotsParseHandler callbacks for the reporting tests:
    counts valid directives (user-agent/allow/disallow/sitemap), unknown directives, the last
    line number that held a directive, and the sitemap value."""
    rep = {"valid": 0, "unknown": 0, "last_line": 0, "sitemap": ""}
    for line_num, line in enumerate(_split_lines(body), start=1):
        kv = _key_value(line)
        if not kv:
            continue
        t = _key_type(kv[0])
        if t == "unknown":
            rep["unknown"] += 1
            rep["last_line"] = line_num
            continue
        assert line_num >= rep["last_line"]
        rep["last_line"] = line_num
        rep["valid"] += 1
        if t == "sitemap":
            rep["sitemap"] += kv[1]
    return rep


class RobotsMatcher:
    """Mirrors RobotsMatcher state machine for one (robots, agents, url) query."""

    def __init__(self, records, user_agents):
        self.records = records
        self.agents = [a for a in user_agents]

    def allowed(self, url: str) -> bool:
        path = get_path_params_query(url)
        seen_global = seen_specific = ever_seen_specific = seen_sep = False
        allow_g = allow_s = dis_g = dis_s = -1
        if not self.records:
            return True

        def seen_any():
            return seen_global or seen_specific

        def handle(kind, value, path, recurse=True):
            nonlocal seen_sep, allow_g, allow_s, dis_g, dis_s
            if not seen_any():
                return
            seen_sep = True
            pr = _match_priority(path, value)
            if pr >= 0:
                if seen_specific:
                    if kind == "allow":
                        allow_s = max(allow_s, pr)
                    else:
                        dis_s = max(dis_s, pr)
                else:
                    if kind == "allow":
                        allow_g = max(allow_g, pr)
                    else:
                        dis_g = max(dis_g, pr)
            elif kind == "allow" and recurse:
                sl = value.rfind("/")
                if sl != -1 and value[sl:].startswith("/index.htm"):
                    handle("allow", value[:sl + 1] + "$", path, recurse=False)

        for t, v in self.records:
            if t == "ua":
                if seen_sep:
                    seen_specific = seen_global = seen_sep = False
                if len(v) >= 1 and v[0] == "*" and (len(v) == 1 or v[1].isspace()):
                    seen_global = True
                else:
                    tok = extract_user_agent(v).lower()
                    for a in self.agents:
                        if tok == a.lower():
                            ever_seen_specific = seen_specific = True
                            break
            elif t in ("allow", "disallow"):
                handle(t, v, path)

        if allow_s > 0 or dis_s > 0:
            return not (dis_s > allow_s)
        if ever_seen_specific:
            return True
        if dis_g > 0 or allow_g > 0:
            return not (dis_g > allow_g)
        return True


def is_user_agent_allowed(robots_body, user_agent: str, url: str) -> bool:
    return RobotsMatcher(parse(robots_body), [user_agent]).allowed(url)


class Robots:
    """Parsed robots.txt for one host with Google's HTTP-status semantics."""

    def __init__(self, body=b"", status=200, agent="seo-intel"):
        self.status = status
        self.agent = extract_user_agent(agent) or "seo-intel"
        self.mode = "parse"
        if status is None or status == 429 or status >= 500:
            self.mode = "disallow_all"
        elif 400 <= status < 500:
            self.mode = "allow_all"
        body = (body or b"")[:500 * 1024]
        self.records = parse(body) if self.mode == "parse" else []
        self.sitemaps = [v for t, v in self.records if t == "sitemap"]
        self._m = RobotsMatcher(self.records, [self.agent])

    def allowed(self, url: str) -> bool:
        if self.mode == "allow_all":
            return True
        if self.mode == "disallow_all":
            return False
        return self._m.allowed(url)
