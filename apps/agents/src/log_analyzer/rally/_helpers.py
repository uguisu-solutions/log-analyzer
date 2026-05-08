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
