"""Port of google/robotstxt robots_test.cc (matching behaviour tests)."""
from seo_intel.robots import (get_path_params_query, is_user_agent_allowed as A,
                              is_valid_user_agent_to_obey, maybe_escape_pattern)


def test_system():
    r = "user-agent: FooBot\ndisallow: /\n"
    assert A("", "FooBot", "")
    assert A(r, "", "")
    assert not A(r, "FooBot", "")
    assert A("", "", "")


def test_line_syntax_line():
    url = "http://foo.bar/x/y"
    assert not A("user-agent: FooBot\ndisallow: /\n", "FooBot", url)
    assert A("foo: FooBot\nbar: /\n", "FooBot", url)
    assert not A("user-agent FooBot\ndisallow /\n", "FooBot", url)


def test_line_syntax_groups():
    r = ("allow: /foo/bar/\n\nuser-agent: FooBot\ndisallow: /\nallow: /x/\n"
         "user-agent: BarBot\ndisallow: /\nallow: /y/\n\n\nallow: /w/\n"
         "user-agent: BazBot\n\nuser-agent: FooBot\nallow: /z/\ndisallow: /\n")
    w, x, y, z = ("http://foo.bar/w/a", "http://foo.bar/x/b", "http://foo.bar/y/c", "http://foo.bar/z/d")
    foo = "http://foo.bar/foo/bar/"
    assert A(r, "FooBot", x)
    assert A(r, "FooBot", z)
    assert not A(r, "FooBot", y)
    assert A(r, "BarBot", y)
    assert A(r, "BarBot", w)
    assert not A(r, "BarBot", z)
    assert A(r, "BazBot", z)
    assert not A(r, "FooBot", foo)
    assert not A(r, "BarBot", foo)
    assert not A(r, "BazBot", foo)


def test_groups_other_rules():
    u = "http://foo.bar/"
    r = "User-agent: BarBot\nSitemap: https://foo.bar/sitemap\nUser-agent: *\nDisallow: /\n"
    assert not A(r, "FooBot", u)
    assert not A(r, "BarBot", u)
    r = "User-agent: FooBot\nInvalid-Unknown-Line: unknown\nUser-agent: *\nDisallow: /\n"
    assert not A(r, "FooBot", u)
    assert not A(r, "BarBot", u)


def test_rep_line_names_case_insensitive():
    ok, bad = "http://foo.bar/x/y", "http://foo.bar/a/b"
    for r in ("USER-AGENT: FooBot\nALLOW: /x/\nDISALLOW: /\n",
              "user-agent: FooBot\nallow: /x/\ndisallow: /\n",
              "uSeR-aGeNt: FooBot\nAlLoW: /x/\ndIsAlLoW: /\n"):
        assert A(r, "FooBot", ok)
        assert not A(r, "FooBot", bad)


def test_valid_user_agents_to_obey():
    assert is_valid_user_agent_to_obey("Foobot")
    assert is_valid_user_agent_to_obey("Foobot-Bar")
    assert is_valid_user_agent_to_obey("Foo_Bar")
    for bad in ("", "ツ", "Foobot*", " Foobot ", "Foobot/2.1", "Foobot Bar"):
        assert not is_valid_user_agent_to_obey(bad)


def test_user_agent_value_case_insensitive():
    ok, bad = "http://foo.bar/x/y", "http://foo.bar/a/b"
    for r in ("User-Agent: FOO BAR\nAllow: /x/\nDisallow: /\n",
              "User-Agent: foo bar\nAllow: /x/\nDisallow: /\n",
              "User-Agent: FoO bAr\nAllow: /x/\nDisallow: /\n"):
        for ua in ("Foo", "foo"):
            assert A(r, ua, ok)
            assert not A(r, ua, bad)


def test_global_groups_secondary():
    url = "http://foo.bar/x/y"
    assert A("", "FooBot", url)
    g = "user-agent: *\nallow: /\nuser-agent: FooBot\ndisallow: /\n"
    assert not A(g, "FooBot", url)
    assert A(g, "BarBot", url)
    s = "user-agent: FooBot\nallow: /\nuser-agent: BarBot\ndisallow: /\nuser-agent: BazBot\ndisallow: /\n"
    assert A(s, "QuxBot", url)


def test_allow_disallow_value_case_sensitive():
    lo, up = "http://foo.bar/x/y", "http://foo.bar/X/Y"
    r = "user-agent: FooBot\ndisallow: /x/\n"
    assert not A(r, "FooBot", lo)
    assert A(r, "FooBot", up)
    r = "user-agent: FooBot\ndisallow: /X/\n"
    assert A(r, "FooBot", lo)
    assert not A(r, "FooBot", up)


def test_longest_match():
    url = "http://foo.bar/x/page.html"
    h = "user-agent: FooBot\n"
    assert A(h + "disallow: /x/page.html\nallow: /x/page.html\n", "FooBot", url)
    assert A(h + "allow: /x/page.html\ndisallow: /x/page.html\n", "FooBot", url)
    assert A(h + "disallow: \nallow: \n", "FooBot", url)
    assert A(h + "allow: \ndisallow: \n", "FooBot", url)
    assert A(h + "disallow: /\nallow: /\n", "FooBot", url)
    assert A(h + "allow: /\ndisallow: /\n", "FooBot", url)
    r = h + "disallow: /x\nallow: /x/\n"
    assert not A(r, "FooBot", "http://foo.bar/x")
    assert A(r, "FooBot", "http://foo.bar/x/")
    assert not A(h + "disallow: /x/page.html\nallow: /x/\n", "FooBot", url)
    r = h + "allow: /page\ndisallow: /*.html\n"
    assert not A(r, "FooBot", "http://foo.bar/page.html")
    assert A(r, "FooBot", "http://foo.bar/page")
    r = h + "allow: /x/page.\ndisallow: /*.html\n"
    assert A(r, "FooBot", url)
    assert not A(r, "FooBot", "http://foo.bar/x/y.html")
    r = "User-agent: *\nDisallow: /x/\nUser-agent: FooBot\nDisallow: /y/\n"
    assert A(r, "FooBot", "http://foo.bar/x/page")
    assert not A(r, "FooBot", "http://foo.bar/y/page")


def test_encoding():
    h = "User-agent: FooBot\nDisallow: /\n"
    r = h + "Allow: /foo/bar?qux=taz&baz=http://foo.bar?tar&par\n"
    assert A(r, "FooBot", "http://foo.bar/foo/bar?qux=taz&baz=http://foo.bar?tar&par")
    r = h + "Allow: /foo/bar/ツ\n"
    assert A(r, "FooBot", "http://foo.bar/foo/bar/%E3%83%84")
    assert not A(r, "FooBot", "http://foo.bar/foo/bar/ツ")
    r = h + "Allow: /foo/bar/%E3%83%84\n"
    assert A(r, "FooBot", "http://foo.bar/foo/bar/%E3%83%84")
    assert not A(r, "FooBot", "http://foo.bar/foo/bar/ツ")
    r = h + "Allow: /foo/bar/%62%61%7A\n"
    assert not A(r, "FooBot", "http://foo.bar/foo/bar/baz")
    assert A(r, "FooBot", "http://foo.bar/foo/bar/%62%61%7A")


def test_special_characters():
    h = "User-agent: FooBot\n"
    r = h + "Disallow: /foo/bar/quz\nAllow: /foo/*/qux\n"
    assert not A(r, "FooBot", "http://foo.bar/foo/bar/quz")
    assert A(r, "FooBot", "http://foo.bar/foo/quz")
    assert A(r, "FooBot", "http://foo.bar/foo//quz")
    assert A(r, "FooBot", "http://foo.bar/foo/bax/quz")
    r = h + "Disallow: /foo/bar$\nAllow: /foo/bar/qux\n"
    assert not A(r, "FooBot", "http://foo.bar/foo/bar")
    assert A(r, "FooBot", "http://foo.bar/foo/bar/qux")
    assert A(r, "FooBot", "http://foo.bar/foo/bar/")
    assert A(r, "FooBot", "http://foo.bar/foo/bar/baz")
    r = h + "# Disallow: /\nDisallow: /foo/quz#qux\nAllow: /\n"
    assert A(r, "FooBot", "http://foo.bar/foo/bar")
    assert not A(r, "FooBot", "http://foo.bar/foo/quz")


def test_index_html_is_directory():
    r = "User-Agent: *\nAllow: /allowed-slash/index.html\nDisallow: /\n"
    assert A(r, "foobot", "http://foo.com/allowed-slash/")
    assert not A(r, "foobot", "http://foo.com/allowed-slash/index.htm")
    assert A(r, "foobot", "http://foo.com/allowed-slash/index.html")
    assert not A(r, "foobot", "http://foo.com/anyother-url")


def test_line_too_long():
    K = 2083 * 8
    eol = 1
    allow, disallow = "allow: ", "disallow: "
    r = "user-agent: FooBot\n"
    longline = "/x/"
    mx = K - len(longline) - len(disallow) + eol
    longline = longline + "a" * (mx - len(longline))
    r += disallow + longline + "/qux\n"
    assert A(r, "FooBot", "http://foo.bar/fux")
    assert not A(r, "FooBot", "http://foo.bar" + longline + "/fux")

    r = "user-agent: FooBot\ndisallow: /\n"
    la, lb = "/x/", "/x/"
    mx = K - len(la) - len(allow) + eol
    la = la + "a" * (mx - len(la))
    lb = lb + "b" * (mx - len(lb))
    r += allow + la + "/qux\n" + allow + lb + "/qux\n"
    assert not A(r, "FooBot", "http://foo.bar/")
    assert A(r, "FooBot", "http://foo.bar" + la + "/qux")
    assert A(r, "FooBot", "http://foo.bar" + lb + "/fux")


def test_accept_user_agent_up_to_first_space():
    r = "User-Agent: *\nDisallow: /\nUser-Agent: Foo Bar\nAllow: /x/\nDisallow: /\n"
    url = "http://foo.bar/x/y"
    assert A(r, "Foo", url)
    assert not A(r, "Foo Bar", url)


def test_bom_skipped():
    r = "﻿User-agent: FooBot\nDisallow: /\n"
    assert not A(r, "FooBot", "http://foo.bar/x")
    r = b"\xef\xbbUser-agent: FooBot\nDisallow: /\n"  # partial BOM also skipped
    assert not A(r, "FooBot", "http://foo.bar/x")


def test_crlf_and_cr_line_endings():
    for sep in ("\r\n", "\r"):
        r = sep.join(["User-agent: FooBot", "Disallow: /", "Allow: /x/", ""])
        assert A(r, "FooBot", "http://foo.bar/x/1")
        assert not A(r, "FooBot", "http://foo.bar/y")


def _doc(rules, cases):
    r = "user-agent: FooBot\n" + rules
    for url, exp in cases:
        assert A(r, "FooBot", "http://foo.bar" + url) == exp, (rules, url)


def test_documentation_checks():
    fish = [("/bar", False), ("/fish", True), ("/fish.html", True), ("/fish/salmon.html", True),
            ("/fishheads", True), ("/fishheads/yummy.html", True), ("/fish.html?id=anything", True),
            ("/Fish.asp", False), ("/catfish", False), ("/?id=fish", False)]
    _doc("disallow: /\nallow: /fish\n", fish)
    _doc("disallow: /\nallow: /fish*\n", fish)
    _doc("disallow: /\nallow: /fish/\n",
         [("/bar", False), ("/fish/", True), ("/fish/salmon", True), ("/fish/?salmon", True),
          ("/fish/salmon.html", True), ("/fish/?id=anything", True), ("/fish", False),
          ("/fish.html", False), ("/Fish/Salmon.html", False)])
    _doc("disallow: /\nallow: /*.php\n",
         [("/bar", False), ("/filename.php", True), ("/folder/filename.php", True),
          ("/folder/filename.php?parameters", True), ("/folder/any.php.file.html", True),
          ("/filename.php/", True), ("/", False), ("/windows.PHP", False)])
    _doc("disallow: /\nallow: /*.php$\n",
         [("/bar", False), ("/filename.php", True), ("/folder/filename.php", True),
          ("/filename.php?parameters", False), ("/filename.php/", False), ("/filename.php5", False),
          ("/php/", False), ("/filename?php", False), ("/aaaphpaaa", False), ("/windows.PHP", False)])
    _doc("disallow: /\nallow: /fish*.php\n",
         [("/bar", False), ("/fish.php", True), ("/fishheads/catfish.php?parameters", True),
          ("/Fish.PHP", False)])
    _doc("allow: /p\ndisallow: /\n", [("/page", True)])
    _doc("allow: /folder\ndisallow: /folder\n", [("/folder/page", True)])
    _doc("allow: /page\ndisallow: /*.htm\n", [("/page.htm", False)])
    _doc("allow: /$\ndisallow: /\n", [("/", True), ("/page.html", False)])


def test_get_path_params_query():
    cases = {
        "": "/", "http://www.example.com": "/", "http://www.example.com/": "/",
        "http://www.example.com/a": "/a", "http://www.example.com/a/": "/a/",
        "http://www.example.com/a/b?c=http://d.e/": "/a/b?c=http://d.e/",
        "http://www.example.com/a/b?c=d&e=f#fg": "/a/b?c=d&e=f",
        "example.com": "/", "example.com/": "/", "example.com/a": "/a", "example.com/a/": "/a/",
        "example.com/a/b?c=d&e=f#fg": "/a/b?c=d&e=f", "a": "/", "a/": "/", "/a": "/a",
        "a/b": "/b", "example.com?a": "/?a", "example.com/a;b#c": "/a;b", "//a/b/c": "/b/c",
    }
    for u, exp in cases.items():
        assert get_path_params_query(u) == exp, u


def test_maybe_escape_pattern():
    assert maybe_escape_pattern("http://www.example.com") == "http://www.example.com"
    assert maybe_escape_pattern("/a/b/c") == "/a/b/c"
    assert maybe_escape_pattern("/á") == "/%C3%A1"
    assert maybe_escape_pattern("%aa") == "%AA"
