"""構成4 オーケストレータノード。

ログを薄く読み、どの監視エージェントを呼ぶべきかを決める。
重い分析は監視エージェント側に任せ、ここはトリアージに徹する。
"""
from __future__ import annotations

import os
import time

import anthropic

from log_analyzer.rally._helpers import extract_json
from log_analyzer.rally.state import Config4State

ORCHESTRATOR_PROMPT = """\
あなたはネットワーク／システムインフラのトリアージ担当者です。
与えられたログを読み、以下の監視エージェントのうちどれを呼ぶべきかを判断してください。

監視エージェント:
- fw: ファイアウォール関連（policy / DENY / ACL の異常）
- routing: ルーティング・接続性関連（タイムアウト / 経路 / TCP 再送 / 帯域）
- app: アプリケーション層（5xx / プロセスエラー / OOM / 502 bad gateway）

判断ルール:
- 該当が複数あれば複数選ぶ（例: ["fw", "routing"]）
- 判断不能な場合は 3 つすべてを選ぶ（["fw", "routing", "app"]）
- 該当が無い場合でも最低 1 つは選ぶ（"fw" を既定）

出力 (JSON のみ):
{
  "invoke": ["fw", "routing"],
  "rationale": "FW で DENY 多発、その下流で upstream timeout が出ているため両方起動。"
}
"""


def orchestrator_node(state: Config4State) -> dict:
    p_overrides = state.get("prompt_overrides", {}) or {}
    m_overrides = state.get("model_overrides", {}) or {}
    model = m_overrides.get("orchestrator") or os.environ.get("BASELINE_MODEL", "claude-sonnet-4-5")
    system_prompt = p_overrides.get("orchestrator", ORCHESTRATOR_PROMPT)
    client = anthropic.Anthropic()
    started = time.perf_counter()
    response = client.messages.create(
        model=model,
        max_tokens=400,
        system=system_prompt,
        messages=[{"role": "user", "content": state["log_text"]}],
    )
    latency_ms = int((time.perf_counter() - started) * 1000)
    raw = response.content[0].text
    decision = extract_json(raw)

    valid_monitors = {"fw", "routing", "app"}
    invoke = [m for m in decision.get("invoke", []) if m in valid_monitors]
    if not invoke:
        invoke = ["fw"]

    return {
        "orchestrator_decision": {
            "invoke": invoke,
            "rationale": decision.get("rationale", ""),
        },
        "monitor_results": {},
        "escalations": [],
        "rally_round": 1,
        "rally_targets_pending": [],
        "token_log": [
            {
                "role": "orchestrator",
                "model": model,
                "tokens_in": response.usage.input_tokens,
                "tokens_out": response.usage.output_tokens,
                "latency_ms": latency_ms,
                "input": state["log_text"][:2000],
                "raw_output": raw,
            }
        ],
    }
