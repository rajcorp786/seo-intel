"""Part E: action-plan agent with code-verified claims.

Pipeline
 1. Code builds candidate actions from Parts A-D; every fact is a structured claim
    (claims.py) taken from the store.
 2. Candidates ranked by expected impact (formula per type, all in [0,1]; see IMPACT doc).
 3. For each of the top actions the LLM (if available) writes title / rationale / steps.
    It may call tools (get_page_fields, get_pair_metrics, get_graph_stats) that return
    new claims with IDs; it may also propose extra claims. Prose may contain ONLY claim
    references like [C12] for facts: a linter rejects digits, URLs, paths and quotes.
 4. Every claim is verified in code against the store. A failing claim is logged and the
    action regenerated once with the rejection fed back; if it still fails the extra
    claims are dropped (or the action falls back to deterministic template prose).
 5. Final pass re-verifies every claim referenced by the output; any failure drops the
    action. No unverified claim can reach the output files.

IMPACT (expected impact, 0-1)
  merge_301 / canonicalize / retarget_intent : 0.55*severity/100 + 0.45*PR-percentile(max of pair)
  add_internal_links : 0.35 + 0.5*min(1, relative PageRank gain of target)
  fix_redirect_chain : 0.25 + 0.4*min(1, internal links to the redirecting URL / 25) + 0.1*(hops>=2)
  fix_canonical_chain: 0.30 + 0.3*(canonical target not 200) + 0.1*(chain length>=3) + 0.2*PR-percentile
  create_page        : 0.30 + 0.45*gap_score/max_gap_score
  do_nothing         : 0.02 (only shown to document a healthy, excluded pair)
"""
from __future__ import annotations

import json
import re
from collections import defaultdict

import numpy as np

from .claims import Ledger, Verifier
from .llm import parse_json, text_of

TOP_N = 25
REF = re.compile(r"\[(C\d+)\]")
BAD_PROSE = re.compile(r"\d|https?:|www\.|\.[a-z]{2,4}\b/|/[a-z0-9\-_]+/|[\"“”]", re.I)


def lint(text, allowed_ids):
    if not isinstance(text, str):
        return False, "not text"
    refs = REF.findall(text)
    if any(r not in allowed_ids for r in refs):
        return False, f"unknown claim ids {sorted(set(r for r in refs if r not in allowed_ids))}"
    stripped = REF.sub("", text)
    m = BAD_PROSE.search(stripped)
    if m:
        return False, f"fact outside a claim reference: '{stripped[max(0, m.start() - 15):m.end() + 15]}'"
    return True, ""


class Agent:
    def __init__(self, ctx, log):
        self.ctx = ctx
        self.log = log
        self.L = Ledger()
        self.V = Verifier(ctx, log)
        self.trace = []

    # ------------------------------------------------------------ claims
    def claim(self, **c):
        """Create a claim with the value computed from the store (so it is true by construction)."""
        cid = self.L.add(c)
        return cid

    def fc(self, url, field):
        return self.claim(kind="field", url=url, field=field, value=self.ctx.page_field(url, field))

    def gc(self, url, metric):
        from .claims import _round
        return self.claim(kind="graph", url=url, metric=metric, value=_round(metric, self.ctx.graph_metric(url, metric)))

    def pc(self, a, b, metric):
        from .claims import _round
        return self.claim(kind="pair", url_a=a, url_b=b, metric=metric,
                          value=_round(metric, self.ctx.pair_metric(a, b, metric)))

    # ------------------------------------------------------------ candidates
    def candidates(self):
        ctx = self.ctx
        acts = []
        tprs = sorted(v["pagerank"] for u, v in ctx.graphinfo.items() if (ctx.page(u) or {"site": ""})["site"] == "target")

        def pct(u):
            if not tprs or u not in ctx.graphinfo:
                return 0.0
            return float(np.searchsorted(tprs, ctx.graphinfo[u]["pagerank"], side="right")) / len(tprs)

        # cannibalization
        for g in ctx.groups[:20]:
            p = g["primary"]
            pair = next((x for x in g["pairs"] if p in (x["url_a"], x["url_b"])), g["pairs"][0])
            o = pair["url_b"] if pair["url_a"] == p else pair["url_a"]
            ov = ctx.pair_metric(p, o, "content_overlap")
            from urllib.parse import urlsplit
            same_path = urlsplit(p).path == urlsplit(o).path
            typ = "merge_301" if ov >= 0.7 else ("canonicalize" if same_path else "retarget_intent")
            cl = [self.pc(o, p, "severity"), self.pc(o, p, "topic_similarity"), self.pc(o, p, "content_overlap"),
                  self.fc(p, "title"), self.fc(o, "title"), self.fc(p, "intent"), self.fc(o, "intent"),
                  self.gc(p, "pagerank_x1000"), self.gc(o, "pagerank_x1000"),
                  self.gc(p, "content_inlinks"), self.gc(o, "content_inlinks")]
            others = [u for u in g["urls"] if u not in (p, o)][:3]
            for u in others:
                cl.append(self.pc(u, p, "topic_similarity"))
            imp = 0.55 * g["severity"] / 100 + 0.45 * max(pct(p), pct(o))
            acts.append({"type": typ, "impact": imp, "urls": [p, o] + others, "claims": cl, "core": cl[:3],
                         "facts": {"keep": p, "other": o, "group": g["id"]}})
        # redirect chains
        rows = ctx.store.db.execute("SELECT url, chain FROM pages WHERE site='target' AND chain IS NOT NULL AND "
                                    "chain != '[]' ORDER BY url").fetchall()
        for u, ch in rows:
            hops = len(json.loads(ch))
            inl = ctx.graph_metric(u, "inlinks_to_redirect")
            if hops >= 2 or (hops == 1 and inl >= 10):
                cl = [self.fc(u, "redirect_hops"), self.claim(kind="chain", url=u, metric="redirect_chain",
                                                               value=ctx.chain(u, "redirect_chain")),
                      self.gc(u, "inlinks_to_redirect")]
                imp = 0.25 + 0.4 * min(1.0, inl / 25) + 0.1 * (hops >= 2)
                acts.append({"type": "fix_redirect_chain", "impact": imp, "urls": [u], "claims": cl, "core": cl,
                             "facts": {"url": u}})
        # canonical problems
        for r in ctx.store.db.execute("SELECT url, canonical FROM pages WHERE site='target' AND status=200 AND "
                                      "canonical IS NOT NULL AND canonical != url ORDER BY url").fetchall():
            u = r[0]
            ts = ctx.page_field(u, "canonical_target_status")
            chain = ctx.chain(u, "canonical_chain") or []
            if ts != 200 or len(chain) >= 3:
                cl = [self.fc(u, "canonical"), self.fc(u, "canonical_target_status")]
                if len(chain) >= 3:
                    cl.append(self.claim(kind="chain", url=u, metric="canonical_chain", value=chain))
                imp = 0.3 + 0.3 * (ts != 200) + 0.1 * (len(chain) >= 3) + 0.2 * pct(u)
                acts.append({"type": "fix_canonical_chain", "impact": imp, "urls": [u], "claims": cl, "core": cl,
                             "facts": {"url": u}})
        # link plan
        by_t = defaultdict(list)
        for l in ctx.plan.get("links", []):
            by_t[l["target"]].append(l)
        for t, ls in sorted(by_t.items()):
            cl = []
            for l in ls:
                cl.append(self.claim(kind="sentence", url=l["source"], value=l["sentence"]))
                cl.append(self.claim(kind="anchor", url=l["source"], sentence=l["sentence"], value=l["anchor"]))
                cl.append(self.pc(l["source"], t, "content_link_exists"))
            from .claims import _round
            b = self.claim(kind="pr_before", urls=[t], value=_round("pr_after", ctx.pr_before([t])))
            a = self.claim(kind="pr_after", urls=[t], value=_round("pr_after", ctx.pr_after([t])))
            before, after = ctx.pr_before([t]), ctx.pr_after([t])
            rel = (after - before) / before if before > 0 else 0
            acts.append({"type": "add_internal_links", "impact": 0.35 + 0.5 * min(1.0, rel), "urls": [t],
                         "claims": [b, a] + cl, "core": [b, a] + cl, "facts": {"target": t, "links": ls}})
        # gaps
        mx = max([g["score"] for g in ctx.gaps], default=1) or 1
        for g in ctx.gaps[:10]:
            cl = [self.claim(kind="gap", gap_id=g["id"], metric="competitor_pages",
                             value=ctx.gap_metric(g["id"], "competitor_pages")),
                  self.claim(kind="gap", gap_id=g["id"], metric="competitor_words_total",
                             value=ctx.gap_metric(g["id"], "competitor_words_total")),
                  self.claim(kind="gap", gap_id=g["id"], metric="target_pages", value=g["target_pages"]),
                  self.claim(kind="gap", gap_id=g["id"], metric="best_target_similarity",
                             value=round(g["best_target_similarity"], 3))]
            for u in g["competitor_example_urls"][:3]:
                cl.append(self.claim(kind="gap", gap_id=g["id"], metric="competitor_url", value=u))
            acts.append({"type": "create_page", "impact": 0.3 + 0.45 * g["score"] / mx, "urls": [],
                         "claims": cl, "core": cl[:4], "facts": {"gap": g["id"], "topic_terms": g["topic_terms"]}})
        # do nothing (healthy pair)
        ex = ctx.results.get("cannibal", {}).get("excluded_examples", {})
        for reason, (a, b) in sorted(ex.items())[:1]:
            if a in ctx.zi and b in ctx.zi:
                cl = [self.pc(a, b, "topic_similarity")]
                acts.append({"type": "do_nothing", "impact": 0.02, "urls": [a, b], "claims": cl, "core": cl,
                             "facts": {"reason": reason}})
        for a in acts:
            a["impact"] = round(float(a["impact"]), 6)
        acts.sort(key=lambda a: (-a["impact"], a["type"], a["urls"][0] if a["urls"] else a["facts"].get("gap", "")))
        return acts

    # ------------------------------------------------------------ prose
    def template(self, a):
        c = a["claims"]
        t = a["type"]
        f = a["facts"]
        if t == "merge_301":
            return ("Merge the overlapping page into the stronger one and redirect it permanently",
                    f"The two pages compete for the same intent [{c[5]}] [{c[6]}] with severity [{c[0]}], "
                    f"topic similarity [{c[1]}] and content overlap [{c[2]}]. The page to keep has PageRank "
                    f"[{c[7]}] and in-content inlinks [{c[9]}] versus [{c[8]}] and [{c[10]}].",
                    ["Move unique content from the weaker page into the page to keep.",
                     "Add a permanent redirect from the weaker page to the page to keep.",
                     "Update internal links that point to the weaker page."])
        if t == "canonicalize":
            return ("Canonicalize the duplicate variant to the main page",
                    f"Both URLs serve near-identical content (overlap [{c[2]}], similarity [{c[1]}]) for the same "
                    f"intent [{c[5]}]; severity [{c[0]}].",
                    ["Add a rel=canonical on the variant pointing to the main page.",
                     "Link internally only to the main page."])
        if t == "retarget_intent":
            return ("Re-target the weaker page to a distinct intent or keyword",
                    f"These pages have the same intent [{c[5]}] [{c[6]}] and a high topic similarity [{c[1]}] "
                    f"(severity [{c[0]}]) but low content overlap [{c[2]}], so they are separate pages fighting "
                    f"for one query. Titles: [{c[3]}] and [{c[4]}].",
                    ["Pick one primary keyword per page and rewrite title, H1 and intro accordingly.",
                     "Link from the weaker page to the stronger page with the shared keyword as anchor."])
        if t == "fix_redirect_chain":
            return ("Collapse the redirect chain and update internal links",
                    f"This URL redirects through [{c[0]}] hops [{c[1]}] and still receives internal links [{c[2]}].",
                    ["Point the first URL directly at the final destination.",
                     "Replace internal links with the final URL."])
        if t == "fix_canonical_chain":
            extra = f" The canonical chain is [{c[2]}]." if len(c) > 2 else ""
            return ("Fix the canonical so it points to a live, self-canonical page",
                    f"The canonical [{c[0]}] points to a target whose status is [{c[1]}].{extra}",
                    ["Set the canonical to the final, indexable URL.", "Make the target page self-canonical."])
        if t == "add_internal_links":
            k = len(f["links"])
            return ("Add the optimised in-content links to this priority page",
                    f"Adding the selected links raises its internal PageRank from [{c[0]}] to [{c[1]}]. Each link "
                    f"uses an existing sentence and anchor on the source page: " +
                    "; ".join(f"[{c[2 + 3 * i]}] anchor [{c[3 + 3 * i]}] (not linked yet [{c[4 + 3 * i]}])"
                              for i in range(k)) + ".",
                    ["Insert each link on the anchor text inside the quoted sentence.",
                     "Re-crawl and confirm the links render in the main content."])
        if t == "create_page":
            return ("Create a page for a topic the competitor covers in depth",
                    f"The competitor has [{c[0]}] pages and [{c[1]}] words on this topic; the target has "
                    f"[{c[2]}] pages and its closest page only reaches similarity [{c[3]}]. Examples: "
                    + " ".join(f"[{x}]" for x in c[4:]) + ".",
                    ["Write a hub page covering the topic terms listed in the action.",
                     "Link it from related existing pages."])
        return ("No action: the similarity is legitimate",
                f"These pages are similar [{c[0]}] but the pair is a healthy pattern, so they should stay as they are.",
                ["Leave both pages unchanged."])

    def tool_defs(self):
        return [
            {"name": "get_page_fields", "description": "Verified fields for one crawled URL (status, title, h1, "
             "canonical, word_count, intent) plus graph stats. Returns claims with IDs.",
             "input_schema": {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]}},
            {"name": "get_pair_metrics", "description": "Verified similarity metrics for two crawled URLs. "
             "Returns claims with IDs.",
             "input_schema": {"type": "object", "properties": {"url_a": {"type": "string"}, "url_b": {"type": "string"}},
                              "required": ["url_a", "url_b"]}},
        ]

    def run_tool(self, name, args):
        ctx = self.ctx
        out = []
        try:
            if name == "get_page_fields":
                u = args["url"]
                if ctx.page(u) is None:
                    return [{"error": "url not in crawl"}]
                for f in ("status", "title", "h1", "canonical", "word_count", "intent"):
                    out.append(self.fc(u, f))
                if u in ctx.graphinfo:
                    out += [self.gc(u, "pagerank_x1000"), self.gc(u, "click_depth"), self.gc(u, "content_inlinks")]
            elif name == "get_pair_metrics":
                a, b = args["url_a"], args["url_b"]
                if a not in ctx.zi or b not in ctx.zi:
                    return [{"error": "url not in topic space"}]
                out += [self.pc(a, b, "topic_similarity"), self.pc(a, b, "content_overlap"),
                        self.pc(a, b, "title_overlap")]
        except (KeyError, TypeError) as e:
            return [{"error": str(e)}]
        return [self.view(cid) for cid in out]

    def view(self, cid):
        c = self.L.get(cid)
        v = {k: (x[:140] if isinstance(x, str) else x) for k, x in c.items()}
        return v

    def write(self, a):
        """LLM prose with tools; falls back to template. Returns (title, rationale, steps, claim_ids, source)."""
        llm = self.ctx.llm
        allowed = set(a["claims"])
        if not llm.available(4000, 700):
            t, r, s = self.template(a)
            return t, r, s, a["claims"], "template"
        system = ("You write one SEO action for a site owner. Facts may appear ONLY as claim references like [C12] "
                  "using the claim IDs given or returned by tools. Never write digits, URLs, paths or quoted page text "
                  "yourself. Be concrete and brief. Reply with JSON: {\"title\": str, \"rationale\": str, "
                  "\"steps\": [str, ...], \"extra_claims\": []}. extra_claims may hold claims you want to add in the "
                  "same schema as the given claims (without id); each will be checked against the crawl.")
        payload = {"action_type": a["type"], "context": {k: v for k, v in a["facts"].items() if k not in ("links",)},
                   "claims": [self.view(c) for c in a["claims"][:40]]}
        msgs = [{"role": "user", "content": json.dumps(payload, ensure_ascii=False)[:20000]}]
        feedback = None
        for attempt in range(2):
            m = list(msgs)
            if feedback:
                m = m + [{"role": "assistant", "content": "{}"}, {"role": "user", "content": feedback}]
            resp, tool_claims = self._tool_loop(system, m)
            if resp is None:
                break
            data = parse_json(text_of(resp))
            if not isinstance(data, dict):
                feedback = "Your reply was not valid JSON. Reply with the JSON object only."
                continue
            ids = allowed | tool_claims
            extra_ok, rejected = [], []
            for ec in (data.get("extra_claims") or [])[:5]:
                if not isinstance(ec, dict) or "kind" not in ec:
                    continue
                ec = {k: v for k, v in ec.items() if k != "id"}
                cid = self.L.add(ec)
                if self.V.verify(self.L.get(cid)):
                    extra_ok.append(cid)
                else:
                    rejected.append(cid)
            ids |= set(extra_ok)
            texts = [data.get("title", ""), data.get("rationale", "")] + list(data.get("steps") or [])
            bad = [lint(t, ids) for t in texts]
            refs_rejected = [r for t in texts if isinstance(t, str) for r in REF.findall(t) if r in rejected]
            if all(ok for ok, _ in bad) and not refs_rejected and data.get("title"):
                used = sorted(set(a["claims"]) | {r for t in texts for r in REF.findall(t)},
                              key=lambda x: int(x[1:]))
                self.trace.append({"action": a["type"], "attempt": attempt, "result": "ok"})
                return data["title"], data["rationale"], data.get("steps") or [], used, "llm"
            reasons = [why for ok, why in bad if not ok] + [f"claim {r} failed verification" for r in refs_rejected]
            self.log(f"REGENERATE {a['type']}: {reasons[:3]}")
            self.trace.append({"action": a["type"], "attempt": attempt, "result": "rejected", "why": reasons[:3]})
            feedback = "Rejected: " + "; ".join(reasons[:3]) + ". Fix it and reply with the JSON object only."
        t, r, s = self.template(a)
        return t, r, s, a["claims"], "template"

    def _tool_loop(self, system, msgs, rounds=2):
        llm = self.ctx.llm
        tools = self.tool_defs()
        tool_claims = set()
        for _ in range(rounds + 1):
            try:
                resp = llm.call(system, msgs, tools=tools, max_tokens=900)
            except ValueError as e:  # over the input-token cap
                self.log(f"LLM call skipped: {e}")
                return None, tool_claims
            if resp is None:
                return None, tool_claims
            uses = [b for b in resp["content"] if b.get("type") == "tool_use"]
            if not uses:
                return resp, tool_claims
            msgs = msgs + [{"role": "assistant", "content": resp["content"]}]
            results = []
            for u in uses[:3]:
                r = self.run_tool(u["name"], u.get("input") or {})
                tool_claims |= {x["id"] for x in r if "id" in x}
                results.append({"type": "tool_result", "tool_use_id": u["id"],
                                "content": json.dumps(r, ensure_ascii=False)[:3000]})
            msgs = msgs + [{"role": "user", "content": results}]
        return None, tool_claims

    # ------------------------------------------------------------ run
    def run(self):
        acts = self.candidates()
        final = []
        for a in acts:
            if len(final) >= TOP_N:
                break
            # core facts must verify before we spend an LLM call
            if not all(self.V.verify(self.L.get(c)) for c in a["core"]):
                self.log(f"DROP {a['type']} {a['urls'][:1]}: core claim failed")
                continue
            title, rationale, steps, ids, src = self.write(a)
            ok = all(self.V.verify(self.L.get(c)) for c in ids)
            texts_ok = all(lint(t, set(ids))[0] for t in [title, rationale] + list(steps))
            if not ok or not texts_ok:
                t, r, s = self.template(a)
                ids = a["claims"]
                if not all(self.V.verify(self.L.get(c)) for c in ids):
                    self.log(f"DROP {a['type']}: claims failed in final pass")
                    continue
                title, rationale, steps, src = t, r, s, "template"
            final.append({"rank": len(final) + 1, "type": a["type"], "impact": a["impact"], "title": title,
                          "rationale": rationale, "steps": steps, "claim_ids": ids, "prose_source": src,
                          "context": {k: v for k, v in a["facts"].items() if k != "links"}})
        used = sorted({c for f in final for c in f["claim_ids"]}, key=lambda x: int(x[1:]))
        claims = {c: self.L.get(c) for c in used}
        # last line of defence: re-verify everything that is output
        bad = [c for c in used if not self.V.verify(claims[c])]
        if bad:
            final = [f for f in final if not set(f["claim_ids"]) & set(bad)]
            used = sorted({c for f in final for c in f["claim_ids"]}, key=lambda x: int(x[1:]))
            claims = {c: self.L.get(c) for c in used}
            for i, f in enumerate(final):
                f["rank"] = i + 1
        return final, claims, self.V.rejections, self.trace
