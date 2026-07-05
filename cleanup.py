"""Physically delete shown papers (DB rows + PDF/figure files).

The persistent seen_keys log lives forever so re-fetched duplicates
won't show again. Runs at the start of daily.py.
"""
from __future__ import annotations

import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

CODE_ROOT = Path(__file__).parent
ROOT = Path(os.environ["PAPER_ROOT"]) if os.environ.get("PAPER_ROOT") else CODE_ROOT
DATA = ROOT / "data"
PDF_DIR = DATA / "pdfs"
FIG_DIR = DATA / "figures"
DB = DATA / "papers.db"


def cleanup() -> tuple[int, int]:
    if not DB.exists():
        return 0, 0
    midnight = (
        datetime.now(timezone.utc)
        .replace(hour=0, minute=0, second=0, microsecond=0)
        .isoformat()
    )
    conn = sqlite3.connect(DB)
    rows = conn.execute(
        "SELECT id FROM papers WHERE shown_at IS NOT NULL AND shown_at < ?",
        (midnight,),
    ).fetchall()
    ids = [r[0] for r in rows]
    files_removed = 0
    for pid in ids:
        for d in (PDF_DIR, FIG_DIR):
            if not d.exists():
                continue
            for f in d.glob(f"{pid}.*"):
                try:
                    f.unlink()
                    files_removed += 1
                except OSError:
                    pass
    if ids:
        conn.executemany("DELETE FROM papers WHERE id = ?", [(pid,) for pid in ids])
        conn.commit()
    conn.close()
    return len(ids), files_removed


def main() -> int:
    rows, files = cleanup()
    print(f"cleanup: dropped {rows} paper rows, removed {files} files")
    return 0


if __name__ == "__main__":
    sys.exit(main())
