"""Scale test for the analysis half (Parts B-E): fills a crawl store with N synthetic pages per site
(template HTML, ~25 links each) and then runs `seo-intel --skip-crawl` on it, measuring time and peak RAM.

usage: python devsite/scale_test.py N OUT_DIR
"""
import json
import random
import sys
import time
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from seo_intel.store import Store  # noqa: E402

N = int(sys.argv[1])
out = Path(sys.argv[2])
out.mkdir(parents=True, exist_ok=True)
st = Store(out / "crawl.db")
rng = random.Random(7)
VOC = [f"word{i}" for i in range(6000)]
TOPICS = [rng.sample(VOC, 80) for _ in range(300)]
t0 = time.time()
for site, host in (("target", "t.example"), ("competitor", "c.example")):
    st.set_meta(f"start_url:{site}", f"https://{host}/")
    st.set_meta(f"state:{site}", "complete")
    st.set_meta(f"mode:{site}", "fresh")
    urls = [f"https://{host}/"] + [f"https://{host}/topic-{i % 300}/page-{i}/" for i in range(1, N)]
    st.db.execute("BEGIN")
    for i, u in enumerate(urls):
        tw = TOPICS[i % 300] if site == "target" or i % 300 < 280 else TOPICS[(i % 300)]
        words = [rng.choice(tw) if rng.random() < 0.5 else rng.choice(VOC) for _ in range(rng.randint(250, 900))]
        paras = "".join(f"<p>{' '.join(words[j:j + 40])}.</p>" for j in range(0, len(words), 40))
        outl = [urls[rng.randrange(len(urls))] for _ in range(8)]
        nav = "".join(f"<li><a href='https://{host}/topic-{k}/page-{k}/'>Topic {k}</a></li>" for k in range(1, 18))
        html = (f"<html><head><title>{tw[0]} {tw[1]} page {i}</title></head><body><nav><ul>{nav}</ul></nav>"
                f"<main><h1>{tw[0]} {tw[1]} {i}</h1>{paras}"
                + "".join(f"<p>See <a href='{o}'>{tw[2]} {tw[3]}</a> too.</p>" for o in outl[:4]) +
                f"</main><footer>Copyright Example <a href='/privacy'>Privacy</a></footer></body></html>")
        links = [(u, f"https://{host}/topic-{k}/page-{k}/", f"Topic {k}", "", k - 1) for k in range(1, 18)]
        links += [(u, o, f"{tw[2]} {tw[3]}", "", 17 + j) for j, o in enumerate(outl[:4])]
        st.db.execute("INSERT OR REPLACE INTO pages(url,site,status,final_url,chain,html,title,h1,depth,run,fetched_at,"
                      "hreflang,scripts) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      (u, site, 200, u, "[]", zlib.compress(html.encode(), 6), f"{tw[0]} {tw[1]} page {i}",
                       f"{tw[0]} {tw[1]} {i}", 1, 1, 0, "[]", 0))
        st.db.executemany("INSERT OR IGNORE INTO links VALUES(?,?,?,?,?)", links)
        st.db.execute("INSERT OR IGNORE INTO urls(url,site,host,state,depth,priority,seen_run) VALUES(?,?,?,?,?,?,?)",
                      (u, site, host, "done", 1, 0, 1))
    st.db.execute("COMMIT")
st.set_meta("first_run_seconds:target", 1)
st.set_meta("run_state", "complete")
print(f"generated {2 * N} pages in {time.time() - t0:.0f}s", flush=True)
st.close()
from seo_intel.cli import main  # noqa: E402

main(["t.example", "--competitor", "c.example", "--out", str(out), "--skip-crawl", "--no-llm"])
print(json.dumps({k: v for k, v in json.loads((out / "logs" / "run_stats.json").read_text()).items()
                  if k in ("total_seconds", "timings", "peak_ram_mb")}))
