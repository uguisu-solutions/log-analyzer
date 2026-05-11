"""構成4 統合ノード。

各監視エージェントの findings を共通スキーマ ``AnalysisResult`` 形に変換する
最終段。`human_judgment_required: true` を一度立てたら下げない（議事録 L3）。
"""
from __future__ import annotations

import json
import os
import time

import anthropic

from log_analyzer.rally._helpers import safe_extract_json
from log_analyzer.rally.state import Config4State

INTEGRATOR_PROMPT = """\
あなたは構成4 ラリー型システムの最終統合エージェントです。
オーケストレータの判断と、各監視エージェント (FW / Routing / App) の findings を
受け取り、共通スキーマ AnalysisResult の中身（root_cause_candidates, recommended_actions, confidence）
を構築してください。

統合ルール:
- 複数監視で支持された原因を rank 1 に。1 監視のみが言うものは rank を下げる
- recommended_actions: ロールバック・再起動・設定変更・データ削除を伴うアクションは
  必ず `human_judgment_required: true`（議事録 L3、外せないフラグ）。
  各監視が立てた true は統合後も維持し、false に上書きしない
- confidence: 監視間で結論が一致する度合いに応じて算出
  - 全監視一致 + トポロジ裏付けあり: 0.9 以上
  - 一部一致: 0.7 〜 0.85
  - 不一致が多い: 0.5 〜 0.7
- evidence: 元ログ行を引用。トポロジ参照を根拠にする場合はその旨を併記

出力 (JSON のみ):
{
  "root_cause_candidates": [
    {"rank": 1, "category": "FW|Net|App|DNS|Sec|Unknown", "summary": "...", "evidence": ["..."]}
  ],
  "recommended_actions": [
    {"action": "...", "human_judgment_required": true, "risk_level": "low|mid|high"}
  ],
  "confidence": 0.0
}

ルール:
- 候補は最大 3 件
- フィールド名・enum 値は英語、summary / action の自然文は日本語
- コードフェンスで囲まない
"""


def integrator_node(state: Config4State) -> dict:
    p_overrides = state.get("prompt_overrides", {}) or {}
    m_overrides = state.get("model_overrides", {}) or {}
    model = m_overrides.get("integrator") or os.environ.get("BASELINE_MODEL", "claude-sonnet-4-5")
    system_prompt = p_overrides.get("integrator", INTEGRATOR_PROMPT)
    payload = {
        "log_text": state["log_text"],
        "orchestrator_decision": state.get("orchestrator_decision", {}),
        "monitor_results": state.get("monitor_results", {}),
        "rally_round_completed": state.get("rally_round", 1),
    }
    user_input = json.dumps(payload, ensure_ascii=False, indent=2)

    client = anthropic.Anthropic()
    started = time.perf_counter()
    # 複数ラウンドのラリーで monitor_results が肥大すると応答も長くなりやすい。
    # 切断による JSON parse 失敗を避けるため余裕を持たせる
    response = client.messages.create(
        model=model,
        max_tokens=4000,
        system=system_prompt,
        messages=[{"role": "user", "content": user_input}],
    )
    latency_ms = int((time.perf_counter() - started) * 1000)
    raw = response.content[0].text
    parsed, parse_error = safe_extract_json(
        raw,
        fallback={
            "root_cause_candidates": [],
            "recommended_actions": [],
            "confidence": 0.0,
        },
    )
    if parse_error:
        # 後段が info_loss_flags に転記できるようマークしておく
        parsed["_parse_error"] = parse_error
        parsed["_raw_truncated"] = raw[-500:]

    return {
        "integrator_result": parsed,
        "token_log": [
            {
                "role": "integrator",
                "model": model,
                "tokens_in": response.usage.input_tokens,
                "tokens_out": response.usage.output_tokens,
                "latency_ms": latency_ms,
                "input": user_input[:2000],
                "raw_output": raw,
            }
        ],
    }
