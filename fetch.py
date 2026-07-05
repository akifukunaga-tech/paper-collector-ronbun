"""Fetch latest papers from arXiv and Crossref (ACM), enrich OA via Unpaywall."""
from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import feedparser
import requests
import yaml

CODE_ROOT = Path(__file__).parent
ROOT = Path(os.environ["PAPER_ROOT"]) if os.environ.get("PAPER_ROOT") else CODE_ROOT
ROOT.mkdir(parents=True, exist_ok=True)
DATA = ROOT / "data"
DATA.mkdir(exist_ok=True)
DB = DATA / "papers.db"
CONFIG_FILE = ROOT / "config.yaml"
if not CONFIG_FILE.exists() and (CODE_ROOT / "config.yaml").exists():
    CONFIG_FILE.write_bytes((CODE_ROOT / "config.yaml").read_bytes())

SCHEMA = """
CREATE TABLE IF NOT EXISTS papers (
    id          TEXT PRIMARY KEY,
    source      TEXT NOT NULL,
    doi         TEXT,
    arxiv_id    TEXT,
    title       TEXT NOT NULL,
    authors     TEXT,
    abstract    TEXT,
    published   TEXT,
    url         TEXT,
    pdf_url     TEXT,
    categories  TEXT,
    is_oa       INTEGER DEFAULT 0,
    fetched_at  TEXT,
    shown_at    TEXT,
    score       REAL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_papers_shown   ON papers(shown_at);
CREATE INDEX IF NOT EXISTS idx_papers_fetched ON papers(fetched_at);

CREATE TABLE IF NOT EXISTS seen_keys (
    key             TEXT PRIMARY KEY,
    doi             TEXT,
    arxiv_id        TEXT,
    title           TEXT,
    first_shown_at  TEXT
);
"""

MIGRATIONS = [
    ("tldr",                       "TEXT"),
    ("citation_count",             "INTEGER"),
    ("influential_citation_count", "INTEGER"),
    ("reference_count",            "INTEGER"),
    ("fields",                     "TEXT"),
    ("methods",                    "TEXT"),
    ("tldr_source",                "TEXT"),
    ("extracted_json",             "TEXT"),
]


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB)
    conn.executescript(SCHEMA)
    existing = {row[1] for row in conn.execute("PRAGMA table_info(papers)")}
    for col, sql_type in MIGRATIONS:
        if col not in existing:
            conn.execute(f"ALTER TABLE papers ADD COLUMN {col} {sql_type}")
    conn.commit()
    return conn


def paper_id(title: str, doi: str | None, arxiv_id: str | None) -> str:
    key = (doi or arxiv_id or title).strip().lower()
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]


def fetch_arxiv(cfg: dict) -> list[dict]:
    rows: list[dict] = []
    for cat in cfg["fetch"]["arxiv_categories"]:
        url = (
            "http://export.arxiv.org/api/query"
            f"?search_query=cat:{cat}"
            "&sortBy=submittedDate&sortOrder=descending"
            f"&max_results={cfg['fetch']['arxiv_max_results']}"
        )
        feed = feedparser.parse(url)
        for e in feed.entries:
            arxiv_id = e.id.rsplit("/", 1)[-1]
            pdf_url = next(
                (l.href for l in e.links if getattr(l, "type", "") == "application/pdf"),
                None,
            )
            rows.append(
                {
                    "source": "arxiv",
                    "doi": None,
                    "arxiv_id": arxiv_id,
                    "title": e.title.strip().replace("\n", " "),
                    "authors": ", ".join(a.name for a in e.authors),
                    "abstract": e.summary.strip().replace("\n", " "),
                    "published": e.published,
                    "url": e.link,
                    "pdf_url": pdf_url,
                    "categories": ",".join(t.term for t in e.tags),
                    "is_oa": 1,
                }
            )
        time.sleep(3)  # arXiv asks for >=3s between API calls
    return rows


def fetch_crossref_acm(cfg: dict) -> list[dict]:
    since = (
        datetime.now(timezone.utc)
        - timedelta(days=cfg["fetch"]["crossref_days_back"])
    ).strftime("%Y-%m-%d")
    params = {
        "filter": f"member:{cfg['fetch']['crossref_acm_member']},from-pub-date:{since}",
        "rows": cfg["fetch"]["crossref_rows"],
        "sort": "published",
        "order": "desc",
        "mailto": cfg["display"]["email"],
    }
    r = requests.get("https://api.crossref.org/works", params=params, timeout=30)
    r.raise_for_status()
    rows: list[dict] = []
    for item in r.json()["message"]["items"]:
        title_list = item.get("title") or []
        title = title_list[0] if title_list else ""
        if not title:
            continue
        authors = ", ".join(
            (a.get("given", "") + " " + a.get("family", "")).strip()
            for a in item.get("author", [])
        )
        abstract = re.sub(r"<[^>]+>", "", item.get("abstract", "") or "").strip()
        date_parts = item.get("issued", {}).get("date-parts", [[None]])[0]
        pub_date = "-".join(f"{p:02d}" if isinstance(p, int) else str(p)
                            for p in date_parts if p)
        pdf_url = None
        for link in item.get("link", []) or []:
            if link.get("content-type") == "application/pdf":
                pdf_url = link.get("URL")
                break
        rows.append(
            {
                "source": "acm",
                "doi": item.get("DOI"),
                "arxiv_id": None,
                "title": title.strip().replace("\n", " "),
                "authors": authors,
                "abstract": abstract,
                "published": pub_date,
                "url": item.get("URL"),
                "pdf_url": pdf_url,
                "categories": ",".join(item.get("subject") or []),
                "is_oa": 1 if pdf_url else 0,
            }
        )
    return rows


def enrich_unpaywall(rows: list[dict], email: str) -> list[dict]:
    for r in rows:
        if not r["doi"] or r["is_oa"]:
            continue
        try:
            resp = requests.get(
                f"https://api.unpaywall.org/v2/{r['doi']}",
                params={"email": email},
                timeout=15,
            )
            if resp.status_code == 200:
                j = resp.json()
                if j.get("is_oa"):
                    loc = j.get("best_oa_location") or {}
                    pdf = loc.get("url_for_pdf") or loc.get("url")
                    if pdf:
                        r["pdf_url"] = pdf
                        r["is_oa"] = 1
            time.sleep(0.1)
        except requests.RequestException:
            continue
    return rows


def save(rows: list[dict]) -> tuple[int, int]:
    conn = db()
    cur = conn.cursor()
    now = datetime.now(timezone.utc).isoformat()
    seen = {row[0] for row in conn.execute("SELECT key FROM seen_keys")}
    added = 0
    skipped_seen = 0
    for r in rows:
        pid = paper_id(r["title"], r["doi"], r["arxiv_id"])
        if pid in seen:
            skipped_seen += 1
            continue
        try:
            cur.execute(
                """INSERT INTO papers
                   (id, source, doi, arxiv_id, title, authors, abstract,
                    published, url, pdf_url, categories, is_oa, fetched_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    pid, r["source"], r["doi"], r["arxiv_id"], r["title"],
                    r["authors"], r["abstract"], r["published"], r["url"],
                    r["pdf_url"], r["categories"], r["is_oa"], now,
                ),
            )
            added += 1
        except sqlite3.IntegrityError:
            pass
    conn.commit()
    conn.close()
    return added, skipped_seen


def main() -> int:
    cfg = yaml.safe_load(CONFIG_FILE.read_text(encoding="utf-8"))
    print("Fetching arXiv ...", flush=True)
    arxiv_rows = fetch_arxiv(cfg)
    print(f"  arXiv: {len(arxiv_rows)} entries")
    print("Fetching Crossref (ACM) ...", flush=True)
    acm_rows = fetch_crossref_acm(cfg)
    print(f"  ACM:   {len(acm_rows)} entries")
    print("Unpaywall enrichment for non-OA ACM rows ...", flush=True)
    acm_rows = enrich_unpaywall(acm_rows, cfg["display"]["email"])
    oa = sum(1 for r in acm_rows if r["is_oa"])
    print(f"  ACM OA after enrichment: {oa}/{len(acm_rows)}")
    added, skipped = save(arxiv_rows + acm_rows)
    print(f"Inserted {added} new papers; skipped {skipped} already-seen.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
