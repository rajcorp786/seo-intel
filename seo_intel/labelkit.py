"""Labelling kit: one self-contained HTML page for the three human-label tasks in the brief.

  1. Main content (Part B F1): for 60 sampled pages, ALL text blocks of the raw page are shown
     (not our extraction, so the gold is not biased by it). Click the blocks that are main content.
  2. Recommendations (Part E judge): label each action good / bad.
  3. Cannibalization pairs (Part C precision / ablation): label each pair real / not.

The page saves progress in the browser and downloads gold.json, labels.csv, pair_labels.csv.
usage: seo-intel-eval label-kit RUN_DIR [RUN_DIR ...] --html label_kit.html
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from . import parse as P
from .extract import blocks
from .store import Store


def build(run_dirs, html_out, n_pages=60, n_pairs=60):
    pages, recs, pairs = [], [], []
    per_run = max(1, n_pages // max(1, len(run_dirs)))
    for rd in run_dirs:
        rd = Path(rd)
        st = Store(rd / "crawl.db")
        urls = [r[0] for r in st.db.execute(
            "SELECT url FROM pages WHERE status=200 AND html IS NOT NULL AND soft404=0 ORDER BY url")]
        urls = sorted(urls, key=lambda u: hashlib.sha1(u.encode()).hexdigest())[:per_run]
        for u in sorted(urls):
            tree = P.to_tree(st.html(u))
            if tree is None:
                continue
            bl = [b["text"] for b in blocks(tree) if len(b["text"]) >= 3][:400]
            if bl:
                pages.append({"run": rd.name, "url": u, "blocks": bl})
        st.close()
        ap = rd / "report" / "actions.json"
        cp = rd / "report" / "claims.json"
        if ap.exists() and cp.exists():
            acts = json.loads(ap.read_text(encoding="utf-8"))
            claims = json.loads(cp.read_text(encoding="utf-8"))
            from .report import describe_claim
            for a in acts:
                recs.append({"run": rd.name, "out_dir": str(rd), "rank": a["rank"], "type": a["type"],
                             "title": a["title"], "rationale": a["rationale"], "steps": a["steps"],
                             "evidence": [f"{c}: {describe_claim(claims[c])}" for c in a["claim_ids"][:14]
                                          if c in claims]})
        cj = rd / "report" / "cannibalization.json"
        if cj.exists() and len(pairs) < n_pairs:
            d = json.loads(cj.read_text(encoding="utf-8"))
            for p in d.get("top_pairs", [])[: n_pairs - len(pairs)]:
                pairs.append({"run": rd.name, "url_a": p["url_a"], "url_b": p["url_b"], "severity": p["severity"],
                              "topic_sim": p["topic_sim"], "content_overlap": p["content_overlap"]})
    data = {"pages": pages, "recs": recs, "pairs": pairs}
    html = TEMPLATE.replace("__DATA__", json.dumps(data, ensure_ascii=False).replace("</", "<\\/"))
    Path(html_out).write_text(html, encoding="utf-8")
    return {"pages": len(pages), "recommendations": len(recs), "pairs": len(pairs), "html": str(html_out)}


TEMPLATE = r"""<!doctype html><html><head><meta charset="utf-8"><title>seo-intel labelling kit</title>
<style>
body{font-family:Segoe UI,system-ui,sans-serif;margin:0;background:#f3f3f3;color:#181818}
header{background:#032d60;color:#fff;padding:12px 20px;display:flex;gap:16px;align-items:center;position:sticky;top:0;z-index:2}
header h1{font-size:17px;margin:0 16px 0 0}
.tab{background:transparent;border:1px solid #6b8fc4;color:#fff;padding:6px 12px;border-radius:4px;cursor:pointer}
.tab.on{background:#0176d3;border-color:#0176d3}
main{max-width:1000px;margin:18px auto;padding:0 16px}
.card{background:#fff;border:1px solid #ddd;border-radius:6px;padding:14px 16px;margin-bottom:14px}
.muted{color:#5c5c5c;font-size:13px}
.blk{padding:6px 8px;margin:3px 0;border:1px solid #e5e5e5;border-radius:4px;cursor:pointer;font-size:14px;line-height:1.4}
.blk.on{background:#d8f0dc;border-color:#2e844a}
.btn{padding:7px 14px;border-radius:4px;border:1px solid #0176d3;background:#fff;color:#0176d3;cursor:pointer;margin-right:6px}
.btn.good.on{background:#2e844a;color:#fff;border-color:#2e844a}.btn.bad.on{background:#ba0517;color:#fff;border-color:#ba0517}
.btn.primary{background:#0176d3;color:#fff}
.nav{display:flex;gap:8px;align-items:center;margin:10px 0}
ul{margin:6px 0 0 18px;padding:0}li{margin:2px 0;font-size:13px}
a{color:#0176d3;word-break:break-all}
</style></head><body>
<header><h1>seo-intel labelling kit</h1>
<button class="tab" data-t="pages">1. Main content</button>
<button class="tab" data-t="recs">2. Recommendations</button>
<button class="tab" data-t="pairs">3. Cannibalization pairs</button>
<span id="prog" class="muted" style="color:#cfe0f5;margin-left:auto"></span></header>
<main id="app"></main>
<script>
const D = __DATA__;
let S = {pages:{}, recs:{}, pairs:{}}, tab="pages", idx={pages:0,recs:0,pairs:0};
try { S = Object.assign(S, JSON.parse(localStorage.getItem("seo-intel-labels")||"{}")); } catch(e) {}
function save(){ try { localStorage.setItem("seo-intel-labels", JSON.stringify(S)); } catch(e) {} prog(); }
function key(o){ return o.run+"|"+(o.url||o.rank||(o.url_a+"|"+o.url_b)); }
function prog(){
  const p=D.pages.filter(x=>S.pages[key(x)]&&S.pages[key(x)].done).length,
        r=D.recs.filter(x=>S.recs[key(x)]!==undefined).length,
        q=D.pairs.filter(x=>S.pairs[key(x)]!==undefined).length;
  document.getElementById("prog").textContent=`pages ${p}/${D.pages.length} · recs ${r}/${D.recs.length} · pairs ${q}/${D.pairs.length}`;
}
function dl(name, text){ const a=document.createElement("a"); a.href=URL.createObjectURL(new Blob([text],{type:"text/plain"})); a.download=name; a.click(); }
function csvq(s){ s=String(s); return /[",\n]/.test(s) ? '"'+s.replace(/"/g,'""')+'"' : s; }
function nav(kind, n){ return `<div class="nav"><button class="btn" onclick="go('${kind}',-1)">◀ Prev</button>
  <span class="muted">${idx[kind]+1} / ${n}</span><button class="btn" onclick="go('${kind}',1)">Next ▶</button></div>`; }
function go(kind,d){ const n=D[kind].length; idx[kind]=Math.max(0,Math.min(n-1,idx[kind]+d)); render(); window.scrollTo(0,0); }
function render(){
  document.querySelectorAll(".tab").forEach(b=>b.classList.toggle("on", b.dataset.t===tab));
  const app=document.getElementById("app"); let h="";
  if(tab==="pages"){
    h+=`<div class="card"><b>Task:</b> click every block that is the page's <b>main content</b> (title/H1, article or product text). Leave menus, footer, sidebars, cookie banners, "related posts" unselected. Then press the blue <b>Done ✓</b> button.
    <div style="margin-top:8px"><button class="btn primary" onclick="exportGold()">Download gold.json</button></div></div>`;
    if(!D.pages.length){ app.innerHTML=h+"<p>No pages.</p>"; return; }
    const p=D.pages[idx.pages], st=S.pages[key(p)]||{sel:[],done:false};
    h+=nav("pages",D.pages.length)+`<div class="card"><div class="muted">${p.run}</div><a href="${p.url}" target="_blank">${p.url}</a>
      <div style="margin:8px 0"><button class="btn" onclick="allBlocks(true)">Select all</button><button class="btn" onclick="allBlocks(false)">Clear</button>
      <button class="btn primary" onclick="donePage()">Done ✓${st.done?" (saved)":""}</button></div>`;
    p.blocks.forEach((b,i)=>{ h+=`<div class="blk ${st.sel.includes(i)?"on":""}" onclick="tog(${i})">${b.replace(/</g,"&lt;")}</div>`; });
    h+="</div>"+nav("pages",D.pages.length);
  } else if(tab==="recs"){
    h+=`<div class="card"><b>Task:</b> would a senior SEO do this? <b>Good</b> = correct, specific and worth doing for this site. <b>Bad</b> = wrong, vague or not worth it.
    <div style="margin-top:8px"><button class="btn primary" onclick="exportRecs()">Download labels.csv</button></div></div>`;
    if(!D.recs.length){ app.innerHTML=h+"<p>No recommendations.</p>"; return; }
    const r=D.recs[idx.recs], v=S.recs[key(r)];
    h+=nav("recs",D.recs.length)+`<div class="card"><div class="muted">${r.run} · #${r.rank} · ${r.type}</div><h3 style="margin:6px 0">${r.title}</h3>
      <p>${r.rationale}</p><ul>${r.steps.map(s=>"<li>"+s+"</li>").join("")}</ul>
      <p class="muted" style="margin-top:10px">Evidence (verified):</p><ul>${r.evidence.map(e=>"<li>"+e.replace(/</g,"&lt;")+"</li>").join("")}</ul>
      <div style="margin-top:10px"><button class="btn good ${v===1?"on":""}" onclick="setRec(1)">Good</button><button class="btn bad ${v===0?"on":""}" onclick="setRec(0)">Bad</button></div></div>`;
  } else {
    h+=`<div class="card"><b>Task:</b> do these two pages really compete for the <b>same search query</b>? <b>Yes</b> = cannibalization. <b>No</b> = different topics/entities, or a legitimate pair.
    <div style="margin-top:8px"><button class="btn primary" onclick="exportPairs()">Download pair_labels.csv</button></div></div>`;
    if(!D.pairs.length){ app.innerHTML=h+"<p>No pairs.</p>"; return; }
    const q=D.pairs[idx.pairs], v=S.pairs[key(q)];
    h+=nav("pairs",D.pairs.length)+`<div class="card"><div class="muted">${q.run} · severity ${q.severity}</div>
      <p><a href="${q.url_a}" target="_blank">${q.url_a}</a></p><p><a href="${q.url_b}" target="_blank">${q.url_b}</a></p>
      <div><button class="btn good ${v===1?"on":""}" onclick="setPair(1)">Yes, they compete</button><button class="btn bad ${v===0?"on":""}" onclick="setPair(0)">No</button></div></div>`;
  }
  app.innerHTML=h;
}
function tog(i){ const p=D.pages[idx.pages], k=key(p); const st=S.pages[k]||{sel:[],done:false};
  st.sel = st.sel.includes(i) ? st.sel.filter(x=>x!==i) : st.sel.concat([i]); S.pages[k]=st; save(); render(); }
function allBlocks(on){ const p=D.pages[idx.pages], k=key(p); const st=S.pages[k]||{sel:[],done:false};
  st.sel = on ? p.blocks.map((_,i)=>i) : []; S.pages[k]=st; save(); render(); }
function donePage(){ const p=D.pages[idx.pages], k=key(p); const st=S.pages[k]||{sel:[],done:false}; st.done=true; S.pages[k]=st; save(); go("pages",1); }
function setRec(v){ S.recs[key(D.recs[idx.recs])]=v; save(); go("recs",1); }
function setPair(v){ S.pairs[key(D.pairs[idx.pairs])]=v; save(); go("pairs",1); }
function exportGold(){ const g={}; D.pages.forEach(p=>{ const st=S.pages[key(p)]; if(st&&st.done) g[p.url]=st.sel.sort((a,b)=>a-b).map(i=>p.blocks[i]).join("\n"); });
  dl("gold.json", JSON.stringify(g,null,1)); }
function exportRecs(){ let t="out_dir,rank,type,title,label_good_1_bad_0\n"; D.recs.forEach(r=>{ const v=S.recs[key(r)]; if(v!==undefined) t+=[r.out_dir,r.rank,r.type,r.title,v].map(csvq).join(",")+"\n"; });
  dl("labels.csv", t); }
function exportPairs(){ let t="url_a,url_b,severity,label_cannibal_1_not_0\n"; D.pairs.forEach(q=>{ const v=S.pairs[key(q)]; if(v!==undefined) t+=[q.url_a,q.url_b,q.severity,v].map(csvq).join(",")+"\n"; });
  dl("pair_labels.csv", t); }
document.querySelectorAll(".tab").forEach(b=>b.onclick=()=>{ tab=b.dataset.t; render(); });
render(); prog();
</script></body></html>"""
