"""構成2 — ルールフィルタ + Haiku 圧縮 + Sonnet 解析。

パイプライン:
    log text
      -> filters.filter_log（異常行抽出 + 正常パターン件数集計）
      -> Haiku 4.5（triage カード生成）
      -> Sonnet 4.5（triage カード + 異常行を共通スキーマに変換）

構成1 と同一の `AnalysisResult` を返すため、比較画面・評価突合は機械的に行える。
metrics.compression_ratio には「Sonnet に渡した入力 / 元ログ」の比率を記録する
（議事録 L2 の「情報をどれだけ残せたか」を後追いで定量評価できるようにするため）。
"""
from __future__ import annotations

import json
import os
import time

import anthropic

from log_analyzer.baseline_agent import SYSTEM_PROMPT
from log_analyzer.filters import FilterResult, filter_log
from log_analyzer.schema import (
    AnalysisResult,
    ConfigId,
    GraphEdge,
    GraphNode,
    Metrics,
    RecommendedAction,
    RootCauseCandidate,
)
from log_analyzer.tracing import flush, get_client

HAIKU_TRIAGE_PROMPT = """\
あなたはインフラログ解析の前処理エンジニアです。
与えられた異常行（ERROR/WARN）と正常パターンの件数を読み、
原因究明に必要な情報だけを残した triage カードを日本語で出力してください。

triage カードのフォーマット:
- 観測期間: <最古〜最新タイムスタンプ>
- 主要な異常イベント: <箇条書き 3〜5 件、件数も含めて>
- 関連サブネット／ホスト: <観測された IP / サブネット>
- 直前の構成変更: <ポリシー適用・設定変更があれば抜粋>
- 推測される影響範囲: <例: 特定セグメントの 80 番ポート通信不可>

ルール:
- triage カードは 12 行以内、各行 200 文字以内に収めること。
- 具体的なログ行の引用は最小限に抑え、要点だけを構造化すること。
- 後段の Sonnet が原因特定するための材料を残すこと（憶測の結論は書かない）。
"""


def run_filtered(
    log_text: str,
    log_ref: str = "inline",
    prompt_overrides: dict[str, str] | None = None,
    model_overrides: dict[str, str] | None = None,
) -> AnalysisResult:
    p_overrides = prompt_overrides or {}
    m_overrides = model_overrides or {}
    haiku_model = m_overrides.get("triage") or os.environ.get("FILTER_MODEL", "claude-haiku-4-5")
    sonnet_model = m_overrides.get("analyze") or os.environ.get("BASELINE_MODEL", "claude-sonnet-4-5")
    triage_prompt = p_overrides.get("triage", HAIKU_TRIAGE_PROMPT)
    analyze_prompt = p_overrides.get("analyze", SYSTEM_PROMPT)
    langfuse = get_client()

    trace = langfuse.trace(
        name="config2-filtered",
        input={"log_ref": log_ref, "log_size_bytes": len(log_text)},
        metadata={"config_id": ConfigId.CONFIG2.value, "schema_version": "v0.1"},
    )

    fr = filter_log(log_text)

    client = anthropic.Anthropic()

    haiku_input = _format_haiku_input(fr)
    haiku_started = time.perf_counter()
    haiku_response = client.messages.create(
        model=haiku_model,
        max_tokens=800,
        system=triage_prompt,
        messages=[{"role": "user", "content": haiku_input}],
    )
    haiku_latency = int((time.perf_counter() - haiku_started) * 1000)
    triage_card = haiku_response.content[0].text.strip()

    trace.generation(
        name=f"{haiku_model}-triage",
        model=haiku_model,
        input=haiku_input[:2000],
        output=triage_card,
        usage_details={
            "input": haiku_response.usage.input_tokens,
            "output": haiku_response.usage.output_tokens,
        },
    )

    sonnet_input = _format_sonnet_input(triage_card, fr)
    sonnet_started = time.perf_counter()
    sonnet_response = client.messages.create(
        model=sonnet_model,
        max_tokens=2000,
        system=analyze_prompt,
        messages=[{"role": "user", "content": sonnet_input}],
    )
    sonnet_latency = int((time.perf_counter() - sonnet_started) * 1000)
    raw_text = sonnet_response.content[0].text
    parsed = _extract_json(raw_text)

    sent_bytes = len(sonnet_input.encode("utf-8"))
    compression_ratio = sent_bytes / max(fr.original_bytes, 1)
    info_loss: list[str] = []
    if fr.other_info_count > 0:
        info_loss.append(
            f"未分類の INFO 行 {fr.other_info_count} 件をフィルタ段で破棄した"
        )

    total_tokens_in = haiku_response.usage.input_tokens + sonnet_response.usage.input_tokens
    total_tokens_out = haiku_response.usage.output_tokens + sonnet_response.usage.output_tokens
    total_latency = haiku_latency + sonnet_latency

    result = AnalysisResult(
        trace_id=str(trace.id),
        config_id=ConfigId.CONFIG2,
        input_log_ref=log_ref,
        root_cause_candidates=[
            RootCauseCandidate(**c) for c in parsed.get("root_cause_candidates", [])
        ],
        recommended_actions=[
            RecommendedAction(**a) for a in parsed.get("recommended_actions", [])
        ],
        confidence=float(parsed.get("confidence", 0.0)),
        metrics=Metrics(
            tokens_in=total_tokens_in,
            tokens_out=total_tokens_out,
            latency_ms_total=total_latency,
            latency_ms_p50=total_latency,
            compression_ratio=compression_ratio,
        ),
        info_loss_flags=info_loss,
        execution_graph_nodes=[
            GraphNode(
                id="filter",
                label="rule-filter",
                role="filter",
                metadata={
                    "anomaly_lines": len(fr.anomaly_lines),
                    "normal_counts": fr.normal_counts,
                    "other_info_count": fr.other_info_count,
                    "compression_ratio_at_filter_stage": round(fr.compression_ratio, 3),
                },
            ),
            GraphNode(
                id="triage",
                label=haiku_model,
                role="triage",
                model=haiku_model,
                latency_ms=haiku_latency,
                tokens_in=haiku_response.usage.input_tokens,
                tokens_out=haiku_response.usage.output_tokens,
            ),
            GraphNode(
                id="analyze",
                label=sonnet_model,
                role="analyze",
                model=sonnet_model,
                latency_ms=sonnet_latency,
                tokens_in=sonnet_response.usage.input_tokens,
                tokens_out=sonnet_response.usage.output_tokens,
            ),
        ],
        execution_graph_edges=[
            GraphEdge(source="filter", target="triage"),
            GraphEdge(source="triage", target="analyze"),
        ],
    )

    trace.generation(
        name=f"{sonnet_model}-analyze",
        model=sonnet_model,
        input=sonnet_input[:2000],
        output=raw_text,
        usage_details={
            "input": sonnet_response.usage.input_tokens,
            "output": sonnet_response.usage.output_tokens,
        },
    )
    trace.update(output=result.model_dump(mode="json"))
    flush()
    return result


def _format_haiku_input(fr: FilterResult) -> str:
    counts_lines = "\n".join(f"- {k}: {v} 件" for k, v in fr.normal_counts.items())
    if not counts_lines:
        counts_lines = "- （該当なし）"
    anomaly_text = "\n".join(fr.anomaly_lines) if fr.anomaly_lines else "（異常行なし）"
    return (
        f"## 異常行（ERROR/WARN/FATAL/CRITICAL）\n{anomaly_text}\n\n"
        f"## 正常パターンの集計\n{counts_lines}\n"
        f"- 未分類 INFO 行: {fr.other_info_count} 件\n"
        f"- 元ログ総行数: {fr.original_lines} 行\n"
    )


def _format_sonnet_input(triage_card: str, fr: FilterResult) -> str:
    anomaly_text = "\n".join(fr.anomaly_lines) if fr.anomaly_lines else "（異常行なし）"
    return (
        f"## Triage カード（前段 Haiku が要約）\n{triage_card}\n\n"
        f"## 異常行の生ログ抜粋\n{anomaly_text}\n"
    )


def _extract_json(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        parts = text.split("```")
        if len(parts) >= 2:
            inner = parts[1]
            if inner.startswith("json"):
                inner = inner[4:]
            text = inner
    return json.loads(text.strip())
