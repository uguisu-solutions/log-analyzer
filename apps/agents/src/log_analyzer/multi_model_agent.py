"""構成3 — 3 モデル並列分析 + 統合エージェント。

パイプライン:
    log text
      ─┬─> Claude Opus 4.7 ────┐
       ├─> Claude Opus 4.7 ────┼─> 統合エージェント (Opus 4.7) ─> AnalysisResult
       └─> OpenAI GPT-5.5 ──────┘

並列実行は `asyncio.gather` で実装（AWS Step Functions Parallel の代替、2026-05-07 決定）。
3rd モデルの GPT-5.5 は Anthropic 系列とは独立したベンダーなので、
Q3「マルチモデルは単一より優れるか」をベンダー横断で評価できる。
（モデル統一方針 2026-06: Claude=Opus 4.7 / OpenAI=GPT-5.5）
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass

import anthropic
import openai

from log_analyzer.baseline_agent import SYSTEM_PROMPT
from log_analyzer.schema import (
    AnalysisResult,
    ConfigId,
    GraphEdge,
    GraphNode,
    Metrics,
    RecommendedAction,
    RootCauseCandidate,
)
from log_analyzer.tracing import flush, get_client, usage_for

INTEGRATION_PROMPT = """\
あなたは複数の LLM の分析結果を統合する上級アナリストです。
与えられた 3 つのモデルの分析結果（共通スキーマ JSON）を比較し、最終的な結論を
共通スキーマで返してください。

統合ルール:
- 多数決（2 つ以上のモデルが同じ category / summary 系統に集まる）で支持された候補を配列先頭に
- 1 モデルだけが言っている候補は後方に置くか、根拠が薄ければ除外
- 候補同士は並列扱い (UI 上もランキングではなくフラット表示) なので、順序強調表現は使わない
- 一致度が高い場合は confidence を高く（0.9 以上）、不一致が多い場合は confidence を 0.7 以下に
- recommended_actions は重複を統合し、`human_judgment_required: true` のものを優先
  （「外せないフラグ」を 1 モデルでも立てたら統合後も維持。議事録 L3）

出力は JSON のみとし、構成1 と同じ形式に厳密に従うこと。
- フィールド名・enum 値は英語表記
- summary / action の自然文は日本語
- コードフェンスで囲まない
"""


@dataclass
class _ModelRunResult:
    role: str
    model: str
    parsed: dict
    tokens_in: int
    tokens_out: int
    latency_ms: int


async def _analyze_with_anthropic(
    log_text: str, model: str, role: str, system_prompt: str
) -> _ModelRunResult:
    client = anthropic.AsyncAnthropic()
    started = time.perf_counter()
    response = await client.messages.create(
        model=model,
        max_tokens=2000,
        system=system_prompt,
        messages=[{"role": "user", "content": log_text}],
    )
    latency_ms = int((time.perf_counter() - started) * 1000)
    raw_text = response.content[0].text
    parsed = _extract_json(raw_text)
    return _ModelRunResult(
        role=role,
        model=model,
        parsed=parsed,
        tokens_in=response.usage.input_tokens,
        tokens_out=response.usage.output_tokens,
        latency_ms=latency_ms,
    )


async def _analyze_with_openai(
    log_text: str, model: str, role: str, system_prompt: str
) -> _ModelRunResult:
    client = openai.AsyncOpenAI()
    started = time.perf_counter()
    # GPT-5.x は Responses API + reasoning.effort / text.verbosity が推奨 (OpenAI 公式ガイダンス)。
    response = await client.responses.create(
        model=model,
        instructions=system_prompt,
        input=log_text,
        max_output_tokens=4000,
        reasoning={"effort": "low"},
        text={"verbosity": "low"},
    )
    latency_ms = int((time.perf_counter() - started) * 1000)
    raw_text = getattr(response, "output_text", None) or ""
    parsed = _extract_json(raw_text)
    usage = getattr(response, "usage", None)
    return _ModelRunResult(
        role=role,
        model=model,
        parsed=parsed,
        tokens_in=int(getattr(usage, "input_tokens", 0) or 0) if usage else 0,
        tokens_out=int(getattr(usage, "output_tokens", 0) or 0) if usage else 0,
        latency_ms=latency_ms,
    )


async def _integrate(
    parallel_results: list[_ModelRunResult],
    integrate_prompt: str,
    integrate_model: str,
) -> _ModelRunResult:
    sonnet_model = integrate_model
    payload = {f"{r.model}-{r.role}": r.parsed for r in parallel_results}
    user_input = (
        "## 各モデルの分析結果\n"
        + json.dumps(payload, ensure_ascii=False, indent=2)
    )
    return await _analyze_with_anthropic_raw(
        system=integrate_prompt,
        user_content=user_input,
        model=sonnet_model,
        role="integrate",
    )


async def _analyze_with_anthropic_raw(
    system: str, user_content: str, model: str, role: str
) -> _ModelRunResult:
    client = anthropic.AsyncAnthropic()
    started = time.perf_counter()
    response = await client.messages.create(
        model=model,
        max_tokens=2000,
        system=system,
        messages=[{"role": "user", "content": user_content}],
    )
    latency_ms = int((time.perf_counter() - started) * 1000)
    raw_text = response.content[0].text
    parsed = _extract_json(raw_text)
    return _ModelRunResult(
        role=role,
        model=model,
        parsed=parsed,
        tokens_in=response.usage.input_tokens,
        tokens_out=response.usage.output_tokens,
        latency_ms=latency_ms,
    )


def run_multi_model(
    log_text: str,
    log_ref: str = "inline",
    prompt_overrides: dict[str, str] | None = None,
    model_overrides: dict[str, str] | None = None,
) -> AnalysisResult:
    return asyncio.run(
        _run_multi_model_async(log_text, log_ref, prompt_overrides or {}, model_overrides or {})
    )


async def _run_multi_model_async(
    log_text: str,
    log_ref: str,
    prompt_overrides: dict[str, str],
    model_overrides: dict[str, str],
) -> AnalysisResult:
    # 3 並列段は設計上 Claude ×2 / OpenAI ×1 で固定（モデル上書き不可）。
    # モデル統一方針 2026-06 で Claude 側は Opus 4.7、OpenAI 側は GPT-5.5。
    sonnet_model = os.environ.get("BASELINE_MODEL", "claude-opus-4-7")
    haiku_model = os.environ.get("FILTER_MODEL", "claude-opus-4-7")
    openai_model = os.environ.get("OPENAI_MODEL", "gpt-5.5")
    # 統合段はモデル上書き可
    integrate_model = model_overrides.get("integrate") or sonnet_model
    analyze_prompt = prompt_overrides.get("analyze", SYSTEM_PROMPT)
    integrate_prompt = prompt_overrides.get("integrate", INTEGRATION_PROMPT)
    langfuse = get_client()

    trace = langfuse.trace(
        name="config3-multi",
        input={"log_ref": log_ref, "log_size_bytes": len(log_text)},
        metadata={"config_id": ConfigId.CONFIG3.value, "schema_version": "v0.1"},
    )

    parallel_started = time.perf_counter()
    parallel_results: list[_ModelRunResult] = await asyncio.gather(
        _analyze_with_anthropic(log_text, sonnet_model, "sonnet", analyze_prompt),
        _analyze_with_anthropic(log_text, haiku_model, "haiku", analyze_prompt),
        _analyze_with_openai(log_text, openai_model, "openai", analyze_prompt),
    )
    parallel_wall_ms = int((time.perf_counter() - parallel_started) * 1000)

    for r in parallel_results:
        trace.generation(
            name=f"{r.model}-analyze-{r.role}",
            model=r.model,
            input=log_text[:2000],
            output=json.dumps(r.parsed, ensure_ascii=False),
            usage=usage_for(r.model, r.tokens_in, r.tokens_out),
        )

    integrated = await _integrate(parallel_results, integrate_prompt, integrate_model)
    trace.generation(
        name=f"{integrated.model}-{integrated.role}",
        model=integrated.model,
        input=json.dumps(
            {f"{r.model}-{r.role}": r.parsed for r in parallel_results},
            ensure_ascii=False,
        )[:2000],
        output=json.dumps(integrated.parsed, ensure_ascii=False),
        usage=usage_for(integrated.model, integrated.tokens_in, integrated.tokens_out),
    )

    total_tokens_in = sum(r.tokens_in for r in parallel_results) + integrated.tokens_in
    total_tokens_out = sum(r.tokens_out for r in parallel_results) + integrated.tokens_out
    # 並列段は wall-clock（最も遅いモデルに引きずられる）+ 統合段の逐次
    total_latency = parallel_wall_ms + integrated.latency_ms

    final = integrated.parsed
    info_loss = []
    # 各モデル個別の confidence を残す（後で「どのモデルの結論を採ったか」を追うため）
    for r in parallel_results:
        c = r.parsed.get("confidence", "?")
        info_loss.append(f"per-model {r.model}-{r.role} confidence={c}")

    graph_nodes: list[GraphNode] = []
    graph_edges: list[GraphEdge] = []
    for r in parallel_results:
        node_id = r.role
        graph_nodes.append(
            GraphNode(
                id=node_id,
                label=r.model,
                role="parallel_model",
                model=r.model,
                latency_ms=r.latency_ms,
                tokens_in=r.tokens_in,
                tokens_out=r.tokens_out,
                metadata={"per_model_confidence": r.parsed.get("confidence")},
            )
        )
        graph_edges.append(GraphEdge(source=node_id, target="integrate"))
    graph_nodes.append(
        GraphNode(
            id="integrate",
            label=integrated.model,
            role="integrator",
            model=integrated.model,
            latency_ms=integrated.latency_ms,
            tokens_in=integrated.tokens_in,
            tokens_out=integrated.tokens_out,
        )
    )

    result = AnalysisResult(
        trace_id=str(trace.id),
        config_id=ConfigId.CONFIG3,
        input_log_ref=log_ref,
        root_cause_candidates=[
            RootCauseCandidate(**c) for c in final.get("root_cause_candidates", [])
        ],
        recommended_actions=[
            RecommendedAction(**a) for a in final.get("recommended_actions", [])
        ],
        confidence=float(final.get("confidence", 0.0)),
        metrics=Metrics(
            tokens_in=total_tokens_in,
            tokens_out=total_tokens_out,
            latency_ms_total=total_latency,
            latency_ms_p50=total_latency,
        ),
        info_loss_flags=info_loss,
        execution_graph_nodes=graph_nodes,
        execution_graph_edges=graph_edges,
    )

    trace.update(output=result.model_dump(mode="json"))
    flush()
    return result


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
