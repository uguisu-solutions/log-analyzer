"""構成4 stategraph 用の State スキーマ（LangGraph TypedDict）。

各フィールドの reducer:
- ``monitor_results``: 監視名（fw / routing / app）をキーとする dict マージ
  （同じ監視が再呼出された場合は最新で上書き）
- ``orchestrator_history``: list の concat（毎ラウンドの判断を蓄積）
- ``token_log``: list の concat（全 LLM 呼び出しのトレース材料を蓄積）
- それ以外: 上書き（後勝ち）

設計メモ:
- 旧 ``escalations`` / ``rally_targets_pending`` は廃止。判断は
  すべて ``orchestrator_node`` に集約され、監視は escalate_to を返さない。
- ``focus_hints`` はオーケストレータが次ラウンドの監視に渡す自然文の観点指示。
  ``{slot_id: "観点"}`` 形式で、上書き reducer。
"""
from __future__ import annotations

from operator import add
from typing import Annotated, Any, TypedDict


def _merge_dicts(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    return {**left, **right}


class Config4State(TypedDict, total=False):
    log_text: str
    log_ref: str

    # オーケストレータの最新判断（action / invoke / focus_hints / rationale / round / forced）
    orchestrator_decision: dict[str, Any]

    # 過去のオーケストレータ判断履歴（再入時にコンテキストとして渡す）
    orchestrator_history: Annotated[list[dict[str, Any]], add]

    # 各監視エージェントの最新結果（同じ監視が再呼出された場合は最新で上書き）
    monitor_results: Annotated[dict[str, Any], _merge_dicts]

    # 次ラウンドで監視に注入する観点指示（slot_id → 自然文ヒント、上書き）
    focus_hints: dict[str, str]

    # ラリー制御
    rally_round: int  # オーケストレータが下したラウンド番号（初回呼出後 1 になる）
    rally_max_rounds: int  # 既定 3。env RALLY_MAX_ROUNDS で上書き可
    # 0 より大きいとき、min_rounds 未達で finalize を選ぶ LLM 出力を invoke に override する
    # PoC のデモ目的（再入を確実に観測したい）。本番では 0 のままが正しい挙動
    rally_force_min_rounds: int

    # 統合エージェントの出力
    integrator_result: dict[str, Any]

    # Langfuse 反映用に各 LLM 呼び出しの足跡を蓄積
    token_log: Annotated[list[dict[str, Any]], add]

    # ユーザー定義構成からのプロンプト/モデル上書き（slot_id → 上書き値）
    # 上書きが無い slot はデフォルトを使う
    prompt_overrides: dict[str, str]
    model_overrides: dict[str, str]
