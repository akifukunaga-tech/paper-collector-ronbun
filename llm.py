"""LLM abstraction. Provider is chosen by config.llm.provider:

  - ollama : local (default when running on the user's PC with Ollama)
  - groq   : cloud, free tier; requires env GROQ_API_KEY
  - claude : cloud, paid; requires env ANTHROPIC_API_KEY

The structured() call returns a normalized dict or None so callers don't
have to care which provider ran.
"""
from __future__ import annotations

import json
import os
import re

import requests


class LLMError(Exception):
    pass


PROMPT_JA = """次の英語論文abstractを読み、指定スキーマに従ったJSONのみを出力してください。説明・前置き不要。

スキーマ:
{{
  "tldr": "この論文が何をしたいかを日本語1文(60-100字)",
  "keywords": ["技術キーワード", "..."],
  "method_essence": "最も難解かつ要となる技術的アイデアを日本語1文(80-140字)",
  "pipeline": ["工程1(12字以内)", "工程2", "工程3"],
  "evaluation": {{
    "datasets": ["..."],
    "metrics": ["..."],
    "key_result": "主要結果を日本語1文。不明なら'記載なし'"
  }}
}}

ルール:
- keywords は3〜5個、専門用語のみ
- pipeline は3〜5ステップ、短いラベル
- 全フィールド必須、空値不可（不明なら'記載なし'）

abstract:
{abstract}

JSON:"""


PROMPT_EN = """Read the abstract below and output JSON only (no preface, no explanation) matching the schema.

Schema:
{{
  "tldr": "one sentence summarizing what the paper wants to do (15-25 words)",
  "keywords": ["technical keyword", "..."],
  "method_essence": "one sentence on the hardest/key technical idea",
  "pipeline": ["step1", "step2", "step3"],
  "evaluation": {{
    "datasets": ["..."],
    "metrics": ["..."],
    "key_result": "one-sentence main result; 'not stated' if missing"
  }}
}}

Rules:
- 3-5 keywords
- 3-5 short pipeline labels
- All fields required; 'not stated' when missing

abstract:
{abstract}

JSON:"""


def _prompt(abstract: str, lang: str) -> str:
    tmpl = PROMPT_JA if lang == "ja" else PROMPT_EN
    return tmpl.format(abstract=abstract[:2400])


def _normalize(data: dict) -> dict | None:
    if not isinstance(data, dict) or not data.get("tldr"):
        return None
    data["tldr"] = str(data.get("tldr", "")).strip()[:260]
    data["keywords"] = [str(k).strip()[:30] for k in (data.get("keywords") or [])][:6]
    data["method_essence"] = str(data.get("method_essence", "")).strip()[:320]
    data["pipeline"] = [str(s).strip()[:24] for s in (data.get("pipeline") or [])][:6]
    ev = data.get("evaluation") or {}
    data["evaluation"] = {
        "datasets":   [str(s).strip()[:40] for s in (ev.get("datasets") or [])][:5],
        "metrics":    [str(s).strip()[:30] for s in (ev.get("metrics") or [])][:5],
        "key_result": str(ev.get("key_result", "")).strip()[:300],
    }
    return data


def _extract_json(text: str) -> str:
    """Some models wrap JSON in ```json ... ``` or preface it. Grab the first {...} block."""
    m = re.search(r"\{[\s\S]*\}", text)
    return m.group(0) if m else text


# ---------- providers ----------

def _ollama(prompt: str, cfg: dict) -> str:
    base = cfg.get("ollama_url", "http://localhost:11434")
    model = cfg.get("model", "gemma4:e2b")
    r = requests.post(
        f"{base}/api/generate",
        json={
            "model": model,
            "prompt": prompt,
            "stream": False,
            "think": False,
            "format": "json",
            "options": {"temperature": 0.2, "num_predict": 900},
        },
        timeout=cfg.get("timeout_seconds", 180),
    )
    r.raise_for_status()
    return (r.json().get("response") or "").strip()


def _groq(prompt: str, cfg: dict) -> str:
    key = os.environ.get("GROQ_API_KEY")
    if not key:
        raise LLMError("GROQ_API_KEY not set")
    model = cfg.get("model", "gemma2-9b-it")
    r = requests.post(
        "https://api.groq.com/openai/v1/chat/completions",
        headers={"Authorization": f"Bearer {key}"},
        json={
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.2,
            "max_tokens": 900,
            "response_format": {"type": "json_object"},
        },
        timeout=cfg.get("timeout_seconds", 60),
    )
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]


def _claude(prompt: str, cfg: dict) -> str:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise LLMError("ANTHROPIC_API_KEY not set")
    model = cfg.get("model", "claude-haiku-4-5-20251001")
    r = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": model,
            "max_tokens": 900,
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=cfg.get("timeout_seconds", 60),
    )
    r.raise_for_status()
    return r.json()["content"][0]["text"]


PROVIDERS = {"ollama": _ollama, "groq": _groq, "claude": _claude}


# ---------- public API ----------

def structured(abstract: str, cfg: dict) -> dict | None:
    """One structured extraction (TL;DR + method + pipeline + evaluation).
    Returns None on any failure so the caller can fall back gracefully."""
    if not abstract or not cfg.get("enabled"):
        return None
    # env override wins over config so GitHub Actions can force groq without editing config.yaml
    provider = (os.environ.get("LLM_PROVIDER") or cfg.get("provider") or "ollama").strip()
    fn = PROVIDERS.get(provider)
    if fn is None:
        return None
    lang = cfg.get("language", "ja")
    try:
        text = fn(_prompt(abstract, lang), cfg)
        if not text:
            return None
        data = json.loads(_extract_json(text))
        return _normalize(data)
    except (requests.RequestException, json.JSONDecodeError, LLMError, KeyError, IndexError):
        return None


def health(cfg: dict) -> tuple[bool, str]:
    """Quick reachability check for the configured provider."""
    if not cfg.get("enabled"):
        return False, "llm.enabled = false"
    provider = cfg.get("provider", "ollama")
    if provider == "ollama":
        try:
            r = requests.get(f"{cfg.get('ollama_url', 'http://localhost:11434')}/api/tags", timeout=3)
            return r.status_code == 200, f"ollama {r.status_code}"
        except requests.RequestException as e:
            return False, f"ollama unreachable: {e}"
    if provider == "groq":
        return bool(os.environ.get("GROQ_API_KEY")), "groq key" + ("" if os.environ.get("GROQ_API_KEY") else " missing")
    if provider == "claude":
        return bool(os.environ.get("ANTHROPIC_API_KEY")), "claude key" + ("" if os.environ.get("ANTHROPIC_API_KEY") else " missing")
    return False, f"unknown provider {provider}"
