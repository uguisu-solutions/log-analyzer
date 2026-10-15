"""実行のモデル構成と入力の指紋（docs/plan/slm_support.md §9-3 / §9-6）。

クラウド LLM と SLM の最終出力の精度差を観測するため、履歴の各実行について
「実際にどのモデルで動いたか」と「モデル以外の条件が同じ実行か」を機械的に判別する。
"""
from __future__ import annotations

import hashlib
import json

from log_analyzer.llm_client import OLLAMA_PREFIX

_MONITOR_ROLES = {"fw", "routing", "app", "dns", "sec"}


def models_used(result: dict) -> dict[str, str]:
    """結果に残った実績値から ``{ノード ID: モデル}`` を返す。

    リクエストの指定ではなく実行グラフの値を使う（環境変数のフォールバックで
    別のモデルになっていた、という取り違えを防ぐため）。
    """
    from_graph = {
        str(n["id"]): str(n["model"])
        for n in result.get("execution_graph_nodes") or []
        if n.get("model")
    }
    if from_graph:
        return from_graph
    # 2 段階解析の結果は実行グラフが空で、両 Stage の round_metrics が連結されて残る
    return {
        _slot_of(str(m["role"])): str(m["model"])
        for m in result.get("round_metrics") or []
        if m.get("model")
    }


def _slot_of(role: str) -> str:
    """round_metrics の役割名を、実行グラフ・UI と同じ単位（slot ID）にする。"""
    return f"{role}_monitor" if role in _MONITOR_ROLES else role


def profile_of(models: dict[str, str]) -> str:
    """実績のモデル構成からプロファイル名を決める（比較ビューの列になる）。

    - すべてクラウド → ``"cloud"``
    - すべて同じ SLM → ``"local:<Ollama のタグ>"``
    - クラウドと SLM、または複数の SLM が混在 → ``"mixed"``（内訳は models_json で見る）
    - 実績なし → ``""``
    """
    if not models:
        return ""
    local = {m for m in models.values() if m.startswith(OLLAMA_PREFIX)}
    if not local:
        return "cloud"
    if len(local) == 1 and len(local) == len(set(models.values())):
        return "local:" + next(iter(local))[len(OLLAMA_PREFIX):]
    return "mixed"


# 解析の条件ではない（画面の表示切替だけの）項目。指紋から除く
_NON_INPUT_KEYS = {"view_mode"}


def input_fingerprint(request: dict) -> str:
    """解析履歴の保存リクエストから、モデル以外の条件の指紋を作る。

    保存リクエストにはモデル指定が含まれないため、同じ指紋の実行どうしは
    「モデル以外の条件（入力ファイル・問診票・トポロジ・ラウンド上限など）が同じ」になる。
    注意: 入力ファイルは名前で識別する（保存リクエストに本文が無いため）。
    同名で中身の違うファイルは区別できない。
    """
    payload = {k: v for k, v in request.items() if k not in _NON_INPUT_KEYS}
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
