import numpy as np

from seo_intel import dedup, linkopt, soft404
from seo_intel.extract import TemplateModel, extract
from seo_intel.parse import parse_page, to_tree
from seo_intel.textnorm import phonetic, script_profile, tokens
from seo_intel.traps import TrapDetector
from seo_intel.urlnorm import normalize, template


def test_normalize_strips_session_tracking_fragment():
    u, sess = normalize("HTTP://Example.COM:80/a//b/?utm_source=x&id=3&PHPSESSID=abc#frag")
    assert u == "http://example.com/a/b/?id=3" and sess == "phpsessid"
    u, sess = normalize("/x;jsessionid=123?y=1", "https://ex.com/a/")
    assert u == "https://ex.com/x?y=1" and sess == "jsessionid"


def test_traps():
    t = TrapDetector()
    assert t.check("https://e.com/shop?color=red&size=m&sort=new")[0] == "facet"
    assert t.check("https://e.com/a/b/a/b/c")[0] == "path"
    assert t.check("https://e.com/x/x/x/")[0] == "path"
    assert t.check("https://e.com/blog/page/2") is None
    assert t.check("https://e.com/events?month=2024-01", 1) is None
    assert t.check("https://e.com/events?month=2024-02", 2) is None
    assert t.check("https://e.com/events?month=2024-04", 4)[0] == "calendar"
    hits = [t.observe(f"https://e.com/item?id={i}", "same") for i in range(14)]
    assert [h for h in hits if h][0][0] == "template"
    assert t.check("https://e.com/item?id=99")[0] == "template"


def test_hindi_hinglish_tokens():
    assert tokens("लिफ्ट की जांच।") == ["लिफ्ट", "की", "जांच"]  # matras kept, danda split
    assert phonetic("achha") == phonetic("acha") == phonetic("accha")
    assert script_profile("लिफ्ट की नियमित जांच बहुत जरूरी है") == "hi"
    assert script_profile("lift ki maintenance har mahine karni chahiye kyunki safety hai") == "hinglish"


def test_minhash_estimates_jaccard():
    mh = dedup.MinHasher(1)
    a = " ".join(f"w{i}" for i in range(400))
    b = " ".join(f"w{i}" for i in range(100, 500))
    sa, sb = mh.signature(a), mh.signature(b)
    A, B = mh.shingles(a), mh.shingles(b)
    true = len(A & B) / len(A | B)
    assert abs(dedup.est_jaccard(sa, sb) - true) < 0.12
    pairs, _ = dedup.lsh_pairs({"a": sa, "b": sb, "c": mh.signature("totally different words here " * 30)}, 0.35)
    assert [(p[0], p[1]) for p in pairs] == [("a", "b")]


def test_template_extraction_removes_boilerplate():
    pages = []
    for i in range(8):
        pages.append(to_tree(f"<html><body><nav><a href=/>Home</a><a href=/b>Blog</a></nav>"
                             f"<main><h1>Title {i}</h1><p>Unique body text number {i} about lifts and safety.</p></main>"
                             f"<footer>Copyright Foo Ltd <a href=/p>Privacy</a></footer></body></html>"))
    tm = TemplateModel()
    tm.learn(pages)
    main, nav = extract(pages[3], tm.to_dict())
    assert "Unique body text number 3" in main and "Copyright" not in main and "Home" not in main


def test_parse_redirect_signals():
    f, _ = parse_page("<html><head><meta http-equiv='refresh' content='0;url=/new'><link rel=canonical href='/c'>"
                      "<script>location.replace('/js')</script></head><body></body></html>", "https://e.com/a")
    assert f["meta_refresh"] == "/new" and f["canonical"] == "/c" and f["js_redirect"] == "/js"


def test_soft404():
    p = {"main_text": "Sorry", "title": "Page not found | Shop", "h1": ""}
    assert soft404.classify(p, None, False)
    p = {"main_text": "A long article " * 50, "title": "How to fix a lift", "h1": "How to"}
    assert soft404.classify(p, None, False) is None


def test_greedy_close_to_optimal():
    r = linkopt.compare_small(seed=3, trials=4)
    assert r["greedy_min_share_of_optimal_gain"] > 0.9


def test_find_anchor_exact_substring():
    phrases = linkopt.target_phrases({"title": "Home Lift Price Guide | Brand", "h1": "Home lift price"}, [], [])
    res = linkopt.find_anchor("Intro words here. Many owners ask about the home lift price before they buy one.",
                              phrases, {"home", "lift", "price", "guide"})
    assert res and res[1] in res[0] and res[1].lower() == "home lift price"


def test_entity_pairs_are_healthy():
    from seo_intel.cannibal import common_title_words, healthy_reason
    pages = [{"title": f"{n} Review: Is This SEO Tool Worth It?", "h1": ""} for n in
             ["Ahrefs", "Serpstat", "Mangools", "Majestic", "Moz", "Semrush"]]
    pages += [{"title": f"{n} Statistics: How Many People Use {n}?", "h1": ""} for n in
              ["Claude", "Signal", "Hulu", "Uber", "Canva"]]
    common = common_title_words(pages)
    base = {"hreflang": [], "lang": None, "canonical": None}
    a = dict(base, **pages[0]); b = dict(base, **pages[1])
    assert healthy_reason("https://e.com/ahrefs-review", "https://e.com/serpstat-review", a, b, common)
    c = dict(base, title="Answer Engine Optimization (AEO): How to Win in AI Search", h1="")
    d = dict(base, title="Generative Engine Optimization (GEO): How to Win in AI Search", h1="")
    assert healthy_reason("https://e.com/aeo", "https://e.com/geo", c, d, common) is None


def test_offsite_marker_is_not_a_hop():
    from seo_intel.analysis import real_hops
    import json
    assert real_hops(json.dumps([["https://a.com/x", 301], ["https://b.com/y", "offsite"]])) == 1
    assert real_hops(json.dumps([["http://a.com/", 301], ["https://a.com/", 301]])) == 2


def test_anchor_rejects_headings_and_generic_words():
    ph = linkopt.target_phrases({"title": "Ad Intelligence Software | Brand", "h1": "Best Ad Intelligence Software"},
                                [], [])
    tw = {"intelligence", "ad"}
    # heading (no sentence ending) and generic single word -> rejected
    assert linkopt.find_anchor("6 Best Ad Intelligence Software to Outsmart the Competition\nNothing else here today.",
                               ph, tw) is None
    assert linkopt.find_anchor("We track the competition closely every single week.", ph, tw) is None
    r = linkopt.find_anchor("Teams now use ad intelligence software to study rival campaigns.", ph, tw)
    assert r and r[1].lower() == "ad intelligence software"


def test_section_template_groups_slugs():
    from seo_intel.urlnorm import section_template
    assert section_template("https://e.com/seo-tips") == section_template("https://e.com/link-building") == "/*"
    assert section_template("https://e.com/tools/keyword") == "/tools/*"


def test_js_redirect_only_unconditional():
    f, _ = parse_page("<html><head><script>document.getElementById('b').addEventListener('click',function(){"
                      "window.location.href='/';});</script></head><body></body></html>", "https://e.com/a")
    assert f["js_redirect"] is None
    f, _ = parse_page("<html><head><script>window.location.href='/new-page';</script></head></html>", "https://e.com/a")
    assert f["js_redirect"] == "/new-page"
    from seo_intel.parse import AUTH_NAV
    assert AUTH_NAV.search("/url?q=https://views.x.com/?sso_continue=1")
    assert not AUTH_NAV.search("/blog/new-post")


def test_limiter_backoff():
    import asyncio, time
    from seo_intel.fetcher import HostLimiter
    lim = HostLimiter(6)
    lim.penalize("h", "1")
    assert lim.rate_of("h") == 3

    async def go():
        t = time.monotonic()
        await lim.acquire("h")
        return time.monotonic() - t
    assert asyncio.run(go()) >= 0.9  # waited for Retry-After


def test_site_scope():
    from seo_intel.urlnorm import site_scope
    assert site_scope("www.example.com") == "example.com"
    assert site_scope("example.co.in") == "example.co.in"
    assert site_scope("docs.djangoproject.com") == "docs.djangoproject.com"
    assert site_scope("www.docs.example.com") == "docs.example.com"


def test_template_gate_no_deadlock_on_retries():
    t = TrapDetector()
    urls = [f"https://e.com/item?id={i}" for i in range(20)]
    for u in urls[:12]:
        assert t.gate(u) == "ok"
        t.dispatched(u)
    assert t.gate(urls[12]) == "wait"
    # all 12 samples failed with 429 and were never observed -> forced verdict, crawl continues
    assert t.force_judgement() is True
    assert t.gate(urls[12]) == "ok"


def test_schema_types_jsonld_microdata_og():
    from seo_intel.parse import parse_page
    html = ("<html><head><script type='application/ld+json'>{\"@context\":\"https://schema.org\",\"@graph\":"
            "[{\"@type\":\"WebSite\"},{\"@type\":[\"Product\",\"IndividualProduct\"],\"offers\":{\"@type\":\"Offer\"}}]}"
            "</script><script type='application/ld+json'>{broken json</script>"
            "<meta property='og:type' content='Product'></head><body>"
            "<div itemscope itemtype='https://schema.org/FAQPage'></div></body></html>")
    f, _ = parse_page(html, "https://e.com/x")
    assert f["schema"] == ["FAQPage", "IndividualProduct", "Offer", "Product", "WebSite", "og:product"]


def test_intent_uses_schema_types():
    from seo_intel.topics import classify_intent, intent_scores
    p = {"url": "https://e.com/lifts/model-x", "title": "Model X", "h1": "Model X", "main_text": "A lift.",
         "word_count": 200}
    assert classify_intent(intent_scores(p, [], 0, 0.0, ["Product", "Offer", "WebSite"]))[0] == "transactional"
    assert classify_intent(intent_scores(p, [], 0, 0.0, ["BlogPosting"]))[0] == "informational"
    assert classify_intent(intent_scores(p, [], 0, 0.0, ["ContactPage"]))[0] == "navigational"
    # site-wide types alone change nothing
    assert intent_scores(p, [], 0, 0.0, ["WebSite", "Organization", "BreadcrumbList"]) == intent_scores(p, [], 0, 0.0)


def test_home_lift_title_is_not_navigational():
    from seo_intel.topics import classify_intent, intent_scores
    p = {"url": "https://e.com/x/home-lift-price-basics/", "title": "Home lift price basics", "h1": None,
         "main_text": "", "word_count": 300}
    assert intent_scores(p, [], 0, 0.0)["navigational"] == 0
    q = dict(p, url="https://e.com/x/", title="Home | Target Lifts")
    assert classify_intent(intent_scores(q, [], 0, 0.0))[0] == "navigational"


def test_anchor_never_shared_between_sibling_pages_and_widen_fallback():
    from seo_intel import linkopt
    pages = [{"url": f"https://e.com/model-{i}/", "title": f"Home Lift Model {i}", "h1": None} for i in range(4)]
    needed = {("home", "lift"): pages[2]["url"], ("lift", "model", "2"): pages[2]["url"]}
    bad = linkopt.ambiguous_grams(needed, pages)
    assert ("home", "lift") in bad and ("lift", "model", "2") not in bad
    text = "Every home lift needs a pit and a yearly service plan here."
    phrases = [["home", "lift", "model", "2"]]
    assert linkopt.find_anchor(text, phrases, {"2"}, banned=bad) is None
    r = linkopt.find_anchor(text, phrases, {"2"}, banned=bad, rewrite_phrases=[("home", "lift", "model", "2")])
    assert r[1] == "home lift" and r[3] == "home lift model 2"
    assert r[4] == "Every home lift model 2 needs a pit and a yearly service plan here."


def test_limiter_settles_below_the_throttled_rate():
    from seo_intel.fetcher import HostLimiter
    lim = HostLimiter(6)
    lim.penalize("h")                      # throttled at 6 -> 3
    assert lim.rate_of("h") == 3
    for _ in range(10):                    # long quiet period: recovers, but never back to 6
        lim.last_penalty["h"] -= 31
        lim.rate_of("h")
    assert lim.rate_of("h") == 5


def test_url_series_groups_one_page_per_place_templates():
    from seo_intel.gaps import url_series
    cities = ["agra", "ajmer", "new-delhi", "pune", "surat", "kota", "goa", "jammu-and-kashmir", "mysore"]
    urls = [f"https://c.com/home-lifts-in-{c}/" for c in cities] + \
           [f"https://c.com/blog/{w}-guide-to-{v}-lift-maintenance-costs/" for w in ("a", "the", "our")
            for v in ("home", "goods", "car", "glass")] + ["https://c.com/about/"]
    s = url_series(urls)
    assert all(s[u] == ("home", "lifts", "in") for u in urls[:9])
    assert all(s[u] is None for u in urls[9:])
