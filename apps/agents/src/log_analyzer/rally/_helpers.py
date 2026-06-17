"""構成4 内部ユーティリティ。"""
from __future__ import annotations

import json
import re

_FENCE_RE = re.compile(r"```(?:json)?\s*\n?(.*?)```", re.DOTALL | re.IGNORECASE)


def _fenced_block(text: str) -> str | None:
    """テキスト中の最初のコードフェンス ```` ```json ... ``` ```` の中身を返す。

    先頭に限らず、前置きの散文の後ろにフェンスが来ても拾う。
    """
    m = _FENCE_RE.search(text)
    return m.group(1) if m else None


def _first_json_object(text: str) -> str | None:
    """テキスト中の最初の波括弧対応した ``{...}`` を返す (文字列リテラルを考慮)。

    前後に散文が付いていても JSON オブジェクト部分だけを切り出すための波括弧
    マッチャ。文字列内の ``{`` / ``}`` やエスケープは数えない。
    """
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        c = text[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        else:
            if c == '"':
                in_str = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    return text[start:i + 1]
    return None


def _json_candidates(text: str):
    """JSON 抽出の候補文字列を優先順に yield する。"""
    yield text  # 1) 純粋 JSON (従来の素直なケース)
    fenced = _fenced_block(text)
    if fenced is not None:
        yield fenced.strip()  # 2) ```json ... ``` の中身 (前置き散文があっても拾う)
    obj = _first_json_object(text)
    if obj is not None:
        yield obj  # 3) 散文に埋もれた {...} を波括弧対応で抽出


def extract_json(text: str) -> dict:
    """LLM 応答から JSON オブジェクトを取り出す (前置き散文 / コードフェンス対応)。

    BQ ツール利用後などモデルが JSON の前に分析文を付けたり ```json フェンスで
    囲んだりするケースに耐えるため、(1) 純粋 JSON → (2) フェンス内 → (3) 波括弧
    対応で散文中の最初のオブジェクト、の順に試す。いずれも dict を要求する。
    """
    text = text.strip()
    for candidate in _json_candidates(text):
        try:
            obj = json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(obj, dict):
            return obj
    raise ValueError("応答から JSON オブジェクトを抽出できませんでした")


def safe_extract_json(text: str, fallback: dict) -> tuple[dict, str | None]:
    """``extract_json`` の例外安全版。失敗時は ``fallback`` と error 文字列を返す。

    LLM 応答が max_tokens で切断されて JSON が不完全になった場合などに
    呼び出し側が 500 で落ちずに劣化結果を返せるようにするためのヘルパ。
    """
    try:
        return extract_json(text), None
    except (json.JSONDecodeError, ValueError, IndexError) as e:
        return dict(fallback), f"{type(e).__name__}: {e}"
