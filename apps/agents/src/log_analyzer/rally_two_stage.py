"""Config-First 2 段階解析オーケストレータ (Phase A)。

議事録 (2026-05-26) で合意された「人の思考プロセスに近い」検証パターン:

    Stage 1: コンフィグ情報のみで原因の当たりをつける
       │
       ▼
    人間による必須承認 (advance / abort)
       │
       ▼
    Stage 2: ログで事実確認する

各 Stage の内部は既存 ``run_rally_stream`` をそのまま再利用する (プロンプトは変えない)。
Stage 1 と Stage 2 の差は **integrator に渡される log_text** だけ:

- Stage 1: ``_build_topology_log_text(topology, node_logs={}, node_configs=...)``
- Stage 2: Stage 1 仮説サマリ + ``_build_topology_log_text(topology, node_logs=..., node_configs=...)``

SSE 経由でブラウザに 2 段階分のイベントを順に流す。Stage 1 完了時に
``stage_one_complete`` を emit して decision_waiter で人間入力を待つ。
"""
from __future__ import annotations

from typing import Any, AsyncIterator, Callable, Awaitable

from log_analyzer.rally_agent import StreamEvent, run_rally_stream
from log_analyzer.schema import (
    AnalysisResult,
    ConfigId,
    DelegationEventDTO,
    StageOutput,
    SuspectedNodeFinding,
)

# Phase A の decision_waiter は 4 アクションを受ける:
#   {"action": "continue", "extend_by": int}     rally_max_rounds 延長 (既存)
#   {"action": "stop"}                            integrator 直行 (既存)
#   {"action": "advance"}                         Stage 1 → Stage 2 へ進む (新)
#   {"action": "abort"}                           Stage 1 で終了 (新)
TwoStageDecisionWaiter = Callable[[], Awaitable[dict[str, Any]]]


_STAGE_LABELS = {
    "config": "Stage 1: コンフィグ解析",
    "log": "Stage 2: ログ検証",
}


def _build_stage_one_hypothesis_block(stage_one: StageOutput) -> str:
    """Stage 1 の結果を Stage 2 の log_text 先頭に挿入する仮説ブロック。

    LLM (integrator / 監視) は自然にこのブロックを読み「仮説をログで検証する」モードになる。
    """
    if not stage_one.suspected_node_findings and not stage_one.summary:
        return ""
    lines: list[str] = []
    lines.append("## Stage 1 仮説 (コンフィグ解析より)")
    if stage_one.summary:
        lines.append(stage_one.summary)
    if stage_one.suspected_node_findings:
        lines.append("")
        lines.append("コンフィグ情報から推定された障害候補ノード:")
        for f in stage_one.suspected_node_findings:
            sev = f.severity or "?"
            lines.append(f"- {f.node_id} [{sev}]: {f.summary or '(詳細未記載)'}")
    lines.append("")
    lines.append("以下の実ログでこれらの仮説を **検証** してください。")
    lines.append("仮説が裏付けられれば確証を強め、矛盾があれば修正してください。")
    lines.append("")
    return "\n".join(lines) + "\n"


def _result_to_stage_output(stage: str, result: AnalysisResult) -> StageOutput:
    """``run_rally_stream`` の最終 result を StageOutput に変換。"""
    summary_parts: list[str] = []
    for c in result.root_cause_candidates[:2]:
        summary_parts.append(f"{c.category.value if hasattr(c.category, 'value') else c.category}: {c.summary}")
    summary = " / ".join(summary_parts) if summary_parts else ""
    return StageOutput(
        stage=stage,
        stage_label=_STAGE_LABELS.get(stage, stage),
        confidence=result.confidence,
        summary=summary,
        suspected_node_ids=list(result.suspected_node_ids),
        suspected_node_findings=list(result.suspected_node_findings),
        delegation_rounds=result.delegation_rounds,
        delegation_history=list(result.delegation_history),
        trace_id=result.trace_id,
        tokens_in=result.metrics.tokens_in,
        tokens_out=result.metrics.tokens_out,
        latency_ms_total=result.metrics.latency_ms_total,
        root_cause_candidates=list(result.root_cause_candidates),
        recommended_actions=list(result.recommended_actions),
    )


async def _run_one_stage(
    *,
    stage: str,
    log_text: str,
    log_ref: str,
    topology_context: dict,
    prompt_overrides: dict[str, str],
    model_overrides: dict[str, str],
    rally_max_rounds: int,
    decision_waiter: TwoStageDecisionWaiter | None,
) -> AsyncIterator[StreamEvent | AnalysisResult]:
    """単一 Stage の rally を実行し、SSE イベントを順次 yield する。

    各 ``run_rally_stream`` のイベントに ``stage`` キーを付加して UI 側で
    Stage の区別をできるようにする。``final`` イベントの代わりに最終 ``AnalysisResult``
    を最後の要素として yield する (呼び出し側で StageOutput に変換)。
    """
    final_result: AnalysisResult | None = None
    async for ev in run_rally_stream(
        log_text,
        log_ref,
        prompt_overrides=prompt_overrides,
        model_overrides=model_overrides,
        rally_max_rounds=rally_max_rounds,
        decision_waiter=decision_waiter,
        topology_context=topology_context,
    ):
        # stage 情報を data に注入 (UI 側で Stage 1/2 の区別に使う)
        if "stage" not in ev.data:
            ev.data["stage"] = stage
        if ev.kind == "final":
            # final は本ラッパが最終 AnalysisResult に統合してから上層に流すため、
            # ここでは生 final を上に流さない。result のみ捕捉。
            payload = ev.data.get("result")
            if isinstance(payload, dict):
                final_result = AnalysisResult.model_validate(payload)
            elif isinstance(payload, AnalysisResult):
                final_result = payload
            continue
        yield ev
    if final_result is None:
        raise RuntimeError(f"stage={stage} ended without producing a result")
    yield final_result


def _build_final_result(
    *,
    stage_outputs: list[StageOutput],
    trace_id: str,
    log_ref: str,
) -> AnalysisResult:
    """Stage 出力から最終 AnalysisResult を組み立てる。

    abort されて Stage 2 が無い場合は Stage 1 の結果をそのまま昇格させる。
    Stage 2 まで進んだ場合は Stage 2 の出力を主結果としつつ、stage_outputs に
    両 Stage 分を残す。
    """
    if not stage_outputs:
        raise ValueError("stage_outputs is empty")
    primary = stage_outputs[-1]  # 最新 Stage を主結果に
    # tokens / latency は両 Stage の合算 (合計コストの可視化のため)
    total_tokens_in = sum(s.tokens_in for s in stage_outputs)
    total_tokens_out = sum(s.tokens_out for s in stage_outputs)
    total_latency_ms = sum(s.latency_ms_total for s in stage_outputs)
    from log_analyzer.schema import Metrics  # 循環回避のため遅延 import
    metrics = Metrics(
        tokens_in=total_tokens_in,
        tokens_out=total_tokens_out,
        latency_ms_total=total_latency_ms,
    )
    info_loss: list[str] = []
    info_loss.append(f"two_stage_mode: stages_completed={len(stage_outputs)}")
    for s in stage_outputs:
        info_loss.append(
            f"{s.stage}: rounds={s.delegation_rounds} confidence={s.confidence:.2f}"
        )
    return AnalysisResult(
        trace_id=trace_id,
        config_id=ConfigId.CONFIG4,
        input_log_ref=log_ref,
        root_cause_candidates=list(primary.root_cause_candidates),
        recommended_actions=list(primary.recommended_actions),
        confidence=primary.confidence,
        metrics=metrics,
        info_loss_flags=info_loss,
        delegation_rounds=primary.delegation_rounds,
        delegation_history=list(primary.delegation_history),
        suspected_node_ids=list(primary.suspected_node_ids),
        suspected_node_findings=list(primary.suspected_node_findings),
        stage_outputs=stage_outputs,
    )


async def run_two_stage_stream(
    *,
    stage_one_log_text: str,
    stage_two_log_text_template: Callable[[StageOutput], str],
    log_ref: str,
    topology_context: dict,
    prompt_overrides: dict[str, str] | None = None,
    model_overrides: dict[str, str] | None = None,
    rally_max_rounds: int = 3,
    decision_waiter: TwoStageDecisionWaiter | None = None,
    audit_after_integrator: bool = False,
) -> AsyncIterator[StreamEvent]:
    """Config-First 2 段階解析の SSE ストリーミング実行。

    呼び出し側 (api.py) が ``stage_one_log_text`` (configs のみで合成済み) と
    ``stage_two_log_text_template`` (Stage 1 結果を受けて Stage 2 ログを返す関数) を
    用意することで、本関数は段階遷移と decision_waiter 制御に専念する。

    SSE シーケンス:
        stage_one_start
        (run_rally_stream のイベント群、stage="config")
        stage_one_complete  ← decision_waiter で advance/abort 待ち
        user_decision
        [advance]: stage_two_start → (rally events stage="log") → final
        [abort]:                                                  → final
    """
    p_overrides = prompt_overrides or {}
    m_overrides = model_overrides or {}
    stage_outputs: list[StageOutput] = []
    overall_trace_id: str = ""

    # ─── Stage 1 ────────────────────────────────────────────────
    yield StreamEvent(
        "stage_one_start",
        {"stage": "config", "stage_label": _STAGE_LABELS["config"]},
    )

    stage_one_result: AnalysisResult | None = None
    stage_one_ctx = dict(topology_context)
    stage_one_ctx["stage"] = "config"
    async for item in _run_one_stage(
        stage="config",
        log_text=stage_one_log_text,
        log_ref=f"{log_ref}::stage1",
        topology_context=stage_one_ctx,
        prompt_overrides=p_overrides,
        model_overrides=m_overrides,
        rally_max_rounds=rally_max_rounds,
        decision_waiter=decision_waiter,
    ):
        if isinstance(item, AnalysisResult):
            stage_one_result = item
        else:
            yield item

    if stage_one_result is None:
        yield StreamEvent("error", {"stage": "config", "message": "Stage 1 が結果を返しませんでした"})
        return

    stage_one_output = _result_to_stage_output("config", stage_one_result)
    stage_outputs.append(stage_one_output)
    overall_trace_id = stage_one_output.trace_id

    # ─── Stage 1 完了通知 + 必須承認待ち ────────────────────────
    yield StreamEvent(
        "stage_one_complete",
        {
            "stage": "config",
            "stage_output": stage_one_output.model_dump(mode="json"),
            "message": "コンフィグ解析が完了しました。ログで事実確認に進むか選択してください。",
        },
    )

    if decision_waiter is None:
        # decision_waiter が無い場合は自動で abort 扱い (テスト経路)
        decision = {"action": "abort"}
    else:
        try:
            decision = await decision_waiter()
        except Exception as e:
            yield StreamEvent("error", {"stage": "await_stage_decision", "message": str(e)})
            return

    yield StreamEvent("user_decision", decision)

    action = str(decision.get("action") or "").lower()
    if action == "abort":
        # Stage 1 のみの最終結果を組み立てて終了
        final = _build_final_result(
            stage_outputs=stage_outputs,
            trace_id=overall_trace_id,
            log_ref=log_ref,
        )
        if audit_after_integrator:
            async for ev in _attach_audit(final, stage_one_log_text, topology_context):
                yield ev
        yield StreamEvent("final", {"result": final.model_dump(mode="json")})
        return
    if action != "advance":
        yield StreamEvent(
            "error",
            {
                "stage": "await_stage_decision",
                "message": f"unknown action='{decision.get('action')}', expected advance/abort",
            },
        )
        return

    # ─── Stage 2 ────────────────────────────────────────────────
    stage_two_log_text = stage_two_log_text_template(stage_one_output)
    yield StreamEvent(
        "stage_two_start",
        {
            "stage": "log",
            "stage_label": _STAGE_LABELS["log"],
            "prior_hypothesis_summary": stage_one_output.summary,
            "prior_suspected_node_ids": list(stage_one_output.suspected_node_ids),
        },
    )

    stage_two_result: AnalysisResult | None = None
    stage_two_ctx = dict(topology_context)
    stage_two_ctx["stage"] = "log"
    stage_two_ctx["prior_hypothesis"] = [
        f.model_dump() for f in stage_one_output.suspected_node_findings
    ]
    async for item in _run_one_stage(
        stage="log",
        log_text=stage_two_log_text,
        log_ref=f"{log_ref}::stage2",
        topology_context=stage_two_ctx,
        prompt_overrides=p_overrides,
        model_overrides=m_overrides,
        rally_max_rounds=rally_max_rounds,
        decision_waiter=decision_waiter,
    ):
        if isinstance(item, AnalysisResult):
            stage_two_result = item
        else:
            yield item

    if stage_two_result is None:
        yield StreamEvent("error", {"stage": "log", "message": "Stage 2 が結果を返しませんでした"})
        return

    stage_two_output = _result_to_stage_output("log", stage_two_result)
    stage_outputs.append(stage_two_output)

    final = _build_final_result(
        stage_outputs=stage_outputs,
        trace_id=overall_trace_id or stage_two_output.trace_id,
        log_ref=log_ref,
    )
    if audit_after_integrator:
        # Stage 2 まで進んだ場合は Stage 2 のログテキストで監査
        async for ev in _attach_audit(final, stage_two_log_text, topology_context):
            yield ev
    yield StreamEvent("final", {"result": final.model_dump(mode="json")})


async def _attach_audit(
    final: AnalysisResult,
    log_text_for_audit: str,
    topology_context: dict,
) -> AsyncIterator[StreamEvent]:
    """最終 AnalysisResult に対して監査 (Phase C) を 1 回実行する補助関数。

    GPT-4o-mini で独立検証し、結果を ``final.audit_report`` にセットする。
    SSE イベントは audit_start / audit_done。失敗時は uncertain で続行
    (本流の final emit を妨げない)。
    """
    import asyncio as _aio
    from log_analyzer.audit_agent import run_audit

    yield StreamEvent("audit_start", {"model_hint": "gpt-4o-mini"})
    loop = _aio.get_running_loop()
    try:
        audit = await loop.run_in_executor(
            None, lambda: run_audit(log_text_for_audit, topology_context, final)
        )
    except Exception as e:
        yield StreamEvent("error", {"stage": "audit", "message": str(e)})
        return
    final.audit_report = audit
    yield StreamEvent(
        "audit_done",
        {
            "verdict": audit.verdict,
            "confidence": audit.confidence,
            "concerns": len(audit.concerns),
            "alternatives": len(audit.alternative_hypotheses),
            "tokens_in": audit.tokens_in,
            "tokens_out": audit.tokens_out,
            "latency_ms": audit.latency_ms,
            "model": audit.model,
        },
    )
