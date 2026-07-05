"""Daily 6 AM update + at-logon server check.

Invoked by the Windows scheduled task `PaperCollectorDaily`.

Behavior:
  - If last fetch was more than STALE_HOURS hours ago, run cleanup + fetch + render.
  - Always ensure the local server (port 8770) is up; start it (windowless) if not.
  - Log everything to data/auto.log so missed-fire conditions are observable.
"""
from __future__ import annotations

import socket
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).parent
DB = ROOT / "data" / "papers.db"
PORT = 8770
STALE_HOURS = 4
LOG = ROOT / "data" / "auto.log"


def log(msg: str) -> None:
    LOG.parent.mkdir(exist_ok=True)
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass
    print(line)


def last_fetch() -> datetime | None:
    if not DB.exists():
        return None
    try:
        conn = sqlite3.connect(DB)
        r = conn.execute("SELECT MAX(fetched_at) FROM papers").fetchone()
        conn.close()
        if r and r[0]:
            return datetime.fromisoformat(r[0])
    except Exception:
        pass
    return None


def need_fetch() -> bool:
    last = last_fetch()
    if not last:
        return True
    return (datetime.now(timezone.utc) - last) > timedelta(hours=STALE_HOURS)


def run_pipeline() -> None:
    for script in ["cleanup.py", "fetch.py", "render.py"]:
        log(f"running {script}")
        try:
            proc = subprocess.run(
                [sys.executable, str(ROOT / script)],
                cwd=str(ROOT),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=20 * 60,
            )
            if proc.returncode != 0:
                log(f"  {script} exit {proc.returncode}: {(proc.stdout or '')[-300:]}")
        except subprocess.TimeoutExpired:
            log(f"  {script} timed out")


def server_running() -> bool:
    s = socket.socket()
    s.settimeout(0.5)
    try:
        s.connect(("127.0.0.1", PORT))
        return True
    except OSError:
        return False
    finally:
        s.close()


def start_server_detached() -> None:
    """Start server.py in the background without a console window."""
    pyw = Path(sys.executable).parent / "pythonw.exe"
    exe = str(pyw if pyw.exists() else sys.executable)
    flags = 0
    if sys.platform == "win32":
        # DETACHED_PROCESS=0x00000008, CREATE_NEW_PROCESS_GROUP=0x00000200
        flags = 0x00000008 | 0x00000200
    subprocess.Popen(
        [exe, str(ROOT / "server.py"), "--no-browser"],
        cwd=str(ROOT),
        creationflags=flags,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
    )


def main() -> int:
    log("=== auto_update start ===")
    if need_fetch():
        run_pipeline()
        log("pipeline complete")
    else:
        log("fetch skipped (last fetch is recent)")
    if not server_running():
        start_server_detached()
        log("server started detached")
    else:
        log("server already running")
    log("=== auto_update done ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
