"""Score, enrich, extract figures, render index.html.

Pipeline:
  1. Pull unread OA papers from DB.
  2. Pre-score by keyword match -> top N candidates.
  3. Batch-enrich candidates via Semantic Scholar (tldr, citations, fields).
  4. Composite score = base * (1 + novelty + citation*recency); pick top K.
  5. Download PDFs, extract figures + nearby captions.
  6. Render a visual HTML page.
"""
from __future__ import annotations

import json
import math
import os
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import fitz  # PyMuPDF
import requests
import yaml
from jinja2 import Template
from PIL import Image

import llm

CODE_ROOT = Path(__file__).parent
# ROOT      = writable state (DB, PDFs, config.yaml)
# PUBLIC    = public site output (index.html + data/figures/ served by Pages)
# Defaults: both = code dir (unchanged local behavior).
ROOT   = Path(os.environ["PAPER_ROOT"])        if os.environ.get("PAPER_ROOT")        else CODE_ROOT
PUBLIC = Path(os.environ["PAPER_PUBLIC_ROOT"]) if os.environ.get("PAPER_PUBLIC_ROOT") else ROOT
ROOT.mkdir(parents=True, exist_ok=True)
PUBLIC.mkdir(parents=True, exist_ok=True)
DATA = ROOT / "data"
PDF_DIR = DATA / "pdfs"
FIG_DIR = PUBLIC / "data" / "figures"     # figures are served publicly
PDF_DIR.mkdir(parents=True, exist_ok=True)
FIG_DIR.mkdir(parents=True, exist_ok=True)
DB = DATA / "papers.db"
CONFIG_FILE = ROOT / "config.yaml"
if not CONFIG_FILE.exists() and (CODE_ROOT / "config.yaml").exists():
    # Seed the writable location from the shipped defaults.
    CONFIG_FILE.write_bytes((CODE_ROOT / "config.yaml").read_bytes())


def ensure_schema() -> None:
    """Apply schema migrations if render.py is invoked before fetch.py has touched the DB."""
    if not DB.exists():
        return
    conn = sqlite3.connect(DB)
    existing = {row[1] for row in conn.execute("PRAGMA table_info(papers)")}
    for col, sql_type in [
        ("tldr",                       "TEXT"),
        ("citation_count",             "INTEGER"),
        ("influential_citation_count", "INTEGER"),
        ("reference_count",            "INTEGER"),
        ("fields",                     "TEXT"),
        ("methods",                    "TEXT"),
        ("tldr_source",                "TEXT"),
        ("extracted_json",             "TEXT"),
    ]:
        if col not in existing:
            conn.execute(f"ALTER TABLE papers ADD COLUMN {col} {sql_type}")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS seen_keys (
            key             TEXT PRIMARY KEY,
            doi             TEXT,
            arxiv_id        TEXT,
            title           TEXT,
            first_shown_at  TEXT
        )
    """)
    conn.commit()
    conn.close()


# ---------- scoring ----------

NOVELTY_PATTERNS = [
    r"\bwe propose\b",
    r"\bwe introduce\b",
    r"\bwe present\b",
    r"\bnovel\b",
    r"\bnew (method|framework|approach|architecture)\b",
    r"\bfirst (to|method|framework)\b",
    r"\boutperforms?\b",
    r"\bstate[- ]of[- ]the[- ]art\b",
    r"\bsota\b",
]


def keyword_score(text: str, interests: dict) -> tuple[float, list[str]]:
    t = text.lower()
    total = 0.0
    hits: list[str] = []
    for name, spec in interests.items():
        w = spec.get("weight", 1.0)
        n = sum(1 for kw in spec["keywords"] if kw.lower() in t)
        if n:
            total += w * (1 + 0.2 * (n - 1))
            hits.append(name)
    return total, hits


def novelty_score(text: str) -> float:
    t = text.lower()
    hits = sum(1 for p in NOVELTY_PATTERNS if re.search(p, t))
    return min(1.0, hits / 3.0)


def extract_methods(text: str, vocab: list[str]) -> list[str]:
    t = text.lower()
    found: list[str] = []
    for m in vocab:
        if re.search(r"\b" + re.escape(m.lower()) + r"\b", t):
            found.append(m)
    return found[:8]


def recency_decay(pub_str: str | None) -> float:
    if not pub_str:
        return 0.5
    try:
        pub = datetime.fromisoformat(pub_str[:10])
    except ValueError:
        return 0.5
    days = max(0, (datetime.now() - pub).days)
    return math.exp(-days / 180.0)


def composite_score(p: dict, cfg: dict) -> tuple[float, list[str]]:
    text = " ".join(filter(None, [
        p.get("title"), p.get("tldr"), p.get("abstract"),
    ]))
    kw, hits = keyword_score(text, cfg["interests"])
    nov = novelty_score(text)
    w = cfg.get("score_weights", {})
    return kw * w.get("keyword", 1.0) * (1.0 + w.get("novelty", 0.3) * nov), hits


# ---------- semantic scholar batch enrichment ----------

def s2_enrich(papers: list[dict]) -> None:
    """Mutate papers in place with tldr/citation_count/fields. Requires arxiv_id or doi."""
    ids: list[str] = []
    back: dict[int, int] = {}
    for i, p in enumerate(papers):
        if p.get("arxiv_id"):
            arxiv = p["arxiv_id"].split("v")[0]
            ids.append(f"ARXIV:{arxiv}")
            back[len(ids) - 1] = i
        elif p.get("doi"):
            ids.append(f"DOI:{p['doi']}")
            back[len(ids) - 1] = i
    if not ids:
        return
    fields = "tldr,citationCount,influentialCitationCount,referenceCount,s2FieldsOfStudy,publicationDate,externalIds"
    try:
        r = requests.post(
            "https://api.semanticscholar.org/graph/v1/paper/batch",
            params={"fields": fields},
            json={"ids": ids},
            timeout=60,
        )
        r.raise_for_status()
        results = r.json()
    except requests.RequestException as e:
        print(f"  S2 enrichment failed: {e}")
        return
    for j, item in enumerate(results):
        if not item:
            continue
        p = papers[back[j]]
        tldr = (item.get("tldr") or {}).get("text")
        if tldr:
            p["tldr"] = tldr
            p["tldr_source"] = "s2"
        if item.get("publicationDate") and not p.get("published"):
            p["published"] = item["publicationDate"]
        ext = item.get("externalIds") or {}
        if ext.get("DOI") and not p.get("doi"):
            p["doi"] = ext["DOI"]


def persist_enrichment(papers: list[dict]) -> None:
    conn = sqlite3.connect(DB)
    for p in papers:
        conn.execute(
            """UPDATE papers SET
                 tldr = COALESCE(?, tldr),
                 tldr_source = COALESCE(?, tldr_source),
                 methods = COALESCE(?, methods),
                 published = COALESCE(?, published),
                 doi = COALESCE(?, doi)
               WHERE id = ?""",
            (
                p.get("tldr"),
                p.get("tldr_source"),
                ",".join(p.get("methods") or []) or None,
                p.get("published"),
                p.get("doi"),
                p["id"],
            ),
        )
    conn.commit()
    conn.close()


PROPOSE_RE = re.compile(
    r"\bwe\s+(propose|introduce|present|develop|design|build|propose to|aim to|study|investigate)\b",
    re.IGNORECASE,
)


def fallback_tldr(abstract: str | None) -> str | None:
    """Pick a 'this is what we did' sentence from the abstract."""
    if not abstract:
        return None
    sentences = re.split(r"(?<=[.!?])\s+", abstract.strip())
    for s in sentences:
        if PROPOSE_RE.search(s):
            return s.strip()[:260]
    return (sentences[0].strip()[:260] if sentences else None)


def apply_tldr_fallback(papers: list[dict]) -> int:
    filled = 0
    for p in papers:
        if not p.get("tldr"):
            t = fallback_tldr(p.get("abstract"))
            if t:
                p["tldr"] = t
                p["tldr_source"] = "fallback"
                filled += 1
    return filled


# ---------- LLM structured extraction (delegated to llm.py) ----------

def llm_structured(abstract: str, cfg: dict) -> dict | None:
    """Delegates to the llm module, which picks Ollama / Groq / Claude by config."""
    return llm.structured(abstract or "", cfg.get("llm") or {})


def llm_refine(papers: list[dict], cfg: dict) -> int:
    """Run the structured LLM call per paper, persist into extracted_json.
    Re-runs hit the cache via tldr_source='llm-structured'."""
    refined = 0
    for i, p in enumerate(papers, 1):
        if p.get("tldr_source") == "llm-structured" and p.get("extracted_json"):
            # already have full structured data, load it
            try:
                data = json.loads(p["extracted_json"])
                p["llm_data"] = data
                print(f"  LLM {i}/{len(papers)}: cached")
                continue
            except (TypeError, json.JSONDecodeError):
                pass  # fall through and regenerate
        data = llm_structured(p.get("abstract") or "", cfg)
        if data:
            p["tldr"] = data["tldr"]
            p["tldr_source"] = "llm-structured"
            p["llm_data"] = data
            p["extracted_json"] = json.dumps(data, ensure_ascii=False)
            refined += 1
            print(f"  LLM {i}/{len(papers)}: {data['tldr'][:60]}...")
        else:
            print(f"  LLM {i}/{len(papers)}: skipped")
    return refined


# ---------- candidate selection ----------

def load_dismissed() -> set[str]:
    """Load the permanent dismissed-paper log. Ships from GDrive via Actions;
    treat missing/malformed file as empty set (fail open)."""
    p = ROOT / "dismissed.json"
    if not p.exists():
        return set()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return set(data.keys()) if isinstance(data, dict) else set()
    except (json.JSONDecodeError, OSError):
        return set()


def select_candidates(cfg: dict) -> list[dict]:
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    where = "WHERE shown_at IS NULL"
    if cfg["display"].get("oa_only", True):
        where += " AND is_oa = 1"
    rows = conn.execute(f"SELECT * FROM papers {where} ORDER BY fetched_at DESC").fetchall()
    conn.close()
    dismissed = load_dismissed()
    if dismissed:
        print(f"  filtering out {len(dismissed)} previously-dismissed paper ids")
    scored: list[tuple[float, dict]] = []
    for r in rows:
        d = dict(r)
        if d["id"] in dismissed:
            continue
        if d.get("fields"):
            d["fields"] = d["fields"].split(",")
        text = (d.get("title") or "") + " " + (d.get("abstract") or "")
        s, _ = keyword_score(text, cfg["interests"])
        if s > 0:
            scored.append((s, d))
    scored.sort(key=lambda x: -x[0])
    pool = cfg.get("candidate_pool", 80)
    return [d for _, d in scored[:pool]]


def finalize_picks(papers: list[dict], cfg: dict, n: int) -> tuple[list[dict], int]:
    enriched: list[tuple[float, list[str], dict]] = []
    for p in papers:
        p["methods"] = extract_methods(
            " ".join(filter(None, [p.get("title"), p.get("tldr"), p.get("abstract")])),
            cfg.get("methods") or [],
        )
        s, hits = composite_score(p, cfg)
        enriched.append((s, hits, p))
    enriched.sort(key=lambda x: -x[0])
    picked = enriched[:n]
    now = datetime.now(timezone.utc).isoformat()
    conn = sqlite3.connect(DB)
    for s, hits, p in picked:
        p["score"] = s
        p["cat_badges"] = hits
        conn.execute(
            "UPDATE papers SET shown_at = ?, score = ? WHERE id = ?",
            (now, s, p["id"]),
        )
        conn.execute(
            """INSERT OR IGNORE INTO seen_keys
               (key, doi, arxiv_id, title, first_shown_at) VALUES (?, ?, ?, ?, ?)""",
            (p["id"], p.get("doi"), p.get("arxiv_id"), p.get("title"), now),
        )
    conn.commit()
    conn.close()
    remaining = max(0, len(enriched) - len(picked))
    return [p for _, _, p in picked], remaining


def persist_llm_tldrs(papers: list[dict]) -> None:
    """Save LLM TLDR + extracted_json back so re-runs hit the cache."""
    conn = sqlite3.connect(DB)
    for p in papers:
        if p.get("tldr_source") == "llm-structured":
            conn.execute(
                "UPDATE papers SET tldr = ?, tldr_source = ?, extracted_json = ? WHERE id = ?",
                (p.get("tldr"), "llm-structured", p.get("extracted_json"), p["id"]),
            )
    conn.commit()
    conn.close()


# ---------- figure + caption extraction ----------

CAPTION_RE = re.compile(r"^(fig(?:ure)?\.?\s*\d+)", re.IGNORECASE)


def find_caption(page, rect) -> str | None:
    blocks = page.get_text("blocks")
    candidates: list[tuple[float, str]] = []
    for x0, y0, x1, y1, text, *_ in blocks:
        if not text or not text.strip():
            continue
        head = text.strip().split("\n", 1)[0]
        if not CAPTION_RE.match(head):
            continue
        if y0 > rect.y1 and (y0 - rect.y1) < 120:
            candidates.append((y0 - rect.y1, text.strip()))
    if not candidates:
        return None
    candidates.sort()
    cap = candidates[0][1].replace("\n", " ").strip()
    return cap[:320]


def extract_figures(pdf: Path, out_prefix: Path, max_figs: int = 3, scan_pages: int = 16) -> list[dict]:
    """Walk up to scan_pages of the PDF, collect all decent-sized embedded images,
    rank by area (largest first) so the hero is the architecture diagram."""
    pool: list[dict] = []
    try:
        doc = fitz.open(pdf)
    except Exception:
        return pool
    seen: set[int] = set()
    for pno in range(min(len(doc), scan_pages)):
        page = doc[pno]
        for img in page.get_images(full=True):
            xref = img[0]
            if xref in seen:
                continue
            seen.add(xref)
            try:
                rects = page.get_image_rects(xref)
                if not rects:
                    continue
                base = doc.extract_image(xref)
                w, h = base.get("width", 0), base.get("height", 0)
                if w < 320 or h < 220:
                    continue
                aspect = w / max(h, 1)
                # Drop banner-shaped strips (very wide & short) and thin sidebars.
                if aspect > 6.0 or aspect < 0.18:
                    continue
                pool.append(
                    {
                        "xref": xref,
                        "page": pno,
                        "rect": rects[0],
                        "ext": base.get("ext", "png"),
                        "bytes": base["image"],
                        "w": w,
                        "h": h,
                        "area": w * h,
                    }
                )
            except Exception:
                continue
    pool.sort(key=lambda f: -f["area"])
    figs: list[dict] = []
    for i, src in enumerate(pool[:max_figs]):
        p = out_prefix.with_name(out_prefix.name + f".fig{i}.{src['ext']}")
        p.write_bytes(src["bytes"])
        cap = find_caption(doc[src["page"]], src["rect"])
        figs.append({"path": p, "caption": cap, "w": src["w"], "h": src["h"]})
    if not figs:
        try:
            pix = doc[0].get_pixmap(matrix=fitz.Matrix(1.5, 1.5))
            thumb = out_prefix.with_name(out_prefix.name + ".thumb.png")
            pix.save(thumb)
            figs.append({"path": thumb, "caption": None, "w": pix.width, "h": pix.height})
        except Exception:
            pass
    doc.close()
    return figs


def compress_image(src: Path, dest: Path, max_px: int, quality: int) -> bool:
    """Resize (max_px on longer side) + JPEG-compress. Composites transparency onto white."""
    try:
        with Image.open(src) as im:
            if im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info):
                if im.mode != "RGBA":
                    im = im.convert("RGBA")
                bg = Image.new("RGB", im.size, (255, 255, 255))
                bg.paste(im, mask=im.split()[-1])
                im = bg
            elif im.mode != "RGB":
                im = im.convert("RGB")
            w, h = im.size
            longer = max(w, h)
            if longer > max_px:
                ratio = max_px / longer
                im = im.resize((max(1, int(w * ratio)), max(1, int(h * ratio))), Image.LANCZOS)
            im.save(dest, "JPEG", quality=quality, optimize=True, progressive=True)
        return True
    except Exception:
        return False


def download_pdf(url: str, dest: Path) -> bool:
    if dest.exists() and dest.stat().st_size > 1024:
        return True
    try:
        r = requests.get(
            url,
            timeout=60,
            stream=True,
            headers={"User-Agent": "paper-collector/0.2 (aki.fukunaga01@gmail.com)"},
            allow_redirects=True,
        )
        if r.status_code != 200:
            return False
        with open(dest, "wb") as f:
            for chunk in r.iter_content(8192):
                f.write(chunk)
        return dest.stat().st_size > 1024
    except requests.RequestException:
        return False


# ---------- HTML ----------

HTML = r"""<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#0b0e13">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="Papers">
<link rel="manifest" href="/manifest.json">
<link rel="apple-touch-icon" href="/icons/icon-180.png">
<link rel="icon" type="image/png" href="/icons/favicon.png">
<title>Today's Papers - {{ date }}</title>
<script src="https://cdn.tailwindcss.com"></script>
{% if gdrive_client_id %}<script src="https://accounts.google.com/gsi/client" async defer></script>{% endif %}
<style>
  :root {
    --bg: #0b0e13; --panel: #141a22; --border: #232b36; --border-hi: #4c8bf5;
    --fg: #e7eef7; --muted: #8a96a6; --accent: #6ea9ff; --hero-bg: #f7f9fc;
    --green: #4ade80; --green-dark: #143b2c;
    --purple: #c084fc; --purple-dark: #2c1f3f;
    --arxiv: #b31b1b; --acm: #0085ca;
  }
  * { box-sizing: border-box; }
  html, body { margin: 0; padding: 0; height: 100vh; height: 100dvh; }
  body { background: var(--bg); color: var(--fg); font-family: -apple-system, "Segoe UI", "Hiragino Sans", "Yu Gothic UI", sans-serif; overflow: hidden; display: flex; flex-direction: column; -webkit-tap-highlight-color: transparent; }
  header, #refreshStatus, .progress { flex-shrink: 0; }

  header { padding: 14px 28px; border-bottom: 1px solid var(--border); display: flex; align-items: center; gap: 16px; flex-wrap: wrap; }
  header h1 { font-size: 29px; font-weight: 700; margin: 0; }
  header .sub { color: var(--muted); font-size: 16px; }
  header .spacer { flex: 1; }
  .chip { background: #1d2530; color: #b6c1d1; padding: 3px 10px; border-radius: 6px; font-size: 14px; }
  .chip strong { color: var(--fg); font-weight: 600; }

  #refreshBtn {
    background: #1f6feb; color: white; font-weight: 600; padding: 8px 16px; border-radius: 6px;
    border: 0; cursor: pointer; display: flex; align-items: center; gap: 8px; font-size: 17px;
  }
  #refreshBtn:hover { background: #388bfd; }
  #refreshBtn:disabled { background: #30363d; cursor: not-allowed; color: var(--muted); }

  .settings-btn {
    background: var(--panel); color: var(--fg); border: 1px solid var(--border);
    padding: 7px 14px; border-radius: 6px; cursor: pointer;
    display: flex; align-items: center; gap: 7px; font-size: 14px;
  }
  .settings-btn:hover { border-color: var(--border-hi); color: var(--accent); }

  /* Settings modal */
  .modal-bg {
    position: fixed; inset: 0; background: rgba(0,0,0,.72); backdrop-filter: blur(4px);
    z-index: 100; display: none; align-items: center; justify-content: center;
  }
  .modal-bg.open { display: flex; }
  .modal {
    background: var(--panel); border: 1px solid var(--border); border-radius: 14px;
    padding: 24px 26px; width: 90%; max-width: 760px; max-height: 90vh; overflow-y: auto;
  }
  .modal-head { display: flex; align-items: center; justify-content: space-between; margin-bottom: 6px; }
  .modal-head h2 { font-size: 22px; font-weight: 700; margin: 0; }
  .modal-x { background: transparent; border: 0; color: var(--muted); font-size: 26px; line-height: 1; cursor: pointer; padding: 0 4px; }
  .modal-x:hover { color: var(--fg); }
  .modal-hint { color: var(--muted); font-size: 13px; line-height: 1.5; margin: 0 0 16px 0; }
  .modal-hint code { background: var(--bg); padding: 1px 6px; border-radius: 3px; font-size: 12px; }
  .cfg-cat {
    background: rgba(255,255,255,.02); border: 1px solid var(--border);
    border-radius: 10px; padding: 14px; margin-bottom: 12px;
  }
  .cfg-row1 { display: flex; align-items: center; gap: 10px; margin-bottom: 10px; }
  .cfg-name {
    background: transparent; border: 0; border-bottom: 1px solid var(--border);
    color: var(--fg); font-size: 16px; padding: 4px 0; flex: 1; font-weight: 600;
  }
  .cfg-name:focus { border-bottom-color: var(--accent); outline: none; }
  .cfg-weight-lb { color: var(--muted); font-size: 13px; }
  .cfg-weight {
    width: 64px; background: var(--bg); border: 1px solid var(--border);
    color: var(--fg); padding: 4px 8px; border-radius: 4px; font-size: 14px;
  }
  .cfg-del {
    background: transparent; border: 0; color: #f87171;
    font-size: 22px; line-height: 1; cursor: pointer; padding: 0 4px;
  }
  .cfg-del:hover { color: #fca5a5; }
  .cfg-kw {
    width: 100%; min-height: 70px;
    background: var(--bg); border: 1px solid var(--border); border-radius: 6px;
    padding: 10px; color: var(--fg); font-size: 14px;
    font-family: ui-monospace, "SF Mono", Menlo, monospace;
    resize: vertical; line-height: 1.5;
  }
  .cfg-kw:focus { border-color: var(--accent); outline: none; }
  .modal-btn {
    padding: 9px 20px; border-radius: 6px; border: 0;
    cursor: pointer; font-size: 14px; font-weight: 600;
  }
  .modal-btn.primary { background: #1f6feb; color: white; }
  .modal-btn.primary:hover { background: #388bfd; }
  .modal-btn.primary:disabled { background: #30363d; cursor: not-allowed; color: var(--muted); }
  .modal-btn.ghost { background: transparent; color: var(--fg); border: 1px solid var(--border); }
  .modal-btn.ghost:hover { background: rgba(255,255,255,.04); border-color: var(--border-hi); }
  .modal-btn.full { width: 100%; margin-top: 4px; }
  .modal-actions { display: flex; gap: 10px; justify-content: flex-end; margin-top: 18px; }
  .modal-status { font-size: 13px; color: var(--muted); margin: 10px 0 0 0; min-height: 18px; text-align: right; }
  .modal-status.ok { color: var(--green); }
  .modal-status.err { color: #f87171; }
  #refreshSpinner { animation: spin 1s linear infinite; }
  @keyframes spin { from {transform: rotate(0deg)} to {transform: rotate(360deg)} }

  #refreshStatus { padding: 8px 28px; font-family: ui-monospace, monospace; font-size: 16px; color: var(--muted); background: #0d1219; border-bottom: 1px solid var(--border); }
  #refreshStatus.hidden { display: none; }

  .progress { height: 3px; background: var(--border); }
  .progress > div { height: 100%; background: linear-gradient(90deg, var(--green), var(--accent)); transition: width .3s; }

  /* Slides */
  .slides {
    display: flex; overflow-x: auto; overflow-y: hidden;
    scroll-snap-type: x mandatory; scroll-behavior: smooth;
    flex: 1 1 auto; min-height: 0;
    scrollbar-width: none;
  }
  .slides::-webkit-scrollbar { display: none; }
  .slide {
    flex: 0 0 100%; min-width: 100%; height: 100%;
    scroll-snap-align: start;
    padding: 24px 32px;
    overflow-y: auto;
    display: grid;
    grid-template-rows: auto auto auto 1fr;
    gap: 14px;
  }
  .slide.dismissed { display: none; }

  /* Row 1: TLDR */
  .tldr {
    border-left: 4px solid var(--accent);
    background: linear-gradient(90deg, rgba(110,169,255,.12), transparent 70%);
    padding: 14px 20px; border-radius: 0 10px 10px 0;
    font-size: 23px; line-height: 1.6; font-weight: 500; color: #e7eef7;
  }
  .tldr .label {
    display: block; font-size: 13px; color: var(--accent); letter-spacing: 1.5px;
    margin-bottom: 6px; text-transform: uppercase; font-weight: 700;
  }

  /* Row 2: Title */
  .title-row h2 {
    font-size: 29px; font-weight: 600; line-height: 1.4; margin: 0;
  }
  .title-row h2 a { color: var(--fg); text-decoration: none; }
  .title-row h2 a:hover { color: var(--accent); }
  .title-row .authors { font-size: 16px; color: var(--muted); margin-top: 4px; }

  /* Row 3: keywords + ids */
  .kw-row { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; }
  .badge { display: inline-flex; padding: 3px 10px; border-radius: 12px; font-size: 14px; font-weight: 500; }
  .badge-src-arxiv { background: var(--arxiv); color: white; }
  .badge-src-acm   { background: var(--acm); color: white; }
  .badge-cat       { background: #1d2530; color: #b6c1d1; border: 1px solid #2a3340; }
  .badge-keyword   { background: var(--green-dark); color: var(--green); border: 1px solid #1f5a45; }
  .id-line { font-family: ui-monospace, monospace; font-size: 14px; color: var(--muted); margin-left: auto; }
  .id-line a { color: var(--muted); margin-left: 12px; text-decoration: none; }
  .id-line a:hover { color: var(--accent); }

  /* Row 4: method | evaluation grid */
  .me-grid {
    display: grid;
    grid-template-columns: minmax(0, 1.3fr) minmax(0, 1fr);
    gap: 22px;
    min-height: 0;
  }
  .panel { background: var(--panel); border: 1px solid var(--border); border-radius: 12px; padding: 18px; display: flex; flex-direction: column; gap: 12px; min-height: 0; overflow-y: auto; }
  .panel h3 {
    font-size: 14px; letter-spacing: 1.5px; color: var(--accent);
    text-transform: uppercase; margin: 0; font-weight: 700;
  }
  .panel.method  h3 { color: var(--green); }
  .panel.insight    { background: linear-gradient(160deg, #1a2530 0%, #141a22 60%); }
  .panel.insight h3 { color: var(--green); }

  /* Method */
  .hero-fig { background: var(--hero-bg); border-radius: 8px; padding: 10px; display: flex; align-items: center; justify-content: center; max-height: 460px; }
  .hero-fig img { max-height: 440px; max-width: 100%; object-fit: contain; }
  .hero-cap { font-size: 14px; color: var(--muted); line-height: 1.5; font-style: italic; margin: 0; }
  .essence {
    background: rgba(74,222,128,.08); border-left: 3px solid var(--green);
    padding: 10px 14px; border-radius: 0 6px 6px 0; font-size: 17px; line-height: 1.6; color: #d4f5dc;
  }
  .essence .label { display: block; font-size: 12px; color: var(--green); letter-spacing: 1.5px;
                    margin-bottom: 4px; text-transform: uppercase; font-weight: 700; }
  .pipeline-section { display: flex; flex-direction: column; gap: 10px; padding-top: 4px; }
  .sub-h-green { color: var(--green); }
  .pipeline {
    display: flex; flex-wrap: wrap; gap: 8px 6px; align-items: center;
    padding: 16px;
    background: rgba(74, 222, 128, .04);
    border: 1px dashed rgba(74, 222, 128, .25);
    border-radius: 12px;
  }
  .pipeline .step {
    background: linear-gradient(135deg, #1a4d3a, #143b2c);
    border: 1.5px solid var(--green);
    color: #d4f5dc;
    padding: 10px 18px; border-radius: 22px;
    font-size: 16px; font-weight: 600; white-space: nowrap;
    box-shadow: 0 2px 8px rgba(74,222,128,.18);
  }
  .pipeline .arrow {
    color: var(--green); font-size: 20px; font-weight: 700;
    text-shadow: 0 0 8px rgba(74,222,128,.5);
  }
  .insight-dismiss {
    align-self: flex-end;
    margin-top: 6px;
    background: rgba(74,222,128,.08);
    border: 1px solid rgba(74,222,128,.3);
    color: var(--green);
    padding: 6px 16px; font-size: 14px;
    border-radius: 6px; cursor: pointer;
  }
  .insight-dismiss:hover { background: rgba(74,222,128,.18); color: #d4f5dc; }

  /* Insight panel (right column): key idea + key result */
  .insight-header {
    display: flex; align-items: center; gap: 10px;
    color: var(--green); font-size: 15px; letter-spacing: 1.5px;
    text-transform: uppercase; font-weight: 700;
    margin-bottom: 4px;
  }
  .insight-block {
    flex: 1;
    background: linear-gradient(135deg, rgba(74,222,128,.18) 0%, rgba(110,169,255,.06) 70%);
    border: 2px solid rgba(74,222,128,.45);
    border-radius: 18px;
    padding: 32px 36px;
    font-size: 22px;
    line-height: 1.75;
    font-weight: 500;
    color: #f0fff5;
    position: relative;
    overflow: hidden;
    display: flex; align-items: center;
    min-height: 220px;
  }
  .insight-block::before {
    content: ""; position: absolute;
    top: -50px; right: -50px;
    width: 200px; height: 200px;
    background: radial-gradient(circle, rgba(74,222,128,.35) 0%, transparent 65%);
    border-radius: 50%; pointer-events: none;
  }
  .insight-block::after {
    content: "\201C"; position: absolute;
    top: -28px; left: 14px;
    font-size: 130px; font-family: Georgia, "Times New Roman", serif;
    color: rgba(74,222,128,.22);
    line-height: 1; font-weight: bold; pointer-events: none;
  }
  .insight-block > * { position: relative; z-index: 1; }
  .key-result-section { display: flex; flex-direction: column; gap: 8px; padding-top: 6px; }
  .key-result-section .lbl {
    font-size: 13px; letter-spacing: 1.5px;
    text-transform: uppercase; font-weight: 700;
    color: var(--purple);
  }

  /* Evaluation rows (now in left "Method" panel) */
  .eval-block {
    border-top: 1px solid var(--border);
    padding-top: 14px; margin-top: 6px;
    display: flex; flex-direction: column; gap: 10px;
  }
  .sub-h {
    font-size: 13px; letter-spacing: 1.5px;
    color: var(--purple); text-transform: uppercase;
    font-weight: 700; margin: 0;
  }
  .eval-row { display: flex; flex-direction: column; gap: 4px; }
  .eval-row .lbl { font-size: 13px; color: var(--purple); letter-spacing: 1px; text-transform: uppercase; font-weight: 700; }
  .eval-row .val { font-size: 17px; color: #e7eef7; line-height: 1.5; }
  .eval-chip {
    display: inline-flex; background: var(--purple-dark); color: var(--purple);
    border: 1px solid #443560; padding: 4px 12px; border-radius: 12px; font-size: 14px;
    margin: 2px 4px 2px 0;
  }
  .eval-key {
    background: rgba(192,132,252,.10); border-left: 4px solid var(--purple);
    padding: 14px 18px; border-radius: 0 8px 8px 0; font-size: 17px; line-height: 1.65;
  }

  /* Actions row at bottom */
  .actions { display: flex; gap: 12px; align-items: center; padding-top: 8px; border-top: 1px solid var(--border); }
  .actions details { flex: 1; }
  .actions details > summary { cursor: pointer; color: var(--accent); font-size: 16px; list-style: none; user-select: none; }
  .actions details > summary::after { content: " \25BC"; font-size: 12px; }
  .actions details[open] > summary::after { content: " \25B2"; }
  .abstract { color: #b6c1d1; font-size: 16px; line-height: 1.65; margin-top: 6px; max-height: 260px; overflow-y: auto; }
  .btn-read {
    background: #1d2530; color: #b6c1d1; border: 1px solid var(--border);
    padding: 6px 16px; border-radius: 6px; font-size: 16px; cursor: pointer;
  }
  .btn-read:hover { background: #2a3340; color: var(--fg); }

  /* Bottom navigation */
  .navbar {
    position: fixed; bottom: 0; left: 0; right: 0;
    background: rgba(11,14,19,.92); backdrop-filter: blur(8px);
    border-top: 1px solid var(--border);
    padding: 10px 24px; display: flex; align-items: center; gap: 16px; z-index: 10;
  }
  .nav-btn {
    background: var(--panel); color: var(--fg); border: 1px solid var(--border);
    width: 36px; height: 36px; border-radius: 8px; cursor: pointer; font-size: 21px; line-height: 1;
  }
  .nav-btn:hover { border-color: var(--border-hi); }
  .nav-btn:disabled { opacity: .3; cursor: not-allowed; }
  .counter { font-family: ui-monospace, monospace; font-size: 17px; color: var(--muted); min-width: 60px; text-align: center; }
  .dots { display: flex; gap: 6px; flex-wrap: wrap; }
  .dot {
    width: 24px; height: 6px; border-radius: 3px; background: var(--border); cursor: pointer; transition: background .2s;
  }
  .dot.active { background: var(--accent); }
  .dot.read { background: var(--green-dark); }
  .help { font-size: 14px; color: var(--muted); margin-left: auto; }
  .help kbd { background: var(--panel); border: 1px solid var(--border); padding: 1px 6px; border-radius: 4px; font-family: ui-monospace, monospace; font-size: 13px; }

  /* ---------- Mobile (<=640px) ---------- */
  @media (max-width: 640px) {
    header { padding: 10px 12px; gap: 8px; }
    header h1 { font-size: 20px; }
    header .sub { font-size: 12px; flex-basis: 100%; order: 3; }
    header .spacer { display: none; }
    header .chip { font-size: 11px; padding: 2px 8px; order: 4; }
    .settings-btn span { display: none; }
    .settings-btn { padding: 6px 10px; }
    #refreshBtn { font-size: 14px; padding: 7px 12px; }
    .slide { padding: 14px 12px; gap: 10px; }
    .tldr { font-size: 17px; padding: 12px 14px; line-height: 1.55; }
    .tldr .label { font-size: 11px; letter-spacing: 1px; }
    .title-row h2 { font-size: 19px; }
    .title-row .authors { font-size: 13px; }
    .kw-row { gap: 5px; }
    .badge { font-size: 12px; padding: 2px 8px; }
    .id-line { margin-left: 0; width: 100%; padding-top: 6px; font-size: 12px; line-height: 1.6; }
    .id-line a { margin-left: 0; margin-right: 12px; }
    .me-grid { grid-template-columns: 1fr; gap: 12px; }
    .panel { padding: 14px; }
    .hero-fig { max-height: 240px; padding: 8px; }
    .hero-fig img { max-height: 220px; }
    .hero-cap { font-size: 12px; }
    .essence { font-size: 15px; padding: 10px 12px; }
    .insight-header { font-size: 13px; }
    .insight-block { font-size: 16px; padding: 20px 22px; min-height: 130px; line-height: 1.6; }
    .insight-block::after { font-size: 90px; top: -20px; }
    .pipeline { padding: 12px; }
    .pipeline .step { padding: 8px 12px; font-size: 13px; }
    .pipeline .arrow { font-size: 16px; }
    .eval-row .val { font-size: 15px; }
    .eval-chip { font-size: 12px; padding: 3px 10px; }
    .eval-key { font-size: 15px; padding: 12px 14px; }
    .abstract { font-size: 14px; max-height: 200px; }
    .btn-read, .modal-btn { min-height: 40px; }  /* touch target */
    .cfg-del { padding: 8px 10px; }
  }
</style>
</head>
<body>

<header>
  <h1>Today's Papers</h1>
  <span class="sub">{{ date }} &middot; {{ papers|length }} picked &middot; <span id="readcount">0</span> read &middot; {{ remaining }} queued</span>
  <span class="spacer"></span>
  <button class="settings-btn" onclick="openCfg()" title="検索キーワード設定">
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round" stroke-linecap="round">
      <circle cx="12" cy="12" r="3"/>
      <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/>
    </svg>
    <span>設定</span>
  </button>
  {% if gdrive_client_id %}
  <button class="settings-btn" onclick="gdriveSignIn()" id="gdriveBtn" title="Google Drive でDismiss ログを同期">
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round" stroke-linecap="round">
      <path d="M15 3h4a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2h-4"/>
      <polyline points="10 17 15 12 10 7"/>
      <line x1="15" y1="12" x2="3" y2="12"/>
    </svg>
    <span id="gdriveLabel">同期</span>
  </button>
  {% endif %}
  <span class="chip">arXiv + ACM (OA)</span>
</header>

<div class="modal-bg" id="cfgModal" onclick="if(event.target===this)closeCfg()">
  <div class="modal">
    <div class="modal-head">
      <h2>検索キーワード設定</h2>
      <button class="modal-x" onclick="closeCfg()" title="閉じる">×</button>
    </div>
    <p class="modal-hint">編集内容は次回「論文を更新」から反映されます。元の設定は <code>config.yaml.bak</code> に保存されます。</p>
    <div id="cfgList"></div>
    <button class="modal-btn ghost full" onclick="addCategory()">＋ カテゴリを追加</button>
    <div class="modal-actions">
      <button class="modal-btn ghost" onclick="closeCfg()">キャンセル</button>
      <button class="modal-btn primary" onclick="saveCfg()" id="cfgSaveBtn">保存</button>
    </div>
    <p class="modal-status" id="cfgStatus"></p>
  </div>
</div>

<div id="refreshStatus" class="hidden"></div>
<div class="progress"><div id="progressbar" style="width:0%"></div></div>

<main class="slides" id="slides">
{% for p in papers %}
  <section class="slide" data-id="{{ p.id }}" data-idx="{{ loop.index0 }}">

    {% if p.tldr %}
    <div class="tldr">
      <span class="label">この論文がやりたいこと</span>
      {{ p.tldr }}
    </div>
    {% endif %}

    <div class="title-row">
      <h2><a href="{{ p.url }}" target="_blank" rel="noopener">{{ p.title }}</a></h2>
      <p class="authors">{{ p.authors_short }}</p>
    </div>

    <div class="kw-row">
      <span class="badge {{ 'badge-src-arxiv' if p.source=='arxiv' else 'badge-src-acm' }}">{{ p.source|upper }}</span>
      {% for k in p.keywords %}<span class="badge badge-keyword">{{ k }}</span>{% endfor %}
      {% for c in p.cat_badges %}<span class="badge badge-cat">{{ c }}</span>{% endfor %}
      <span class="id-line">
        {% if p.published %}{{ p.published[:10] }}{% endif %}
        {% if p.doi %}<a href="https://doi.org/{{ p.doi }}" target="_blank" rel="noopener">doi:{{ p.doi }}</a>{% endif %}
        {% if p.arxiv_id %}<a href="https://arxiv.org/abs/{{ p.arxiv_id }}" target="_blank" rel="noopener">arXiv:{{ p.arxiv_id }}</a>{% endif %}
        {% if p.pdf_url %}<a href="{{ p.pdf_url }}" target="_blank" rel="noopener">[PDF]</a>{% endif %}
      </span>
    </div>

    <div class="me-grid">
      <div class="panel method">
        <h3>Method</h3>
        {% if p.hero %}
        <div class="hero-fig"><img src="{{ p.hero.path }}" alt="figure" loading="lazy"></div>
        {% if p.hero.caption %}<p class="hero-cap">{{ p.hero.caption }}</p>{% endif %}
        {% endif %}

        {% if p.eval_datasets or p.eval_metrics %}
        <div class="eval-block">
          <h4 class="sub-h">Evaluation</h4>
          {% if p.eval_datasets %}
          <div class="eval-row">
            <span class="lbl">Datasets</span>
            <div class="val">{% for d in p.eval_datasets %}<span class="eval-chip">{{ d }}</span>{% endfor %}</div>
          </div>
          {% endif %}
          {% if p.eval_metrics %}
          <div class="eval-row">
            <span class="lbl">Metrics</span>
            <div class="val">{% for m in p.eval_metrics %}<span class="eval-chip">{{ m }}</span>{% endfor %}</div>
          </div>
          {% endif %}
        </div>
        {% endif %}
      </div>

      <div class="panel insight">
        {% if p.method_essence %}
        <div class="insight-header">
          <svg viewBox="0 0 24 24" width="26" height="26" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round" stroke-linecap="round">
            <path d="M9 18h6"/>
            <path d="M10 21h4"/>
            <path d="M12 3a7 7 0 0 0-4 12.5V18h8v-2.5A7 7 0 0 0 12 3z" fill="rgba(74,222,128,.1)"/>
          </svg>
          <span>要となるアイデア</span>
        </div>
        <div class="insight-block">{{ p.method_essence }}</div>
        {% endif %}

        {% if p.pipeline %}
        <div class="pipeline-section">
          <h4 class="sub-h sub-h-green">Pipeline</h4>
          <div class="pipeline">
            {% for s in p.pipeline %}
            <span class="step">{{ s }}</span>
            {% if not loop.last %}<span class="arrow">▶</span>{% endif %}
            {% endfor %}
          </div>
        </div>
        {% endif %}

        {% if p.eval_key_result %}
        <div class="key-result-section">
          <span class="lbl">Key Result</span>
          <div class="eval-key">{{ p.eval_key_result }}</div>
        </div>
        {% endif %}

        <button class="btn-read insight-dismiss" onclick="markRead('{{ p.id }}', this)">Read &amp; Dismiss (R)</button>
      </div>
    </div>
  </section>
{% endfor %}
</main>


<script>
const STORE = 'paper_read_v3';
const TOTAL = {{ papers|length }};
const slides = document.getElementById('slides');
let current = 0;

function getRead() { try { return JSON.parse(localStorage.getItem(STORE) || '{}'); } catch (e) { return {}; } }
function setRead(m) { localStorage.setItem(STORE, JSON.stringify(m)); }

function visibleSlides() { return [...document.querySelectorAll('.slide:not(.dismissed)')]; }

function syncCounter() {
  const m = getRead();
  let n = 0;
  document.querySelectorAll('.slide').forEach(c => { if (m[c.dataset.id]) n++; });
  const rc = document.getElementById('readcount'); if (rc) rc.textContent = n;
  const pb = document.getElementById('progressbar'); if (pb) pb.style.width = (TOTAL ? n / TOTAL * 100 : 0) + '%';
}

function goTo(i) {
  const vis = visibleSlides();
  if (vis.length === 0) return;
  current = Math.max(0, Math.min(vis.length - 1, i));
  vis[current].scrollIntoView({ behavior: 'smooth', inline: 'start', block: 'nearest' });
  syncCounter();
}
function goPrev() { goTo(current - 1); }
function goNext() { goTo(current + 1); }

function markRead(id, btn) {
  const m = getRead();
  m[id] = Date.now();
  setRead(m);
  const slide = btn.closest('.slide');
  slide.classList.add('dismissed');
  // Also push to GDrive so tomorrow's cron excludes this paper permanently
  const title = slide.querySelector('.title-row h2 a')?.textContent || '';
  if (typeof gdriveMarkDismissed === 'function') gdriveMarkDismissed(id, title);
  // After dismissing, recompute current so we don't jump weirdly
  const vis = visibleSlides();
  if (vis.length === 0) { syncCounter(); return; }
  current = Math.min(current, vis.length - 1);
  vis[current].scrollIntoView({ behavior: 'smooth', inline: 'start', block: 'nearest' });
  syncCounter();
}

slides.addEventListener('scroll', () => {
  const vis = visibleSlides();
  if (vis.length === 0) return;
  const i = Math.round(slides.scrollLeft / slides.clientWidth);
  if (i !== current && vis[i]) {
    current = i;
    syncCounter();
  }
});

document.addEventListener('keydown', e => {
  if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA') return;
  if (e.key === 'ArrowLeft')      { e.preventDefault(); goPrev(); }
  else if (e.key === 'ArrowRight'){ e.preventDefault(); goNext(); }
  else if (e.key.toLowerCase() === 'r') {
    const vis = visibleSlides();
    if (vis[current]) {
      const btn = vis[current].querySelector('.btn-read');
      if (btn) btn.click();
    }
  }
  else if (e.key === 'Enter') {
    const vis = visibleSlides();
    if (vis[current]) {
      const a = vis[current].querySelector('.title-row h2 a');
      if (a) window.open(a.href, '_blank');
    }
  }
});

// PWA: register the service worker so Chrome/Android show the install prompt
// and offline works. Runs only over http(s), never on file://.
if ('serviceWorker' in navigator && location.protocol !== 'file:') {
  window.addEventListener('load', () => {
    navigator.serviceWorker.register('/service-worker.js').catch(() => {});
  });
}

// GDrive OAuth wants a token client once the GIS script has loaded.
window.addEventListener('load', () => {
  // GIS script is async; give it a beat, then poll a few times.
  let tries = 0;
  const iv = setInterval(() => {
    if (window.google?.accounts?.oauth2) {
      clearInterval(iv);
      gdriveInit();
    } else if (++tries > 20) {
      clearInterval(iv);
    }
  }, 200);
});

window.addEventListener('DOMContentLoaded', () => {
  if (location.protocol === 'file:') {
    const b = document.createElement('div');
    b.style.cssText = 'background:#7a1d1d;color:#fff;padding:14px 20px;text-align:center;font-size:15px;line-height:1.6;font-weight:500;border-bottom:2px solid #b91c1c';
    b.innerHTML = '⚠ このページは <code style="background:rgba(0,0,0,.3);padding:2px 8px;border-radius:4px;font-family:ui-monospace,monospace">file://</code> で開かれているため、設定保存・更新ボタンが動作しません。<br>' +
      'まず <code style="background:rgba(0,0,0,.3);padding:2px 8px;border-radius:4px;font-family:ui-monospace,monospace">setup.bat</code> または <code style="background:rgba(0,0,0,.3);padding:2px 8px;border-radius:4px;font-family:ui-monospace,monospace">start.bat</code> を起動し、<a href="http://localhost:8770/" style="color:#fbbf24;text-decoration:underline">http://localhost:8770/</a> をブックマークしてください。';
    document.body.prepend(b);
  }
  const m = getRead();
  document.querySelectorAll('.slide').forEach(c => { if (m[c.dataset.id]) c.classList.add('dismissed'); });
  // Position at first visible
  const vis = visibleSlides();
  if (vis.length) { vis[0].scrollIntoView({ inline: 'start' }); }
  syncCounter();
});

// --- Settings modal (interest keywords) ---
async function openCfg() {
  const status = document.getElementById('cfgStatus');
  status.className = 'modal-status'; status.textContent = '';
  let data;
  try { data = await fetch('/api/config').then(r => r.json()); }
  catch (e) { alert('設定の読み込みに失敗。server.py が起動していますか?'); return; }
  const list = document.getElementById('cfgList');
  list.innerHTML = '';
  const interests = data.interests || {};
  if (Object.keys(interests).length === 0) {
    list.appendChild(makeCategoryRow('', 1.0, []));
  } else {
    for (const [name, spec] of Object.entries(interests)) {
      list.appendChild(makeCategoryRow(name, spec.weight ?? 1.0, spec.keywords || []));
    }
  }
  document.getElementById('cfgModal').classList.add('open');
}
function closeCfg() { document.getElementById('cfgModal').classList.remove('open'); }
function makeCategoryRow(name, weight, keywords) {
  const div = document.createElement('div');
  div.className = 'cfg-cat';
  const kwText = Array.isArray(keywords) ? keywords.join(', ') : String(keywords || '');
  div.innerHTML = `
    <div class="cfg-row1">
      <input class="cfg-name" value="${name.replace(/"/g, '&quot;')}" placeholder="カテゴリ名 (例: VR_XR_HCI)">
      <span class="cfg-weight-lb">重み</span>
      <input class="cfg-weight" type="number" step="0.1" min="0" max="5" value="${weight}">
      <button class="cfg-del" onclick="this.closest('.cfg-cat').remove()" title="このカテゴリを削除">×</button>
    </div>
    <textarea class="cfg-kw" placeholder="キーワードをカンマ区切りで入力 (例: vr, xr, virtual reality, hci)"></textarea>
  `;
  div.querySelector('.cfg-kw').value = kwText;
  return div;
}
function addCategory() {
  document.getElementById('cfgList').appendChild(makeCategoryRow('', 1.0, []));
}
async function saveCfg() {
  const status = document.getElementById('cfgStatus');
  const btn = document.getElementById('cfgSaveBtn');
  const cats = document.querySelectorAll('.cfg-cat');
  const interests = {};
  for (const c of cats) {
    const name = c.querySelector('.cfg-name').value.trim();
    if (!name) continue;
    const weight = parseFloat(c.querySelector('.cfg-weight').value) || 1.0;
    const kws = c.querySelector('.cfg-kw').value
      .split(',').map(s => s.trim()).filter(Boolean);
    if (kws.length === 0) continue;
    interests[name] = { weight, keywords: kws };
  }
  if (Object.keys(interests).length === 0) {
    status.className = 'modal-status err';
    status.textContent = '有効なカテゴリが1つもありません (名前とキーワードを入れてください)';
    return;
  }
  btn.disabled = true;
  status.className = 'modal-status'; status.textContent = '保存中...';
  try {
    const r = await fetch('/api/config', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ interests }),
    });
    const result = await r.json();
    if (r.ok) {
      status.className = 'modal-status ok';
      status.textContent = `保存しました (${result.categories.length}カテゴリ)。次回「論文を更新」から反映されます。`;
      setTimeout(closeCfg, 1200);
    } else {
      status.className = 'modal-status err';
      status.textContent = '保存失敗: ' + (result.error || r.status);
    }
  } catch (e) {
    status.className = 'modal-status err';
    status.textContent = 'リクエスト失敗: ' + e.message;
  }
  btn.disabled = false;
}
// ESC closes modal; S opens it
document.addEventListener('keydown', e => {
  if (e.key === 'Escape' && document.getElementById('cfgModal').classList.contains('open')) {
    closeCfg();
  }
});

// --- GDrive dismiss log sync (optional) ---
// When GDRIVE_CLIENT_ID is baked in by render.py, dismissed papers are also
// synced to Google Drive so the next GitHub Actions run excludes them.
const GDRIVE_CLIENT_ID = {{ gdrive_client_id | tojson }};
const GDRIVE_FILE = "paper-collector-dismissed.json";
const GDRIVE_SCOPE = "https://www.googleapis.com/auth/drive.file";
const GDRIVE_TOKEN_KEY = "gdrive_token_v1";
let gdriveTokenClient = null;
let gdriveToken = null;   // { access_token, expires_at }

function gdriveTokenValid() { return gdriveToken && gdriveToken.expires_at > Date.now() + 30_000; }

function gdriveLoadCached() {
  try {
    const t = JSON.parse(sessionStorage.getItem(GDRIVE_TOKEN_KEY) || "null");
    if (t && t.expires_at > Date.now()) gdriveToken = t;
  } catch (e) {}
}

function gdriveSaveCached() {
  if (gdriveToken) sessionStorage.setItem(GDRIVE_TOKEN_KEY, JSON.stringify(gdriveToken));
}

function gdriveInit() {
  if (!GDRIVE_CLIENT_ID || !window.google?.accounts?.oauth2) return;
  gdriveLoadCached();
  gdriveTokenClient = google.accounts.oauth2.initTokenClient({
    client_id: GDRIVE_CLIENT_ID,
    scope: GDRIVE_SCOPE,
    callback: (resp) => {
      if (!resp.access_token) return;
      gdriveToken = {
        access_token: resp.access_token,
        expires_at: Date.now() + (resp.expires_in ? resp.expires_in * 1000 : 3600_000),
      };
      gdriveSaveCached();
      const lbl = document.getElementById('gdriveLabel');
      if (lbl) lbl.textContent = '接続済';
      flushDismissQueue();
    },
  });
  const lbl = document.getElementById('gdriveLabel');
  if (lbl) lbl.textContent = gdriveTokenValid() ? '接続済' : '同期';
}

function gdriveSignIn() {
  if (!gdriveTokenClient) return alert('GDrive クライアントが未初期化です');
  gdriveTokenClient.requestAccessToken({ prompt: gdriveTokenValid() ? '' : 'consent' });
}

async function gdriveFindFileId(token) {
  const q = encodeURIComponent(`name='${GDRIVE_FILE}' and trashed=false`);
  const r = await fetch(`https://www.googleapis.com/drive/v3/files?q=${q}&fields=files(id)&spaces=drive`, {
    headers: { Authorization: `Bearer ${token}` },
  });
  if (!r.ok) throw new Error('list ' + r.status);
  const j = await r.json();
  return j.files?.[0]?.id || null;
}

async function gdriveReadFile(token, fileId) {
  const r = await fetch(`https://www.googleapis.com/drive/v3/files/${fileId}?alt=media`, {
    headers: { Authorization: `Bearer ${token}` },
  });
  if (!r.ok) return {};
  try { return await r.json(); } catch (e) { return {}; }
}

async function gdriveWriteFile(token, fileId, body) {
  const url = fileId
    ? `https://www.googleapis.com/upload/drive/v3/files/${fileId}?uploadType=media`
    : `https://www.googleapis.com/upload/drive/v3/files?uploadType=media`;
  const method = fileId ? 'PATCH' : 'POST';
  const r = await fetch(url, {
    method,
    headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  return r.ok;
}

async function gdriveCreateFile(token) {
  const r = await fetch('https://www.googleapis.com/drive/v3/files', {
    method: 'POST',
    headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
    body: JSON.stringify({ name: GDRIVE_FILE }),
  });
  if (!r.ok) throw new Error('create ' + r.status);
  return (await r.json()).id;
}

// Queue for when the user isn't signed in yet
function dismissQueue() {
  try { return JSON.parse(localStorage.getItem('gdrive_pending') || '{}'); }
  catch (e) { return {}; }
}
function setDismissQueue(q) { localStorage.setItem('gdrive_pending', JSON.stringify(q)); }

async function flushDismissQueue() {
  const q = dismissQueue();
  if (Object.keys(q).length === 0) return;
  if (!gdriveTokenValid()) return;
  try {
    let fileId = await gdriveFindFileId(gdriveToken.access_token);
    if (!fileId) fileId = await gdriveCreateFile(gdriveToken.access_token);
    const cur = await gdriveReadFile(gdriveToken.access_token, fileId);
    const merged = { ...cur, ...q };
    if (await gdriveWriteFile(gdriveToken.access_token, fileId, merged)) {
      setDismissQueue({});
    }
  } catch (e) { console.warn('[gdrive] flush failed', e); }
}

async function gdriveMarkDismissed(id, title) {
  // Always queue; flush attempts sync if signed in.
  const q = dismissQueue();
  q[id] = { ts: new Date().toISOString(), title: (title || '').slice(0, 200) };
  setDismissQueue(q);
  if (gdriveTokenValid()) flushDismissQueue();
}
</script>
</body>
</html>
"""


# ---------- main ----------

def authors_short(a: str | None, k: int = 3) -> str:
    if not a:
        return ""
    parts = [x.strip() for x in a.split(",") if x.strip()]
    if len(parts) <= k:
        return ", ".join(parts)
    return ", ".join(parts[:k]) + f", +{len(parts) - k} more"


def main() -> int:
    cfg = yaml.safe_load(CONFIG_FILE.read_text(encoding="utf-8"))
    n = cfg["display"]["papers_per_day"]
    ensure_schema()

    print("Selecting candidates ...")
    cands = select_candidates(cfg)
    print(f"  {len(cands)} candidates after keyword pre-filter")
    if not cands:
        print("No unread, keyword-matching, OA papers. Run fetch.py first or widen keywords.")
        return 1

    print("Enriching via Semantic Scholar ...")
    s2_enrich(cands)
    s2_tldr = sum(1 for c in cands if c.get("tldr"))
    extra = apply_tldr_fallback(cands)
    # arXiv assigns DOIs as 10.48550/arXiv.<id> for all papers since 2022
    for p in cands:
        if not p.get("doi") and p.get("arxiv_id"):
            p["doi"] = f"10.48550/arXiv.{p['arxiv_id'].split('v')[0]}"
    persist_enrichment(cands)
    print(f"  TLDRs: {s2_tldr} from S2 + {extra} from abstract fallback (total {s2_tldr + extra}/{len(cands)})")

    picked, remaining = finalize_picks(cands, cfg, n)
    print(f"Picked {len(picked)} papers, {remaining} more queued.")

    if (cfg.get("llm") or {}).get("enabled"):
        print(f"Refining TLDRs via Ollama ({cfg['llm'].get('model')}) ...")
        refined = llm_refine(picked, cfg)
        persist_llm_tldrs(picked)
        print(f"  LLM refined: {refined}/{len(picked)}")

    img_cfg = cfg.get("images") or {}
    hero_max = img_cfg.get("hero_max_px", 900)
    hero_q   = img_cfg.get("hero_quality", 80)
    more_max = img_cfg.get("more_max_px", 400)
    more_q   = img_cfg.get("more_quality", 75)
    total_kb = 0
    for i, p in enumerate(picked, 1):
        print(f"  [{i:2d}] {(p.get('title') or '')[:80]}")
        figs: list[dict] = []
        if p.get("pdf_url"):
            pdf = PDF_DIR / f"{p['id']}.pdf"
            if download_pdf(p["pdf_url"], pdf):
                figs = extract_figures(pdf, FIG_DIR / p["id"])
            time.sleep(0.8)
        for j, f in enumerate(figs):
            src: Path = f["path"]
            thumb = FIG_DIR / f"{p['id']}.thumb{j}.jpg"
            max_px = hero_max if j == 0 else more_max
            quality = hero_q if j == 0 else more_q
            if compress_image(src, thumb, max_px, quality):
                try:
                    src.unlink()  # keep only the compressed thumbnail
                except OSError:
                    pass
                f["path"] = thumb.relative_to(PUBLIC).as_posix()
                total_kb += thumb.stat().st_size // 1024
            else:
                f["path"] = src.relative_to(PUBLIC).as_posix()
                total_kb += src.stat().st_size // 1024
        p["hero"] = figs[0] if figs else None
        p["more_figs"] = figs[1:] if len(figs) > 1 else []
        p["abstract"] = (p.get("abstract") or "")[:1500]
        p["authors_short"] = authors_short(p.get("authors"), 3)
        fs = p.get("fields") or []
        p["fields_short"] = fs[:3] if isinstance(fs, list) else []

        # Expand structured LLM fields for the slide template
        data = p.get("llm_data")
        if not data and p.get("extracted_json"):
            try:
                data = json.loads(p["extracted_json"])
            except (TypeError, json.JSONDecodeError):
                data = None
        data = data or {}
        p["keywords"] = data.get("keywords") or []
        p["method_essence"] = data.get("method_essence") or ""
        p["pipeline"] = data.get("pipeline") or []
        ev = data.get("evaluation") or {}
        p["eval_datasets"] = ev.get("datasets") or []
        p["eval_metrics"] = ev.get("metrics") or []
        p["eval_key_result"] = ev.get("key_result") or ""

    print(f"Total figure payload: {total_kb} KB across {sum(len(p.get('more_figs',[])) + (1 if p.get('hero') else 0) for p in picked)} images")
    # Prefer env for the OAuth client id (set in GitHub Actions); fall back to config.
    gdrive_client_id = (
        os.environ.get("GDRIVE_CLIENT_ID")
        or ((cfg.get("gdrive") or {}).get("client_id") or "")
    ).strip()
    html = Template(HTML).render(
        date=datetime.now().strftime("%Y-%m-%d (%a)"),
        papers=picked,
        remaining=remaining,
        gdrive_client_id=gdrive_client_id,
    )
    out = PUBLIC / "index.html"
    out.write_text(html, encoding="utf-8")
    arch = PUBLIC / "archive"
    arch.mkdir(exist_ok=True)
    (arch / f"{datetime.now().strftime('%Y-%m-%d')}.html").write_text(html, encoding="utf-8")
    print(f"Wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
