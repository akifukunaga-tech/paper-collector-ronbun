"""Local HTTP server for the paper collector.

- Serves index.html and figures so the page can be opened from a bookmark
- POST /api/refresh runs cleanup + fetch + render in a background thread
- GET  /api/status reports progress so the UI can show what's happening

Bookmark http://localhost:8770/ in your browser; start this via start.bat.
"""
from __future__ import annotations

import base64
import hmac
import json
import os
import subprocess
import sys
import threading
import time
import urllib.parse
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

CODE_ROOT = Path(__file__).parent
ROOT = Path(os.environ["PAPER_ROOT"]) if os.environ.get("PAPER_ROOT") else CODE_ROOT
ROOT.mkdir(parents=True, exist_ok=True)
CONFIG_FILE = ROOT / "config.yaml"
if not CONFIG_FILE.exists() and (CODE_ROOT / "config.yaml").exists():
    CONFIG_FILE.write_bytes((CODE_ROOT / "config.yaml").read_bytes())

PORT = int(os.environ.get("PORT", "8770"))
CLOUD_MODE = "PORT" in os.environ or "PAPER_ROOT" in os.environ
BIND = "0.0.0.0" if CLOUD_MODE else "127.0.0.1"


def _load_auth() -> tuple[str, str] | None:
    """Priority: env AUTH_USERNAME/AUTH_PASSWORD (cloud) > config.yaml server.auth.
    In cloud mode we FAIL loud if no auth is configured, to avoid accidental exposure."""
    u = os.environ.get("AUTH_USERNAME", "").strip()
    p = os.environ.get("AUTH_PASSWORD", "").strip()
    if u and p:
        return u, p
    try:
        import yaml
        cfg = yaml.safe_load(CONFIG_FILE.read_text(encoding="utf-8")) or {}
        a = ((cfg.get("server") or {}).get("auth") or {})
        u2, p2 = (a.get("username") or "").strip(), (a.get("password") or "").strip()
        if u2 and p2:
            return u2, p2
    except Exception:
        pass
    return None


AUTH = _load_auth()
AUTH_STATUS = "enabled" if AUTH else "disabled (localhost use only)"
if CLOUD_MODE and not AUTH:
    print("!! CLOUD_MODE detected but no auth configured. Refusing to start.")
    print("   Set env vars AUTH_USERNAME and AUTH_PASSWORD, or config.yaml server.auth.")
    sys.exit(2)

STATE: dict = {
    "status": "idle",          # idle | running | error
    "stage": None,             # cleanup | fetch | render | done
    "log": [],
    "started_at": None,
    "finished_at": None,
}
LOCK = threading.Lock()

CTYPE = {
    ".html": "text/html; charset=utf-8",
    ".css":  "text/css; charset=utf-8",
    ".js":   "application/javascript; charset=utf-8",
    ".png":  "image/png",
    ".jpg":  "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif":  "image/gif",
    ".svg":  "image/svg+xml",
    ".pdf":  "application/pdf",
    ".json": "application/json; charset=utf-8",
}


def run_pipeline() -> None:
    """Run cleanup -> fetch -> render, capturing stdout into STATE['log']."""
    with LOCK:
        if STATE["status"] == "running":
            return
        STATE["status"] = "running"
        STATE["stage"] = None
        STATE["log"] = []
        STATE["started_at"] = _ts()
        STATE["finished_at"] = None
    try:
        for stage, script in [("cleanup", "cleanup.py"), ("fetch", "fetch.py"), ("render", "render.py")]:
            STATE["stage"] = stage
            STATE["log"].append(f"=== {stage} ===")
            env = {"PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
            proc = subprocess.Popen(
                [sys.executable, "-u", str(CODE_ROOT / script)],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                env={**_env_passthrough(), **env},
            )
            assert proc.stdout is not None
            for line in proc.stdout:
                line = line.rstrip()
                if line:
                    STATE["log"].append(line)
                    if len(STATE["log"]) > 400:
                        del STATE["log"][:200]
            proc.wait()
            if proc.returncode != 0:
                STATE["status"] = "error"
                STATE["log"].append(f"[{stage}] exit code {proc.returncode}")
                STATE["finished_at"] = _ts()
                return
        STATE["stage"] = "done"
        STATE["status"] = "idle"
        STATE["finished_at"] = _ts()
    except Exception as e:
        STATE["status"] = "error"
        STATE["log"].append(f"ERROR: {e!r}")
        STATE["finished_at"] = _ts()


def _ts() -> str:
    from datetime import datetime
    return datetime.now().strftime("%H:%M:%S")


def _env_passthrough() -> dict:
    import os
    return dict(os.environ)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args, **kwargs):  # silence default access log
        pass

    def _is_pwa_asset(self, path: str) -> bool:
        """Public PWA assets. Serving these without auth so service-worker
        registration and manifest fetch succeed before the browser has
        cached Basic-auth credentials."""
        return (
            path == "/manifest.json"
            or path == "/service-worker.js"
            or path.startswith("/icons/")
        )

    def _check_auth(self) -> bool:
        """Return True if request is authorized. In cloud mode, always require
        auth (except public PWA assets). In local mode, bypass for direct-
        localhost requests but enforce for tunneled requests (cloudflared/
        ngrok connect from localhost but carry forwarded-IP headers)."""
        if not AUTH:
            return True
        path = urllib.parse.urlparse(self.path).path
        if self._is_pwa_asset(path):
            return True
        if not CLOUD_MODE:
            peer = self.client_address[0]
            is_local = peer in ("127.0.0.1", "::1", "localhost")
            tunneled = bool(
                self.headers.get("Cf-Connecting-Ip")
                or self.headers.get("X-Forwarded-For")
                or self.headers.get("X-Real-Ip")
                or self.headers.get("Ngrok-Trace-Id")
            )
            if is_local and not tunneled:
                return True
        hdr = self.headers.get("Authorization", "")
        if not hdr.startswith("Basic "):
            return False
        try:
            creds = base64.b64decode(hdr[6:]).decode("utf-8")
        except Exception:
            return False
        return hmac.compare_digest(creds, f"{AUTH[0]}:{AUTH[1]}")

    def _send_auth_challenge(self) -> None:
        body = b"Authentication required."
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="Paper Collector"')
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _send(self, code: int, body: bytes | str, ctype: str = "text/html; charset=utf-8") -> None:
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _send_json(self, code: int, obj) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False), "application/json; charset=utf-8")

    def do_GET(self) -> None:
        if not self._check_auth():
            self._send_auth_challenge(); return
        path = urllib.parse.urlparse(self.path).path
        if path == "/":
            path = "/index.html"
        if path == "/api/status":
            self._send_json(200, {
                "status": STATE["status"],
                "stage": STATE["stage"],
                "tail": STATE["log"][-12:],
                "lines": len(STATE["log"]),
                "started_at": STATE["started_at"],
                "finished_at": STATE["finished_at"],
            })
            return
        if path == "/api/config":
            try:
                import yaml
                cfg = yaml.safe_load(CONFIG_FILE.read_text(encoding="utf-8"))
                self._send_json(200, {"interests": cfg.get("interests") or {}})
            except Exception as e:
                self._send_json(500, {"error": str(e)})
            return
        # Static file. Check ROOT first (runtime output: index.html, data/figures/),
        # fall back to CODE_ROOT (shipped assets: icons/, manifest.json, service-worker.js).
        rel = urllib.parse.unquote(path.lstrip("/"))
        for base in (ROOT, CODE_ROOT):
            try:
                target = (base / rel).resolve()
                if not str(target).startswith(str(base.resolve())):
                    continue
                if target.is_file():
                    ctype = "application/manifest+json" if target.name == "manifest.json" \
                        else CTYPE.get(target.suffix.lower(), "application/octet-stream")
                    self._send(200, target.read_bytes(), ctype)
                    return
            except OSError:
                continue
        if path == "/index.html":
            self._send(200, _welcome_html(), "text/html; charset=utf-8")
            return
        self.send_error(404)

    def do_POST(self) -> None:
        if not self._check_auth():
            self._send_auth_challenge(); return
        if self.path == "/api/refresh":
            if STATE["status"] == "running":
                self._send_json(409, {"error": "already running"})
                return
            threading.Thread(target=run_pipeline, daemon=True).start()
            self._send_json(202, {"started": True})
            return
        if self.path == "/api/config":
            length = int(self.headers.get("Content-Length", 0) or 0)
            body = self.rfile.read(length).decode("utf-8")
            try:
                data = json.loads(body)
            except json.JSONDecodeError:
                self._send_json(400, {"error": "invalid json"}); return
            new_interests = data.get("interests")
            if not isinstance(new_interests, dict) or not new_interests:
                self._send_json(400, {"error": "interests dict required, non-empty"}); return
            cleaned: dict = {}
            for name, spec in new_interests.items():
                if not isinstance(name, str) or not name.strip():
                    continue
                if not isinstance(spec, dict):
                    continue
                kws_raw = spec.get("keywords") or []
                if isinstance(kws_raw, str):
                    kws = [k.strip() for k in kws_raw.split(",")]
                else:
                    kws = [str(k).strip() for k in kws_raw]
                kws = [k for k in kws if k]
                try:
                    weight = float(spec.get("weight", 1.0))
                except (TypeError, ValueError):
                    weight = 1.0
                weight = max(0.0, min(5.0, weight))
                cleaned[name.strip()] = {"weight": weight, "keywords": kws}
            if not cleaned:
                self._send_json(400, {"error": "no valid categories"}); return
            try:
                import yaml
                cfg_path = CONFIG_FILE
                cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
                cfg["interests"] = cleaned
                # Backup the prior config in case the user wants to revert
                backup = CONFIG_FILE.with_suffix(".yaml.bak")
                backup.write_text(cfg_path.read_text(encoding="utf-8"), encoding="utf-8")
                cfg_path.write_text(
                    yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False, width=120),
                    encoding="utf-8",
                )
                self._send_json(200, {"saved": True, "categories": list(cleaned.keys())})
            except Exception as e:
                self._send_json(500, {"error": str(e)})
            return
        self.send_error(404)


def _welcome_html() -> str:
    return """<!doctype html>
<html lang="ja"><head><meta charset="utf-8"><title>Paper Collector</title>
<script src="https://cdn.tailwindcss.com"></script>
<style>body{background:#0b0e13;color:#e7eef7;font-family:sans-serif}</style>
</head><body class="p-10 text-center">
<h1 class="text-3xl font-bold mb-4">Paper Collector</h1>
<p class="text-gray-400 mb-6">まだ論文が取得されていません。下のボタンで初回取得を開始してください。</p>
<button onclick="fetch('/api/refresh',{method:'POST'}).then(()=>location.reload())"
  class="bg-blue-600 hover:bg-blue-500 text-white px-6 py-3 rounded font-semibold">
  論文を取得する
</button>
</body></html>"""


def _last_render_epoch() -> float:
    """When was index.html last written? Used by the daily scheduler to decide
    whether to catch up on startup."""
    idx = ROOT / "index.html"
    return idx.stat().st_mtime if idx.exists() else 0.0


def _daily_scheduler() -> None:
    """In cloud mode, kick the pipeline once a day (env DAILY_HOUR_UTC, default 22 = 07 JST)
    and once on startup if the last render was more than 20 hours ago."""
    if not CLOUD_MODE:
        return
    try:
        target_hour = int(os.environ.get("DAILY_HOUR_UTC", "22"))
    except ValueError:
        target_hour = 22

    def worker():
        # Catch-up on startup
        age_h = (time.time() - _last_render_epoch()) / 3600
        if age_h > 20:
            print(f"[scheduler] last render was {age_h:.1f}h ago, kicking pipeline now")
            threading.Thread(target=run_pipeline, daemon=True).start()
        while True:
            now = datetime.now(timezone.utc)
            fire = now.replace(hour=target_hour, minute=0, second=0, microsecond=0)
            if fire <= now:
                fire = fire.replace(day=fire.day + 1)
            wait_s = (fire - now).total_seconds()
            print(f"[scheduler] next daily fire in {wait_s/3600:.1f}h at {fire.isoformat()}")
            time.sleep(wait_s)
            if STATE["status"] != "running":
                threading.Thread(target=run_pipeline, daemon=True).start()

    threading.Thread(target=worker, daemon=True).start()


def main() -> int:
    no_browser = "--no-browser" in sys.argv or CLOUD_MODE
    server = ThreadingHTTPServer((BIND, PORT), Handler)
    url = f"http://{'localhost' if BIND == '127.0.0.1' else BIND}:{PORT}/"
    print(f"Paper Collector serving on {url}  (mode: {'cloud' if CLOUD_MODE else 'local'})")
    print(f"Auth: {AUTH_STATUS}")
    print(f"Data root: {ROOT}")
    print("Press Ctrl+C to stop.")
    _daily_scheduler()
    if not no_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping")
        server.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
