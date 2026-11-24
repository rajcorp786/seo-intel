"""Synthetic dev sites for end-to-end tests (target + competitor) with every hard case:
robots rules, sitemaps (+lastmod), template nav/footer/sidebar, near-duplicates, cannibalizing
pages, hreflang Hindi versions, Hinglish pages, pagination, product+reviews, calendar trap,
faceted trap, session IDs, growing-path trap, redirect chain + loop, meta refresh, JS redirect,
X-Robots-Tag, canonical chain / to 404 / to redirect, soft 404s, JS-rendered pages.
Also writes gold main-content text for extraction F1.

usage: python devsite/server.py [--port-target 8001] [--port-rival 8002] [--gold gold.json] [--variant 0|1]
variant 1 = 'next day' changes (title change, canonical change, new page, removed page, new redirect).
"""
import argparse
import hashlib
import json
import random
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

TOPICS = {
    "maintenance": ("elevator maintenance", "lubrication inspection technician schedule brake rope wear "
                    "preventive checklist downtime contract callback logbook".split()),
    "safety": ("escalator safety", "handrail step comb plate emergency stop skirt brush passengers "
               "children shoes entrapment signage audit".split()),
    "homelift": ("home lift price", "residential villa budget cabin capacity installation cost quote "
                 "warranty compact pit shaft".split()),
    "install": ("elevator installation", "shaft civil work machine room rails guide permit commissioning "
                "load test drawings timeline crew".split()),
    "modern": ("lift modernization", "controller upgrade drive inverter energy door operator retrofit "
               "obsolete parts savings compliance".split()),
    "codes": ("elevator safety codes", "standard bureau regulation licence inspection authority certificate "
              "clause compliance fine registration".split()),
}
RIVAL_GAPS = {
    "hydraulic": ("hydraulic elevator", "piston cylinder oil pump valve jack pressure leakage hose "
                  "low-rise holeless reservoir".split()),
    "dumbwaiter": ("dumbwaiter lift", "kitchen restaurant food trolley hatch service counter small goods "
                   "hotel floors interlock".split()),
}
FILLER = ("the team should plan this carefully because buildings differ and owners want reliable results "
          "with clear costs and fewer surprises during the year while tenants expect smooth daily rides").split()
HINDI = ("लिफ्ट की नियमित जांच बहुत जरूरी है क्योंकि सुरक्षा सबसे पहले आती है । तकनीशियन हर महीने ब्रेक और रस्सी "
         "की स्थिति देखते हैं । भवन मालिक को अनुबंध में सभी सेवाओं की सूची रखनी चाहिए । ").split()
HINGLISH = ("lift ki maintenance har mahine karni chahiye kyunki safety sabse zaroori hai aur technician brake "
            "aur rope ko check karta hai taaki koi problem na ho").split()


def para(rng, words, n):
    out = []
    for _ in range(n):
        k = rng.randint(14, 26)
        s = [rng.choice(words) if rng.random() < 0.45 else rng.choice(FILLER) for _ in range(k)]
        out.append(" ".join(s).capitalize() + ".")
    return " ".join(out)


def article(seed, topic_words, name, n_par):
    rng = random.Random(seed)
    words = topic_words + name.split()
    return [para(rng, words, rng.randint(3, 6)) for _ in range(n_par)]


class Site:
    def __init__(self, name, port, rival=False, variant=0):
        self.name, self.port, self.rival, self.variant = name, port, rival, variant
        self.host = f"{name}.test:{port}"
        self.pages = {}   # path -> dict(title, h1, paras, extra_head, links)
        self.special = {}
        self.gold = {}
        self.build()

    def u(self, path):
        return f"http://{self.host}{path}"

    def add(self, path, title, paras, h1=None, head="", lastmod="2024-05-01", lang="en", body_extra=""):
        self.pages[path] = dict(title=title, h1=h1 or title, paras=paras, head=head, lastmod=lastmod,
                                lang=lang, body_extra=body_extra)
        self.gold[self.u(path)] = "\n".join([h1 or title] + paras)

    def build(self):
        topics = dict(TOPICS)
        if self.rival:
            topics = {k: v for k, v in TOPICS.items() if k in ("maintenance", "safety", "homelift", "install")}
            topics.update(RIVAL_GAPS)
        self.slugs = []
        for ti, (tk, (tname, tw)) in enumerate(sorted(topics.items())):
            n = 10 if not self.rival else (9 if tk in RIVAL_GAPS else 6)
            for i in range(n):
                slug = f"{tname.replace(' ', '-')}-{['guide', 'tips', 'checklist', 'faq', 'basics', 'explained', 'cost', 'mistakes', 'trends', 'case-study'][i]}"
                title = f"{tname.title()} {slug.split('-')[-1].replace('case', 'case').title()} | {self.name.title()}"
                paras = article(hash_seed(self.name + slug), tw, tname, 8 if tk in RIVAL_GAPS else 5)
                self.add(f"/blog/{slug}/", title, paras, h1=f"{tname.title()}: {slug.split('-')[-1]}")
                self.slugs.append((tk, slug))
        if not self.rival:
            # near-duplicate pair + cannibalizing pair
            base = self.pages["/blog/elevator-maintenance-tips/"]
            p2 = [x.replace("team", "crew") for x in base["paras"]]
            self.add("/blog/elevator-maintenance-tips-2/", "Elevator Maintenance Tips (Updated) | Target", p2,
                     h1="Elevator maintenance: tips")
            hp = TOPICS["homelift"][1]
            self.add("/home-lift-price-guide/", "Home Lift Price Guide | Target",
                     article(7, hp + ["price", "price", "cost"], "home lift price", 5), h1="Home lift price guide")
            self.add("/best-home-lift-price/", "Best Home Lift Price in India | Target",
                     article(8, hp + ["price", "best", "cost"], "home lift price", 5), h1="Best home lift price")
            # products + reviews + category
            prods = []
            for i in range(12):
                pp = f"/products/home-lift-model-{i}/"
                prods.append(pp)
                self.add(pp, f"Home Lift Model {i} - Buy Now | Target",
                         [f"Add to cart. Price ₹ {400000 + i * 25000}. In stock with free installation."] +
                         article(100 + i, hp, f"home lift model {i}", 2), h1=f"Home Lift Model {i}")
                self.add(pp + "reviews/", f"Home Lift Model {i} Reviews | Target",
                         article(200 + i, hp + ["review", "rating", "customer"], f"home lift model {i}", 2),
                         h1=f"Home Lift Model {i} reviews")
            self.add("/products/", "Home Lifts - All Models | Target",
                     article(300, hp, "home lift models", 1), h1="All home lift models")
            self.prods = prods
            # Hindi hreflang versions
            for tk, slug in self.slugs[:6]:
                hp_ = f"/hi/blog/{slug}/"
                rng = random.Random(hash_seed(slug))
                paras = [" ".join(rng.choice(HINDI) for _ in range(40)) for _ in range(4)]
                alt = (f'<link rel="alternate" hreflang="en" href="{self.u("/blog/" + slug + "/")}">'
                       f'<link rel="alternate" hreflang="hi" href="{self.u(hp_)}">')
                self.add(hp_, "लिफ्ट रखरखाव गाइड | Target", paras, h1="लिफ्ट रखरखाव", head=alt, lang="hi")
                self.pages[f"/blog/{slug}/"]["head"] += alt
            for i in range(4):
                rng = random.Random(500 + i)
                paras = [" ".join(rng.choice(HINGLISH) for _ in range(35)) for _ in range(4)]
                self.add(f"/hinglish/lift-maintenance-kaise-kare-{i}/", f"Lift maintenance kaise kare {i} | Target",
                         paras, h1=f"Lift maintenance kaise kare {i}")
            # canonical cases
            a = article(900, TOPICS["codes"][1], "code amp", 3)
            self.add("/blog/codes-amp/", "Codes AMP | Target", a,
                     head=f'<link rel="canonical" href="{self.u("/blog/codes-print/")}">')
            self.add("/blog/codes-print/", "Codes Print | Target", a,
                     head=f'<link rel="canonical" href="{self.u("/blog/elevator-safety-codes-guide/")}">')
            self.add("/blog/stale-post/", "Stale | Target", article(901, FILLER, "stale", 2),
                     head=f'<link rel="canonical" href="{self.u("/blog/gone-post/")}">')
            self.add("/blog/canon-to-redirect/", "C2R | Target", article(902, FILLER, "c2r", 2),
                     head=f'<link rel="canonical" href="{self.u("/old-page")}">')
            # JS-rendered SPA pages
            for i in range(6):
                self.special[f"/app/feature-{i}/"] = ("spa", i)
                self.gold[self.u(f"/app/feature-{i}/")] = "\n".join(
                    [f"Smart Lift Feature {i}"] + article(600 + i, TOPICS["modern"][1], "smart lift feature", 3))
            self.add("/contact/", "Contact Us | Target", ["Call our office or send a message using the form."])
            self.add("/about/", "About Us | Target", article(700, FILLER, "about our company", 2))
            self.add("/private/public-note/", "Public note | Target", article(701, FILLER, "note", 1))
            self.add("/private-report/", "Report | Target", article(702, FILLER, "report", 2))
            if self.variant == 1:
                self.pages["/about/"]["title"] = "About Our Lift Company | Target"
                self.pages["/blog/stale-post/"]["head"] = f'<link rel="canonical" href="{self.u("/blog/stale-post/")}">'
                self.pages["/blog/stale-post/"]["lastmod"] = "2024-06-02"  # a CMS bumps <lastmod> on edit
                self.add("/blog/new-post-next-day/", "New Post | Target", article(703, FILLER, "new post", 3))
                del self.pages["/contact/"]
        else:
            self.add("/contact/", "Contact | Rival", ["Talk to our sales desk."])
            self.add("/about/", "About | Rival", article(710, FILLER, "about rival", 2))
        self.blog_paths = sorted(p for p in self.pages if p.startswith("/blog/") and p.count("/") == 3)

    # ------------------------------------------------------------- html
    def nav(self):
        items = [("/", "Home"), ("/blog/", "Blog"), ("/products/", "Products"), ("/about/", "About"), ("/contact/", "Contact")]
        return "<header class='site-header'><div class='logo'>" + self.name.title() + "</div><nav class='main-nav'><ul>" + \
            "".join(f"<li><a href='{h}'>{t}</a></li>" for h, t in items) + "</ul></nav></header>"

    def sidebar(self, path):
        rng = random.Random(hash_seed(path))
        picks = rng.sample(self.blog_paths, min(5, len(self.blog_paths)))
        return "<aside class='sidebar'><h3>Related posts</h3><ul>" + "".join(
            f"<li><a href='{p}'>{self.pages[p]['h1']}</a></li>" for p in picks) + "</ul></aside>"

    def footer(self):
        return ("<footer class='site-footer'><p>Copyright 2024 " + self.name.title() + " Lifts Pvt Ltd. All rights reserved."
                "</p><a href='/about/'>About</a> <a href='/privacy/'>Privacy</a> <a href='/terms/'>Terms</a>"
                "<p>Subscribe to our newsletter for monthly lift tips.</p></footer>")

    def jsonld(self, path, p):
        """Structured data like a real CMS: a site-wide @graph on every page plus a page type."""
        graph = [{"@type": "WebSite", "name": self.name}, {"@type": "Organization", "name": self.name},
                 {"@type": "BreadcrumbList", "itemListElement": []}]
        if path.startswith("/blog/") and path.count("/") == 3:
            graph.append({"@type": "BlogPosting", "headline": p["h1"]})
        elif path.startswith("/products/") and path.count("/") == 3:
            graph.append({"@type": "Product", "name": p["h1"], "offers": {"@type": "Offer", "priceCurrency": "INR"},
                          "review": [{"@type": "Review"}]})
        elif path == "/about/":
            graph.append({"@type": "AboutPage"})
        return "<script type='application/ld+json'>" + json.dumps({"@context": "https://schema.org", "@graph": graph}) + "</script>"

    def page_html(self, path, p, extra_links=""):
        body = f"<h1>{p['h1']}</h1>" + "".join(f"<p>{x}</p>" for x in p["paras"])
        # in-content links: link to two related posts with descriptive anchors
        if path.startswith("/blog/") and self.blog_paths:
            rng = random.Random(hash_seed("c" + path))
            tgt = rng.choice(self.blog_paths)
            body += f"<p>Read our <a href='{tgt}'>{self.pages[tgt]['h1'].lower()}</a> next.</p>"
        return (f"<!doctype html><html lang='{p['lang']}'><head><meta charset='utf-8'><title>{p['title']}</title>"
                f"{p['head']}{self.jsonld(path, p)}</head><body>{self.nav()}<div class='wrap'><main class='content'><article>{body}"
                f"</article>{extra_links}</main>{self.sidebar(path)}</div>{p['body_extra']}{self.footer()}</body></html>")

    def home(self):
        links = "".join(f"<li><a href='{p}'>{self.pages[p]['h1']}</a></li>" for p in self.blog_paths[:12])
        extra = ""
        if not self.rival:
            extra = ("<p>See the <a href='/old-page'>old guide</a>, <a href='/loop-a'>archive</a>, "
                     "<a href='/moved-meta'>moved</a>, <a href='/js-moved'>js moved</a>, "
                     "<a href='/events/calendar?month=2024-01'>events</a>, <a href='/shop'>shop</a>, "
                     "<a href='/loop/x/'>browse</a>, <a href='/private/secret/'>secret</a>, "
                     "<a href='/private-report/'>report</a>, <a href='/blog/?sessionid=" + "a" * 32 + "'>blog</a>, "
                     "<a href='/products/discontinued-item/'>old product</a>, "
                     "<a href='/blog/codes-amp/'>codes</a>, <a href='/blog/stale-post/'>stale</a>, "
                     "<a href='/blog/canon-to-redirect/'>c2r</a>, <a href='/hinglish/lift-maintenance-kaise-kare-0/'>"
                     "hinglish</a>, <a href='/blog/elevator-maintenance-tips-2/'>tips 2</a>, "
                     "<a href='/home-lift-price-guide/'>price guide</a>, <a href='/best-home-lift-price/'>best price</a>"
                     + "".join(f", <a href='/app/feature-{i}/'>feature {i}</a>" for i in range(6)) + "</p>")
        p = {"title": f"{self.name.title()} Lifts - Home", "h1": f"Welcome to {self.name.title()} Lifts",
             "paras": ["We design, install and maintain elevators and escalators."], "head": "", "lang": "en",
             "body_extra": ""}
        return self.page_html("/", p, f"<ul class='home-list'>{links}</ul>{extra}")

    def blog_index(self, n):
        per = 10
        items = self.blog_paths[(n - 1) * per:n * per]
        pages = (len(self.blog_paths) + per - 1) // per
        lst = "".join(f"<li><a href='{p}'>{self.pages[p]['h1']}</a></li>" for p in items)
        pager = " ".join(f"<a href='/blog/page/{i}/'>{i}</a>" for i in range(2, pages + 1))
        p = {"title": f"Blog page {n} | {self.name.title()}", "h1": "Blog", "paras": ["Latest articles."],
             "head": "", "lang": "en", "body_extra": ""}
        return self.page_html(f"/blog/page/{n}/", p, f"<ul>{lst}</ul><div class='pager'>{pager}</div>")


def hash_seed(s):
    return int(hashlib.md5(s.encode()).hexdigest()[:8], 16)


THROTTLE = {"on": False}


def make_handler(site: Site):
    throttled_once = set()
    lock = threading.Lock()
    counter = {"n": 0}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def send(self, code, body="", ctype="text/html; charset=utf-8", headers=None):
            b = body.encode("utf-8") if isinstance(body, str) else body
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(b)))
            etag = '"' + hashlib.md5(b).hexdigest() + '"'
            self.send_header("ETag", etag)
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(b)

        def do_GET(self):
            sp = urlsplit(self.path)
            path, q = sp.path, parse_qs(sp.query)
            s = site
            etag_req = self.headers.get("If-None-Match")
            if path == "/robots.txt":
                return self.send(200, "User-agent: *\nDisallow: /private/\nAllow: /private/public-note/\n"
                                      "Disallow: /*?print=1\n\nUser-agent: Googlebot\nDisallow: /nogoogle/\n\n"
                                      f"Sitemap: http://{s.host}/sitemap_index.xml\n", "text/plain")
            if path == "/sitemap_index.xml":
                return self.send(200, f"<?xml version='1.0'?><sitemapindex xmlns='http://www.sitemaps.org/schemas/sitemap/0.9'>"
                                      f"<sitemap><loc>http://{s.host}/sitemap-blog.xml</loc></sitemap>"
                                      f"<sitemap><loc>http://{s.host}/sitemap-pages.xml</loc></sitemap></sitemapindex>",
                                 "application/xml")
            if path in ("/sitemap-blog.xml", "/sitemap-pages.xml"):
                ps = [p for p in sorted(s.pages) if p.startswith("/blog/") == (path == "/sitemap-blog.xml")]
                return self.send(200, "<?xml version='1.0'?><urlset xmlns='http://www.sitemaps.org/schemas/sitemap/0.9'>" +
                                 "".join(f"<url><loc>http://{s.host}{p}</loc><lastmod>{s.pages[p]['lastmod']}</lastmod></url>"
                                         for p in ps) + "</urlset>", "application/xml")
            if THROTTLE.get("delay"):  # a slow site: fixed latency per request
                time.sleep(THROTTLE["delay"] / 1000)
            if THROTTLE.get("jitter"):  # real-internet-like random latency (0-300 ms)
                import time as _t
                _t.sleep(random.random() * 0.3)
            if THROTTLE["on"] and path != "/robots.txt":
                with lock:
                    counter["n"] += 1
                    n = counter["n"]
                if n % THROTTLE.get("every", 10):  # heavy throttling: most requests get 429 with a long Retry-After
                    return self.send(429, "Too Many Requests", "text/plain", headers={"Retry-After": "60"})
            if not s.rival and path.startswith("/products/home-lift-model-") and path.endswith("/") \
                    and "reviews" not in path:
                with lock:
                    first = path not in throttled_once
                    throttled_once.add(path)
                if first:  # Shopify-style rate limiting: first request to each product page gets 429
                    return self.send(429, "Too Many Requests", "text/plain", headers={"Retry-After": "1"})
            if path == "/":
                return self.cond(s.home())
            if path == "/blog/":
                return self.cond(s.blog_index(1))
            if path.startswith("/blog/page/"):
                n = int(path.strip("/").split("/")[-1])
                return self.cond(s.blog_index(n))
            if not s.rival:
                if path == "/old-page":
                    return self.send(301, "", headers={"Location": "/older-page"})
                if path == "/older-page":
                    return self.send(301, "", headers={"Location": "/blog/elevator-maintenance-guide/"})
                if path == "/loop-a":
                    return self.send(302, "", headers={"Location": "/loop-b"})
                if path == "/loop-b":
                    return self.send(302, "", headers={"Location": "/loop-a"})
                if path == "/moved-meta":
                    return self.send(200, "<html><head><meta http-equiv='refresh' content='0; url=/products/'>"
                                          "<title>Moved</title></head><body>Moved.</body></html>")
                if path == "/js-moved":
                    return self.send(200, "<html><head><title>Moving</title><script>window.location.href='/blog/';"
                                          "</script></head><body>Redirecting</body></html>")
                if path == "/events/calendar":
                    m = q.get("month", ["2024-01"])[0]
                    y, mo = map(int, m.split("-"))
                    nxt = f"{y + (mo // 12)}-{mo % 12 + 1:02d}"
                    prv = f"{y - (1 if mo == 1 else 0)}-{(mo - 2) % 12 + 1:02d}"
                    p = {"title": "Events | Target", "h1": "Events calendar", "paras": ["No events this month."],
                         "head": "", "lang": "en", "body_extra": ""}
                    return self.send(200, s.page_html(path, p, f"<a href='/events/calendar?month={nxt}'>Next</a> "
                                                                f"<a href='/events/calendar?month={prv}'>Prev</a>"))
                if path == "/shop":
                    colors, sizes, sorts = ["red", "blue", "green", "black"], ["s", "m", "l", "xl"], ["price", "new", "pop"]
                    cur = {k: v[0] for k, v in q.items()}
                    links = []
                    for c in colors:
                        for z in sizes:
                            for o in sorts:
                                d = dict(cur, color=c, size=z, sort=o)
                                links.append("<a href='/shop?" + "&".join(f"{k}={v}" for k, v in sorted(d.items())) + f"'>{c} {z} {o}</a>")
                    p = {"title": "Shop | Target", "h1": "Shop", "paras": ["Filter our accessories."], "head": "",
                         "lang": "en", "body_extra": ""}
                    return self.send(200, s.page_html(path, p, "<div class='facets'>" + " ".join(links) + "</div>"))
                if path.startswith("/loop/"):
                    p = {"title": "Browse | Target", "h1": "Browse", "paras": ["Browse the archive."], "head": "",
                         "lang": "en", "body_extra": ""}
                    return self.send(200, s.page_html(path, p, f"<a href='{path}x/'>deeper</a>"))
                if path == "/products/discontinued-item/":
                    return self.send(200, "<html><head><title>Product not found | Target</title></head><body>" + s.nav() +
                                     "<main><h1>Sorry, this product was not found</h1><p>It may have been removed.</p>"
                                     "</main>" + s.footer() + "</body></html>")
                if path.startswith("/app/feature-"):
                    i = s.special[path][1]
                    g = s.gold[s.u(path)].split("\n")
                    data = json.dumps({"h1": g[0], "paras": g[1:]})
                    return self.send(200, "<!doctype html><html><head><title>Smart Lift Feature | Target</title>"
                                     "</head><body>" + s.nav() + "<div id='root'></div><script>var d=" + data +
                                     ";var r=document.getElementById('root');var a=document.createElement('article');"
                                     "a.innerHTML='<h1>'+d.h1+'</h1>'+d.paras.map(function(p){return '<p>'+p+'</p>'}).join('');"
                                     "r.appendChild(a);</script><script src='/static/app.js'></script>" + s.footer() + "</body></html>")
                if path == "/static/app.js":
                    if THROTTLE.get("slow_js"):
                        import time as _t
                        _t.sleep(200)  # a sub-resource that never answers in time
                    return self.send(200, "console.log('app');", "application/javascript")
                if path == "/private-report/":
                    return self.send(200, s.page_html(path, s.pages[path]), headers={"X-Robots-Tag": "noindex, nofollow"})
            if path in s.pages:
                return self.cond(s.page_html(path, s.pages[path]))
            return self.send(404, "<html><head><title>404 Not Found</title></head><body><h1>404</h1></body></html>")

        def cond(self, html):
            b = html.encode("utf-8")
            etag = '"' + hashlib.md5(b).hexdigest() + '"'
            if self.headers.get("If-None-Match") == etag:
                self.send_response(304)
                self.send_header("ETag", etag)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(b)))
            self.send_header("ETag", etag)
            self.end_headers()
            self.wfile.write(b)
    return H


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port-target", type=int, default=8001)
    ap.add_argument("--port-rival", type=int, default=8002)
    ap.add_argument("--gold", default=None)
    ap.add_argument("--variant", type=int, default=0)
    ap.add_argument("--delay", type=int, default=0, help="fixed latency in ms per request (slow-site test)")
    ap.add_argument("--jitter", action="store_true", help="random 0-300 ms latency per request")
    ap.add_argument("--slow-js", action="store_true", help="JS sub-resource hangs for 200 s (render hang test)")
    ap.add_argument("--throttle", type=int, default=0, help="N: answer all but every N-th request with 429 (Retry-After: 60)")
    a = ap.parse_args()
    THROTTLE["on"] = a.throttle > 0
    THROTTLE["every"] = max(1, a.throttle)
    THROTTLE["slow_js"] = a.slow_js
    THROTTLE["jitter"] = a.jitter
    THROTTLE["delay"] = a.delay
    t = Site("target", a.port_target, variant=a.variant)
    r = Site("rival", a.port_rival, rival=True)
    if a.gold:
        json.dump({**t.gold, **r.gold}, open(a.gold, "w", encoding="utf-8"), ensure_ascii=False, indent=0)
    servers = [ThreadingHTTPServer(("127.0.0.1", a.port_target), make_handler(t)),
               ThreadingHTTPServer(("127.0.0.1", a.port_rival), make_handler(r))]
    for s in servers:
        threading.Thread(target=s.serve_forever, daemon=True).start()
    print(f"serving target on {a.port_target}, rival on {a.port_rival}", flush=True)
    threading.Event().wait()


if __name__ == "__main__":
    main()
