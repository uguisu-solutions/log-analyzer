"""Configuration 1 — simple-LLM baseline.

Pipeline: log text -> Claude Sonnet -> common-schema JSON.

Phase 1 uses this to prove the trace path (Anthropic call -> Langfuse trace ->
Dify-compatible result). It is the comparison baseline for configs 2-4.
"""
from __future__ import annotations

import json
import os
import time

import anthropic

from log_analyzer.schema import (
    SCHEMA_VERSION,
    AnalysisResult,
    ConfigId,
    GraphEdge,
    GraphNode,
    Metrics,
    RecommendedAction,
    RootCauseCandidate,
)
from log_analyzer.tracing import flush, get_client, usage_for

SYSTEM_PROMPT = """\
あなたは経験豊富なネットワーク／システムエンジニアです。
提供されたインフラログを解析し、根本原因の分析結果を構造化された JSON で返してください。

出力は JSON のみとし、以下の形式に厳密に従うこと。
{
  "root_cause_candidates": [
    {"category": "FW|Net|App|DNS|Sec|Unknown", "summary": "...", "evidence": ["log line excerpt"]}
  ],
  "recommended_actions": [
    {"action": "...", "human_judgment_required": true, "risk_level": "low|mid|high"}
  ],
  "confidence": 0.0
}

ルール:
- 候補は最大 3 件まで。配列順は LLM の確信度順でよいが、UI 側では並列として扱われるので「rank 1 が最良」を強く想起させる表現は使わない。
- ロールバック・再起動・設定変更・データ削除を伴うアクションは、必ず `human_judgment_required: true` とすること。
- 各候補・各アクションには、根拠となるログ行（または抜粋）を `evidence` に必ず引用すること。
- JSON をコードフェンスで囲まないこと。
- フィールド名（`root_cause_candidates`, `category` 等）と enum 値（`FW`, `Net`, `low`, `mid`, `high` など）は上記の英語表記のまま使うこと。
- `summary` および `action` の自然文は日本語で記述すること。
"""


def run_baseline(
    log_text: str,
    log_ref: str = "inline",
    prompt_overrides: dict[str, str] | None = None,
    model_overrides: dict[str, str] | None = None,
) -> AnalysisResult:
    """Run config1 against a single log and return the common-schema result.

    Args:
        prompt_overrides: slot_id → 上書きプロンプトの辞書。config1 では `analyze` slot のみ。
        model_overrides: slot_id → 上書きモデル名の辞書。config1 では `analyze` slot のみ。
        未指定 / 該当 slot が無ければデフォルトを使う。
    """
    p_overrides = prompt_overrides or {}
    m_overrides = model_overrides or {}
    model = m_overrides.get("analyze") or os.environ.get("BASELINE_MODEL", "claude-opus-4-7")
    system_prompt = p_overrides.get("analyze", SYSTEM_PROMPT)
    langfuse = get_client()

    trace = langfuse.trace(
        name="config1-baseline",
        input={"log_ref": log_ref, "log_size_bytes": len(log_text)},
        metadata={"config_id": ConfigId.CONFIG1.value, "schema_version": SCHEMA_VERSION},
    )

    started = time.perf_counter()
    client = anthropic.Anthropic()
    response = client.messages.create(
        model=model,
        max_tokens=2000,
        system=system_prompt,
        messages=[{"role": "user", "content": log_text}],
    )
    latency_ms = int((time.perf_counter() - started) * 1000)

    raw_text = response.content[0].text
    parsed = _extract_json(raw_text)

    result = AnalysisResult(
        trace_id=str(trace.id),
        config_id=ConfigId.CONFIG1,
        input_log_ref=log_ref,
        root_cause_candidates=[
            RootCauseCandidate(**c) for c in parsed.get("root_cause_candidates", [])
        ],
        recommended_actions=[
            RecommendedAction(**a) for a in parsed.get("recommended_actions", [])
        ],
        confidence=float(parsed.get("confidence", 0.0)),
        metrics=Metrics(
            tokens_in=response.usage.input_tokens,
            tokens_out=response.usage.output_tokens,
            latency_ms_total=latency_ms,
            latency_ms_p50=latency_ms,
        ),
        execution_graph_nodes=[
            GraphNode(
                id="model",
                label=model,
                role="model_call",
                model=model,
                latency_ms=latency_ms,
                tokens_in=response.usage.input_tokens,
                tokens_out=response.usage.output_tokens,
                metadata={
                    "prompt_overridden": "analyze" in p_overrides,
                    "model_overridden": "analyze" in m_overrides,
                },
            )
        ],
        execution_graph_edges=[],
    )

    trace.generation(
        name=model,
        model=model,
        input=log_text[:2000],
        output=raw_text,
        usage=usage_for(model, response.usage.input_tokens, response.usage.output_tokens),
    )
    trace.update(output=result.model_dump(mode="json"))
    flush()
    return result


def _extract_json(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        # Strip ``` or ```json fences if the model adds them anyway.
        parts = text.split("```")
        if len(parts) >= 2:
            inner = parts[1]
            if inner.startswith("json"):
                inner = inner[4:]
            text = inner
    return json.loads(text.strip())
