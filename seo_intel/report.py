"""Deterministic output files (report/). Timing/RAM/cost go to logs/ (not compared)."""
from __future__ import annotations

import csv
import json
from collections import Counter
from pathlib import Path


def _clean(o):
    if isinstance(o, float):
        return round(o, 6)
    if isinstance(o, dict):
        return {str(k): _clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_clean(x) for x in o]
    if hasattr(o, "item"):
        return _clean(o.item())
    return o


def dump(path, obj):
    Path(path).write_text(json.dumps(_clean(obj), ensure_ascii=False, sort_keys=True, indent=1) + "\n",
                          encoding="utf-8")


def crawl_summary(store, site):
    db = store.db
    st = Counter()
    for (s,) in db.execute("SELECT status FROM pages WHERE site=?", (site,)):
        st[str(s)] += 1
    html = store.count("SELECT COUNT(*) FROM pages WHERE site=? AND status=200 AND html IS NOT NULL", (site,))
    rendered = store.count("SELECT COUNT(*) FROM pages WHERE site=? AND rendered=1", (site,))
    reasons = Counter(r[0].split(":")[0] for r in db.execute(
        "SELECT render_reason FROM pages WHERE site=? AND rendered=1", (site,)) if r[0])
    traps = [dict(kind=k, pattern=p, example=e, hits=h) for k, p, e, h in db.execute(
        "SELECT kind, pattern, example, hits FROM traps WHERE site=? ORDER BY kind, pattern", (site,))]
    chains, loops, metas, jsr, xr, offsite = [], [], [], [], [], []
    for r in db.execute("SELECT url, status, final_url, chain, meta_refresh, js_redirect, x_robots FROM pages "
                        "WHERE site=? ORDER BY url", (site,)):
        ch = json.loads(r["chain"] or "[]")
        if ch:
            item = {"url": r["url"], "hops": [[u, s] for u, s in ch], "final_url": r["final_url"]}
            hops = sum(1 for _, s in ch if isinstance(s, int))
            item["redirects"] = hops
            if any(s == "loop" for _, s in ch):
                loops.append(item)
            elif hops >= 2:
                chains.append(item)
            if any(s == "offsite" for _, s in ch):
                offsite.append({"url": r["url"], "final_url": r["final_url"], "redirects": hops})
        if r["meta_refresh"] and r["meta_refresh"] != r["url"]:
            metas.append({"url": r["url"], "target": r["meta_refresh"]})
        if r["js_redirect"] and r["js_redirect"] != r["url"]:
            jsr.append({"url": r["url"], "target": r["js_redirect"]})
        if r["x_robots"]:
            xr.append({"url": r["url"], "x_robots_tag": r["x_robots"]})
    statuses = {r[0]: (r[1], r[2]) for r in db.execute("SELECT url, status, canonical FROM pages WHERE site=?", (site,))}
    canon = []
    for u, (s, c) in sorted(statuses.items()):
        if s != 200 or not c or c == u:
            continue
        chain = [u, c]
        seen = {u}
        while chain[-1] in statuses and statuses[chain[-1]][1] and statuses[chain[-1]][1] != chain[-1] \
                and chain[-1] not in seen and len(chain) < 10:
            seen.add(chain[-1])
            chain.append(statuses[chain[-1]][1])
        tgt = statuses.get(c)
        issue = None
        if tgt is None:
            issue = "canonical target not crawled"
        elif tgt[0] in (301, 302, 303, 307, 308):
            issue = "canonical points to a redirect"
        elif tgt[0] in (404, 410):
            issue = "canonical points to a 404/410"
        elif tgt[0] != 200:
            issue = f"canonical target status {tgt[0]}"
        elif len(chain) >= 3:
            issue = "canonical chain"
        if issue:
            canon.append({"url": u, "chain": chain, "issue": issue})
    soft = [{"url": u, "reason": r} for u, r in db.execute(
        "SELECT url, soft404_reason FROM pages WHERE site=? AND soft404=1 ORDER BY url", (site,))]
    # honest trap-budget measure: every fetched page whose URL template matches a detected trap
    from .urlnorm import template
    trap_tpls = {template(t["example"]) for t in traps if t["kind"] in ("calendar", "template", "path")}
    trap_fetches = sum(1 for (u,) in db.execute("SELECT url FROM pages WHERE site=?", (site,))
                       if template(u) in trap_tpls)
    requests = store.count("SELECT COUNT(*) FROM pages WHERE site=?", (site,))
    return {"pages_by_status": dict(sorted(st.items())), "html_pages": html,
            "rendered_pages": rendered, "rendered_share": round(rendered / html, 4) if html else 0,
            "render_reasons": dict(sorted(reasons.items())),
            "render_available": bool(store.get_stat(site, "render_available", 0)),
            "traps": traps, "trap_urls_avoided": sum(t["hits"] for t in traps if t["kind"] != "session"),
            "trap_pages_fetched": trap_fetches,
            "trap_budget_share": round(trap_fetches / requests, 4) if requests else 0.0,
            "redirect_chains": chains, "redirect_loops": loops, "redirects_leaving_site": offsite, "meta_refresh": metas, "js_redirects": jsr,
            "x_robots_tag": xr, "canonical_issues": canon, "soft_404": soft,
            "urls_blocked_by_robots": store.count("SELECT COUNT(*) FROM urls WHERE site=? AND state='blocked'", (site,)),
            "urls_discovered_not_fetched": store.count("SELECT COUNT(*) FROM urls WHERE site=? AND state='queued'", (site,)),
            "sitemap_urls": int(store.get_stat(site, "sitemap_urls"))}


def change_report(store, site):
    db = store.db
    prev = {r[0]: r for r in db.execute("SELECT url, status, title, canonical FROM prev_pages WHERE site=?", (site,))}
    if not prev:
        return {"note": "first run: no previous crawl to compare"}
    cur = {r[0]: r for r in db.execute("SELECT url, status, title, canonical FROM pages WHERE site=?", (site,))}
    redir = (301, 302, 303, 307, 308)
    return {
        "new": sorted(set(cur) - set(prev)),
        "removed": sorted(set(prev) - set(cur)),
        "title_changed": [{"url": u, "before": prev[u][2], "after": cur[u][2]} for u in sorted(set(cur) & set(prev))
                          if prev[u][1] == 200 and cur[u][1] == 200 and prev[u][2] != cur[u][2]],
        "canonical_changed": [{"url": u, "before": prev[u][3], "after": cur[u][3]} for u in sorted(set(cur) & set(prev))
                              if prev[u][1] == 200 and cur[u][1] == 200 and prev[u][3] != cur[u][3]],
        "newly_redirected": [{"url": u, "status": cur[u][1]} for u in sorted(set(cur) & set(prev))
                             if cur[u][1] in redir and prev[u][1] not in redir],
        "status_changed": [{"url": u, "before": prev[u][1], "after": cur[u][1]} for u in sorted(set(cur) & set(prev))
                           if prev[u][1] != cur[u][1] and not (cur[u][1] in redir and prev[u][1] not in redir)],
        "unchanged_reused": store.count("SELECT COUNT(*) FROM pages WHERE site=? AND unchanged=1", (site,)),
    }


def pages_csv(ctx, site, path):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["url", "status", "final_url", "title", "h1", "canonical", "intent", "word_count",
                    "pagerank_x1000", "click_depth", "content_inlinks", "soft404", "rendered"])
        for r in ctx.store.db.execute(
                "SELECT p.url, p.status, p.final_url, p.title, p.h1, p.canonical, p.word_count, p.soft404, p.rendered, "
                "g.pagerank, g.click_depth, g.content_inlinks FROM pages p LEFT JOIN graph g ON g.url=p.url "
                "WHERE p.site=? ORDER BY p.url", (site,)):
            it = ctx.intents.get(r[0], (None,))[0]
            w.writerow([r[0], r[1], r[2], r[3], r[4], r[5], it, r[6],
                        "" if r[9] is None else f"{r[9] * 1000:.4f}", r[10], r[11], r[7], r[8]])


def describe_claim(c):
    k = c["kind"]
    v = c.get("value")
    if k == "field":
        return f"{c['field']} of {c['url']} = {v!r}"
    if k == "graph":
        return f"{c['metric']} of {c['url']} = {v}"
    if k == "pair":
        return f"{c['metric']}({c['url_a']}, {c['url_b']}) = {v}"
    if k == "sentence":
        return f"sentence on {c['url']}: \"{v}\""
    if k == "anchor":
        return f"anchor text \"{v}\" in that sentence on {c['url']}"
    if k == "title_phrase":
        return f"phrase \"{v}\" is in the title/H1 of {c['url']}"
    if k == "chain":
        return f"{c['metric']} of {c['url']} = {' -> '.join(v or [])}"
    if k == "gap":
        return f"{c['metric']} of topic gap {c['gap_id']} = {v}"
    if k in ("pr_after", "pr_before"):
        return f"{'PageRank x1000 after plan' if k == 'pr_after' else 'PageRank x1000 now'} of {', '.join(c['urls'])} = {v}"
    return json.dumps(c, ensure_ascii=False)


def render_ref(text, claims):
    import re
    return re.sub(r"\[(C\d+)\]", lambda m: f"[{m.group(1)}]", text or "")


def action_plan_md(actions, claims, gaps, path):
    lines = ["# SEO action plan", "",
             "Every fact below is a reference to a machine-checked claim (see the claim table at the end "
             "and `claims.json`).", ""]
    for a in actions:
        lines.append(f"## {a['rank']}. {a['title']}")
        lines.append(f"*Type:* `{a['type']}` · *expected impact:* {a['impact']:.3f}")
        if a["type"] == "create_page":
            g = next((x for x in gaps if x["id"] == a["context"].get("gap")), None)
            if g:
                lines.append(f"*Topic terms:* {', '.join(g['topic_terms'])}")
        lines.append("")
        lines.append(render_ref(a["rationale"], claims))
        lines.append("")
        for s in a["steps"]:
            lines.append(f"- {render_ref(s, claims)}")
        lines.append("")
        lines.append("<details><summary>Evidence (verified claims)</summary>")
        lines.append("")
        for cid in a["claim_ids"]:
            lines.append(f"- **{cid}** {describe_claim(claims[cid])}")
        lines.append("")
        lines.append("</details>")
        lines.append("")
    lines += ["## Claims", "", "| ID | Claim |", "|---|---|"]
    for cid, c in claims.items():
        lines.append(f"| {cid} | {describe_claim(c).replace('|', '/')} |")
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_all(ctx, out, actions, claims, rejections):
    rep = Path(out) / "report"
    rep.mkdir(parents=True, exist_ok=True)
    st = ctx.store
    dump(rep / "crawl_summary.json", {s: crawl_summary(st, s) for s in ("target", "competitor")})
    dump(rep / "change_report.json", {s: change_report(st, s) for s in ("target", "competitor")})
    dump(rep / "duplicates.json", ctx.results.get("dedup", {}))
    dump(rep / "cannibalization.json", {"summary": ctx.results.get("cannibal", {}), "groups": ctx.groups,
                                        "top_pairs": sorted(ctx.pairs, key=lambda p: (-p["severity"], p["url_a"],
                                                                                      p["url_b"]))[:3000]})
    dump(rep / "gaps.json", {"summary": ctx.results.get("gaps", {}),
                             "definition": {"in_depth": "competitor pages>=5, median words>=400, total words>=3000, "
                                                        "cohesion>=0.35",
                                            "target_misses": "target pages<=1 and target words<20% of competitor "
                                                             "and best target cosine<0.55"},
                             "gaps": ctx.gaps})
    dump(rep / "link_plan.json", ctx.plan)
    dump(rep / "analysis_summary.json", {k: v for k, v in ctx.results.items() if k not in ("dedup",)})
    dump(rep / "actions.json", actions)
    dump(rep / "claims.json", claims)
    action_plan_md(actions, claims, ctx.gaps, rep / "action_plan.md")
    for s in ("target", "competitor"):
        pages_csv(ctx, s, rep / f"pages_{s}.csv")
    logs = Path(out) / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    with open(logs / "rejections.jsonl", "w", encoding="utf-8") as f:
        for r in rejections:
            f.write(json.dumps(_clean(r), ensure_ascii=False, default=str) + "\n")
