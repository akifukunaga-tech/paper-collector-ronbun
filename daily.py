"""One-shot pipeline (cleanup -> fetch -> render). Useful for cron or CLI;
for interactive use prefer start.bat which launches the server with the
in-page refresh button."""
from __future__ import annotations

import subprocess
import sys
import webbrowser
from pathlib import Path

ROOT = Path(__file__).parent


def run(script: str) -> None:
    print(f"\n=== {script} ===")
    subprocess.check_call([sys.executable, str(ROOT / script)])


def main() -> int:
    run("cleanup.py")
    run("fetch.py")
    run("render.py")
    index = ROOT / "index.html"
    if index.exists():
        webbrowser.open(index.as_uri())
    return 0


if __name__ == "__main__":
    sys.exit(main())
