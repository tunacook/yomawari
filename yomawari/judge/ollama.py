"""Ollama の /api/chat に、JSON スキーマ付きで問い合わせる。"""

from __future__ import annotations

import json
import threading
import time
from typing import Any

import httpx


class OllamaError(Exception):
    pass


_busy_lock = threading.Lock()
_busy = 0
_last_end = 0.0


def busy_since(t: float) -> bool:
    """時刻 t（time.monotonic）以降に Ollama に問い合わせていたか。Ollama は GPU を大きく使うので、その間は
    ゲームのパフォーマンスの判定から外す（DESIGN.md §10「パフォーマンス」）。"""
    with _busy_lock:
        return _busy > 0 or _last_end >= t


def chat_json(endpoint: str, model: str, prompt: str, schema: dict[str, Any], timeout: float) -> dict[str, Any]:
    """prompt を送り、schema に沿った JSON の答えを返す。つながらない・答えが JSON でなければ OllamaError。"""
    global _busy, _last_end
    with _busy_lock:
        _busy += 1
    try:
        return _chat_json(endpoint, model, prompt, schema, timeout)
    finally:
        with _busy_lock:
            _busy -= 1
            _last_end = time.monotonic()


def _chat_json(endpoint: str, model: str, prompt: str, schema: dict[str, Any], timeout: float) -> dict[str, Any]:
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
