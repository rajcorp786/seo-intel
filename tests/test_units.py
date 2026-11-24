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
