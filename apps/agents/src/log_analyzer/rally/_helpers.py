"""構成4 内部ユーティリティ。"""
from __future__ import annotations

import json


def extract_json(text: str) -> dict:
    """LLM 応答からコードフェンスを剥がし JSON を取り出す。"""
    text = text.strip()
    if text.startswith("```"):
        parts = text.split("```")
        if len(parts) >= 2:
            inner = parts[1]
            if inner.startswith("json"):
                inner = inner[4:]
            text = inner
    return json.loads(text.strip())


def safe_extract_json(text: str, fallback: dict) -> tuple[dict, str | None]:
    """``extract_json`` の例外安全版。失敗時は ``fallback`` と error 文字列を返す。

    LLM 応答が max_tokens で切断されて JSON が不完全になった場合などに
    呼び出し側が 500 で落ちずに劣化結果を返せるようにするためのヘルパ。
    """
    try:
        return extract_json(text), None
    except (json.JSONDecodeError, ValueError, IndexError) as e:
        return dict(fallback), f"{type(e).__name__}: {e}"
