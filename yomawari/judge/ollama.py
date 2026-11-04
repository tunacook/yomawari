"""Ollama の /api/chat に、JSON スキーマ付きで問い合わせる。"""

from __future__ import annotations

import json
from typing import Any

import httpx


class OllamaError(Exception):
    pass


def chat_json(endpoint: str, model: str, prompt: str, schema: dict[str, Any], timeout: float) -> dict[str, Any]:
    """prompt を送り、schema に沿った JSON の答えを返す。つながらない・答えが JSON でなければ OllamaError。"""
    try:
        r = httpx.post(
            f"{endpoint.rstrip('/')}/api/chat",
            json={
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "format": schema,
                "stream": False,
                "options": {"temperature": 0},
            },
            timeout=timeout,
        )
        r.raise_for_status()
        content = r.json()["message"]["content"]
        out = json.loads(content)
    except (httpx.HTTPError, KeyError, ValueError) as e:
        raise OllamaError(f"{type(e).__name__}: {e}") from e
    if not isinstance(out, dict):
        raise OllamaError(f"JSON のオブジェクトではありません: {content[:100]}")
    return out
