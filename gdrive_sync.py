"""Google Drive sync for the dismissed-paper log.

Two entry points, both called from the GitHub Actions workflow:

  python gdrive_sync.py pull -> writes ./dismissed.json (empty {} if the file
                                doesn't exist on GDrive yet)

  python gdrive_sync.py push -> uploads ./dismissed.json to GDrive
                                (creates or overwrites)

Env vars (set as GitHub secrets):
  GDRIVE_CLIENT_ID       OAuth 2.0 client id (public)
  GDRIVE_CLIENT_SECRET   OAuth 2.0 client secret
  GDRIVE_REFRESH_TOKEN   long-lived refresh token from the one-time browser flow

Uses the `drive.file` scope, so we can only see files created by this app.
The file is named "paper-collector-dismissed.json" at the root of My Drive.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import requests

FILE_NAME = "paper-collector-dismissed.json"
LOCAL_FILE = Path(__file__).parent / "dismissed.json"
TOKEN_URL = "https://oauth2.googleapis.com/token"
API_ROOT = "https://www.googleapis.com/drive/v3"
UPLOAD_ROOT = "https://www.googleapis.com/upload/drive/v3"


def _access_token() -> str:
    body = {
        "client_id":     os.environ["GDRIVE_CLIENT_ID"],
        "client_secret": os.environ["GDRIVE_CLIENT_SECRET"],
        "refresh_token": os.environ["GDRIVE_REFRESH_TOKEN"],
        "grant_type":    "refresh_token",
    }
    r = requests.post(TOKEN_URL, data=body, timeout=15)
    r.raise_for_status()
    return r.json()["access_token"]


def _find_file(token: str) -> str | None:
    """Return the fileId of the dismissed log, or None if it doesn't exist yet."""
    r = requests.get(
        f"{API_ROOT}/files",
        params={
            "q": f"name='{FILE_NAME}' and trashed=false",
            "fields": "files(id,name)",
            "spaces": "drive",
        },
        headers={"Authorization": f"Bearer {token}"},
        timeout=15,
    )
    r.raise_for_status()
    files = r.json().get("files", [])
    return files[0]["id"] if files else None


def pull() -> None:
    """Download dismissed log to LOCAL_FILE. If not found upstream, write '{}'."""
    token = _access_token()
    file_id = _find_file(token)
    if not file_id:
        print(f"[gdrive] no upstream {FILE_NAME}; writing empty log")
        LOCAL_FILE.write_text("{}", encoding="utf-8")
        return
    r = requests.get(
        f"{API_ROOT}/files/{file_id}",
        params={"alt": "media"},
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )
    r.raise_for_status()
    LOCAL_FILE.write_bytes(r.content)
    try:
        n = len(json.loads(LOCAL_FILE.read_text(encoding="utf-8")))
    except json.JSONDecodeError:
        n = -1
    print(f"[gdrive] pulled {FILE_NAME} ({len(r.content)} bytes, {n} entries)")


def push() -> None:
    """Upload LOCAL_FILE to GDrive (create or update)."""
    if not LOCAL_FILE.exists():
        print(f"[gdrive] {LOCAL_FILE} missing, nothing to push")
        return
    token = _access_token()
    file_id = _find_file(token)
    body = LOCAL_FILE.read_bytes()
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    if file_id:
        r = requests.patch(
            f"{UPLOAD_ROOT}/files/{file_id}",
            params={"uploadType": "media"},
            headers=headers,
            data=body,
            timeout=30,
        )
    else:
        # Create with metadata via multipart. Simplest: two calls.
        create = requests.post(
            f"{API_ROOT}/files",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={"name": FILE_NAME},
            timeout=15,
        )
        create.raise_for_status()
        file_id = create.json()["id"]
        r = requests.patch(
            f"{UPLOAD_ROOT}/files/{file_id}",
            params={"uploadType": "media"},
            headers=headers,
            data=body,
            timeout=30,
        )
    r.raise_for_status()
    print(f"[gdrive] pushed {FILE_NAME} ({len(body)} bytes)")


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1] not in {"pull", "push"}:
        print("usage: gdrive_sync.py {pull|push}", file=sys.stderr)
        return 2
    try:
        (pull if sys.argv[1] == "pull" else push)()
    except KeyError as e:
        print(f"[gdrive] missing env var: {e}", file=sys.stderr)
        return 1
    except requests.RequestException as e:
        print(f"[gdrive] request failed: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
