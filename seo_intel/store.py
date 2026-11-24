"""SQLite crawl store (WAL mode).

Crash-safety model: a URL moves queued -> done inside ONE transaction that also
writes the page row and its outlinks. If the process dies mid-fetch, the URL is
still 'queued' (we never persist an intermediate 'fetching' state), so resume
re-fetches it exactly once and nothing is duplicated (links use a PK).
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import zlib
from pathlib import Path

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS urls(
  url TEXT PRIMARY KEY, site TEXT, host TEXT, state TEXT, depth INT,
  priority REAL, src TEXT, sitemap_lastmod TEXT, reason TEXT, seen_run INT DEFAULT 0, ord TEXT);
CREATE INDEX IF NOT EXISTS urls_q ON urls(site, state, priority);
CREATE TABLE IF NOT EXISTS pages(
  url TEXT PRIMARY KEY, site TEXT, status INT, final_url TEXT, chain TEXT,
  ctype TEXT, etag TEXT, last_modified TEXT, raw_hash TEXT, html BLOB,
  rendered INT DEFAULT 0, render_reason TEXT, x_robots TEXT, meta_robots TEXT,
  canonical TEXT, meta_refresh TEXT, js_redirect TEXT, title TEXT, h1 TEXT,
  lang TEXT, hreflang TEXT, depth INT, soft404 INT DEFAULT 0, soft404_reason TEXT,
  main_text TEXT, main_hash TEXT, word_count INT, fetched_at INT, unchanged INT DEFAULT 0,
  run INT DEFAULT 0, lastmod TEXT, scripts INT DEFAULT 0, vhash TEXT, schema_types TEXT);
CREATE INDEX IF NOT EXISTS pages_site ON pages(site);
CREATE TABLE IF NOT EXISTS prev_pages(
  url TEXT PRIMARY KEY, site TEXT, status INT, final_url TEXT, title TEXT,
  canonical TEXT, etag TEXT, last_modified TEXT, raw_hash TEXT, html BLOB,
  chain TEXT);
CREATE TABLE IF NOT EXISTS links(
  src TEXT, dst TEXT, anchor TEXT, block TEXT, ord INT,
  PRIMARY KEY(src, dst, ord));
CREATE INDEX IF NOT EXISTS links_dst ON links(dst);
CREATE TABLE IF NOT EXISTS traps(
  site TEXT, kind TEXT, pattern TEXT, example TEXT, hits INT,
  PRIMARY KEY(site, kind, pattern));
CREATE TABLE IF NOT EXISTS stats(site TEXT, k TEXT, v REAL, PRIMARY KEY(site,k));
"""


class Store:
    def __init__(self, path: str | Path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self._migrate()

    def _migrate(self):
        """Older crawl.db files (earlier versions) get the newer columns, so incremental runs keep working."""
        want = {"urls": [("ord", "TEXT")], "pages": [("vhash", "TEXT"), ("schema_types", "TEXT")]}
        for table, cols in want.items():
            have = {r[1] for r in self.db.execute(f"PRAGMA table_info({table})")}
            for name, typ in cols:
                if name not in have:
                    self.db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {typ}")

    # ---------- meta ----------
    def get_meta(self, k, default=None):
        r = self.db.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
        return json.loads(r[0]) if r else default

    def set_meta(self, k, v):
        self.db.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (k, json.dumps(v)))

    def stat(self, site, k, v):
        self.db.execute("INSERT OR REPLACE INTO stats VALUES(?,?,?)", (site, k, float(v)))

    def get_stat(self, site, k, default=0.0):
        r = self.db.execute("SELECT v FROM stats WHERE site=? AND k=?", (site, k)).fetchone()
        return r[0] if r else default

    # ---------- incremental ----------
    def snapshot_previous(self, site):
        """Move last run's pages into prev_pages so this run can diff against them."""
        n = self.db.execute("SELECT COUNT(*) FROM pages WHERE site=?", (site,)).fetchone()[0]
        if n == 0:
            return 0
        self.db.execute("BEGIN")
        self.db.execute("DELETE FROM prev_pages WHERE site=?", (site,))
        self.db.execute(
            """INSERT INTO prev_pages SELECT url,site,status,final_url,title,canonical,
               etag,last_modified,raw_hash,html,chain FROM pages WHERE site=?""", (site,))
        # keep pages+links (carried over if not re-fetched); re-decide every URL
        self.db.execute("UPDATE urls SET state='queued', reason=NULL WHERE site=?", (site,))
        self.db.execute("DELETE FROM traps WHERE site=?", (site,))
        self.db.execute("COMMIT")
        return n

    def drop_unseen(self, site, run):
        """Pages not reached in this run are removed (they stay in prev_pages for the report)."""
        self.db.execute("BEGIN")
        # discovered this run but not re-processed before the deadline: carry over
        self.db.execute("""UPDATE pages SET run=?, unchanged=1 WHERE site=? AND run<? AND url IN
                           (SELECT url FROM urls WHERE site=? AND seen_run=?)""", (run, site, run, site, run))
        self.db.execute("DELETE FROM links WHERE src IN (SELECT url FROM pages WHERE site=? AND run<?)", (site, run))
        self.db.execute("DELETE FROM pages WHERE site=? AND run<?", (site, run))
        self.db.execute("DELETE FROM urls WHERE site=? AND seen_run<?", (site, run))
        self.db.execute("COMMIT")

    def prev(self, url):
        return self.db.execute("SELECT * FROM prev_pages WHERE url=?", (url,)).fetchone()

    # ---------- frontier ----------
    def add_urls(self, rows, run=0):
        """rows: (url, site, host, depth, priority, src, lastmod). Upsert marks seen in this run."""
        self.db.executemany(
            """INSERT INTO urls(url,site,host,state,depth,priority,src,sitemap_lastmod,seen_run,ord)
               VALUES(?,?,?,'queued',?,?,?,?,?,?)
               ON CONFLICT(url) DO UPDATE SET seen_run=excluded.seen_run,
                 depth=MIN(depth, excluded.depth), priority=MAX(priority, excluded.priority),
                 sitemap_lastmod=COALESCE(excluded.sitemap_lastmod, sitemap_lastmod)""",
            [r + (run, hashlib.sha1(r[0].encode("utf-8", "replace")).hexdigest()[:16]) for r in rows])

    def mark(self, url, state, reason=None):
        self.db.execute("UPDATE urls SET state=?, reason=? WHERE url=?", (state, reason, url))

    def next_batch(self, site, n, run=0, exclude=(), depth=None):
        """Queued URLs of one BFS level in a fixed order (URL hash) - the order never depends on timing."""
        if depth is None:
            depth = self.min_queued_depth(site, run)
            if depth is None:
                return []
        rows = self.db.execute(
            """SELECT url, depth, host, sitemap_lastmod FROM urls WHERE site=? AND state='queued' AND seen_run=?
               AND depth=? ORDER BY ord, url LIMIT ?""", (site, run, depth, n + len(exclude))).fetchall()
        return [r for r in rows if r[0] not in exclude][:n]

    def min_queued_depth(self, site, run=0):
        r = self.db.execute("SELECT MIN(depth) FROM urls WHERE site=? AND state='queued' AND seen_run=?",
                            (site, run)).fetchone()
        return r[0] if r else None

    def count(self, sql, args=()):
        return self.db.execute(sql, args).fetchone()[0]

    def save_page(self, row: dict, links: list[tuple]):
        """Atomic: page + outlinks + url state 'done'."""
        html = row.pop("html", None)
        if html is not None:
            row["html"] = zlib.compress(html.encode("utf-8", "replace"), 6)
        cols = ",".join(row.keys())
        qs = ",".join("?" * len(row))
        self.db.execute("BEGIN")
        try:
            self.db.execute(f"INSERT OR REPLACE INTO pages({cols}) VALUES({qs})", list(row.values()))
            self.db.execute("DELETE FROM links WHERE src=?", (row["url"],))
            if links:
                self.db.executemany("INSERT OR IGNORE INTO links VALUES(?,?,?,?,?)", links)
            self.db.execute("UPDATE urls SET state='done' WHERE url=?", (row["url"],))
            self.db.execute("COMMIT")
        except Exception:
            self.db.execute("ROLLBACK")
            raise

    def update_page(self, url, **fields):
        sets = ",".join(f"{k}=?" for k in fields)
        self.db.execute(f"UPDATE pages SET {sets} WHERE url=?", list(fields.values()) + [url])

    def html(self, url):
        r = self.db.execute("SELECT html FROM pages WHERE url=?", (url,)).fetchone()
        if not r or r[0] is None:
            return None
        return zlib.decompress(r[0]).decode("utf-8", "replace")

    @staticmethod
    def unz(blob):
        return zlib.decompress(blob).decode("utf-8", "replace") if blob else None

    def pages(self, site, where="1=1", cols="*"):
        return self.db.execute(
            f"SELECT {cols} FROM pages WHERE site=? AND {where} ORDER BY url", (site,)).fetchall()

    def add_trap(self, site, kind, pattern, example):
        self.db.execute(
            """INSERT INTO traps VALUES(?,?,?,?,1) ON CONFLICT(site,kind,pattern)
               DO UPDATE SET hits=hits+1, example=MIN(example, excluded.example)""", (site, kind, pattern, example))

    def close(self):
        self.db.close()
