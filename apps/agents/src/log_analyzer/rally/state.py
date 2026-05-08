"""構成4 stategraph 用の State スキーマ（LangGraph TypedDict）。

各フィールドの reducer:
- `monitor_results`: 監視名（fw / routing / app）をキーとする dict マージ
- `escalations`: list の concat（複数監視が並列に書き込む）
- `token_log`: list の concat（全 LLM 呼び出しのトレース材料を蓄積）
- それ以外: 上書き（後勝ち）
"""
from __future__ import annotations

from operator import add
from typing import Annotated, Any, TypedDict


def _merge_dicts(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    return {**left, **right}


class Config4State(TypedDict, total=False):
    log_text: str
    log_ref: str

    # オーケストレータの判断結果
    orchestrator_decision: dict[str, Any]

    # 各監視エージェントの結果（キー: "fw" / "routing" / "app"）
    monitor_results: Annotated[dict[str, Any], _merge_dicts]

    # 監視からの「他レイヤを呼べ」のシグナル（複数監視が並列に書き込む）
    escalations: Annotated[list[str], add]

    # ラリー制御
    rally_round: int
    rally_targets_pending: list[str]

    # 統合エージェントの出力
    integrator_result: dict[str, Any]

    # Langfuse 反映用に各 LLM 呼び出しの足跡を蓄積
    token_log: Annotated[list[dict[str, Any]], add]

    # ユーザー定義構成からのプロンプト/モデル上書き（slot_id → 上書き値）
    # 上書きが無い slot はデフォルトを使う
    prompt_overrides: dict[str, str]
    model_overrides: dict[str, str]
