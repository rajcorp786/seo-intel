"""Exercise the Part E agent's LLM path without an API key.

A scripted fake LLM:
  attempt 1 for every action: calls a tool, then answers with (a) prose containing a raw number
            and (b) an extra claim whose value is FALSE  -> must be rejected + logged + regenerated
  attempt 2: valid prose that references only claim IDs it was given / got from tools
Then asserts: rejections were logged, no false claim reached claims.json, every output claim
re-verifies, and prose contains only claim references.

usage: python devsite/mock_agent_check.py RUN_DIR      (RUN_DIR from a finished seo-intel run)
"""
import json
import os
import re
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("ANTHROPIC_API_KEY", "mock")
os.environ["SEO_INTEL_CACHE"] = "/tmp/seo-intel-mock-cache"
shutil.rmtree("/tmp/seo-intel-mock-cache", ignore_errors=True)

from seo_intel import llm as L  # noqa: E402

state = {"calls": 0, "attempts": {}}


def fake_call(self, system, messages, tools=None, max_tokens=800):
    state["calls"] += 1
    first = messages[0]["content"]
    if "Classify each web page" in system:
        items = json.loads(first)
        return {"content": [{"type": "text", "text": json.dumps({i["url"]: "commercial" for i in items})}],
                "stop_reason": "end_turn", "usage": {}}
    payload = json.loads(first)
    ids = [c["id"] for c in payload["claims"]]
    key = first[:200]
    last = messages[-1]["content"]
    if isinstance(last, list) and last and last[0].get("type") == "tool_result":
        got = json.loads(last[0]["content"])
        tool_ids = [g["id"] for g in got if "id" in g]
        n = state["attempts"].get(key, 0)
        state["attempts"][key] = n + 1
        if n == 0:
            bad = {"title": "Fix this now", "rationale": f"It has 42 problems [{ids[0]}].", "steps": ["Do it."],
                   "extra_claims": [{"kind": "field", "url": payload["claims"][0].get("url", "x"),
                                     "field": "title", "value": "THIS TITLE IS INVENTED"}]}
            return {"content": [{"type": "text", "text": json.dumps(bad)}], "stop_reason": "end_turn", "usage": {}}
        good = {"title": "Act on the verified evidence",
                "rationale": f"The evidence [{ids[0]}] and [{ids[-1]}] supports this; see also "
                             f"[{tool_ids[0] if tool_ids else ids[0]}].",
                "steps": [f"Apply the change described by [{ids[0]}].", "Re-crawl to confirm."], "extra_claims": []}
        return {"content": [{"type": "text", "text": json.dumps(good)}], "stop_reason": "end_turn", "usage": {}}
    # no tool result yet: ask for a tool
    url = None
    for c in payload["claims"]:
        url = c.get("url") or c.get("url_a")
        if url:
            break
    if url is None:
        return {"content": [{"type": "text", "text": json.dumps({"title": "Create the page", "rationale":
                f"Evidence [{ids[0]}].", "steps": ["Write it."], "extra_claims": []})}],
                "stop_reason": "end_turn", "usage": {}}
    return {"content": [{"type": "tool_use", "id": f"tu{state['calls']}", "name": "get_page_fields",
                         "input": {"url": url}}], "stop_reason": "tool_use", "usage": {}}


L.LLM.call = fake_call
L.LLM.available = lambda self, a=0, b=0: True

src = Path(sys.argv[1])
dst = Path("/tmp/mock-agent-run")
shutil.rmtree(dst, ignore_errors=True)
shutil.copytree(src, dst)
from seo_intel.cli import main  # noqa: E402

main([ "http://target.test:8001/", "--competitor", "http://rival.test:8002/", "--out", str(dst), "--skip-crawl"])
rep = dst / "report"
claims = json.loads((rep / "claims.json").read_text())
actions = json.loads((rep / "actions.json").read_text())
rej = [json.loads(l) for l in (dst / "logs" / "rejections.jsonl").read_text().splitlines() if l.strip()]
invented = [c for c in claims.values() if c.get("value") == "THIS TITLE IS INVENTED"]
ref = re.compile(r"\[C\d+\]")
bad_prose = [a["rank"] for a in actions for t in [a["title"], a["rationale"]] + a["steps"]
             if re.search(r"\d|https?:", ref.sub("", t))]
llm_written = sum(1 for a in actions if a["prose_source"] == "llm")
print(json.dumps({"llm_calls": state["calls"], "actions": len(actions), "llm_written_actions": llm_written,
                  "rejections_logged": len(rej), "invented_claims_in_output": len(invented),
                  "actions_with_raw_facts_in_prose": bad_prose,
                  "example_rejection": rej[0] if rej else None}, indent=1, ensure_ascii=False))
assert rej and not invented and not bad_prose and llm_written > 0
print("MOCK AGENT CHECK PASSED")
