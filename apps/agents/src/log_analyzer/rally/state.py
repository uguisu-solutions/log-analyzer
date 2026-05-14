"""構成4 (委譲型ラリー) 用の State スキーマ。

旧版は LangGraph TypedDict + reducer による fan-out 型だったが、
新版は「シングルアクティブな委譲チェーン」に置き換えた:

    orchestrator (初回 1 回のみ、最初の監視を選ぶ)
        → monitor_A (分析し、次ノードを 1 つ指名)
        → monitor_B (同様)
        → ...
        → integrator (どこかの監視が指名)

監視 → 監視 の連続遷移制約:
    - 自己遷移禁止 (A → A)
    - 直前と同じノード禁止 (A → B → A の即時 ping-pong)
    違反時は integrator に自動フォールバック。

ラウンド数 ≥ ``rally_confirmation_threshold`` の時点で、UI 側に
``await_confirmation`` イベントを送って一時停止する。ユーザー応答で
``rally_max_rounds`` を延長して再開するか、整理して integrator に移る。
"""
from __future__ import annotations

from typing import Any, TypedDict


class Config4State(TypedDict, total=False):
    log_text: str
    log_ref: str

    # 委譲制御
    current_node: str
    # "orchestrator" / "fw" / "routing" / "app" / "dns" / "sec" / "integrator"
    previous_node: str | None
    pending_focus_hint: str  # current_node に渡す観点指示（前ノードからの引き継ぎ）

    # 各監視の最新結果（同じ監視が再呼出された場合は最新で上書き）
    monitor_results: dict[str, Any]

    # 委譲履歴（毎ステップ 1 件追記）
    delegation_history: list[dict[str, Any]]

    # ラリー制御
    rally_round: int
    rally_max_rounds: int
    rally_confirmation_threshold: int  # このラウンドに達したら UI 確認

    # 統合エージェントの出力
    integrator_result: dict[str, Any]

    # Langfuse 反映用に各 LLM 呼び出しの足跡を蓄積
    token_log: list[dict[str, Any]]

    # ユーザー定義構成からのプロンプト/モデル上書き
    prompt_overrides: dict[str, str]
    model_overrides: dict[str, str]
