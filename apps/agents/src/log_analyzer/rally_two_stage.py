"""config-log 解析の 2 段階オーケストレータ。

議事録 (2026-05-26) で合意された「人の思考プロセスに近い」検証パターンを
一般化したもの。Stage 1 で当たりをつけ、人間承認を挟んで Stage 2 で検証する。

    Stage 1: 一方のデータ種別 (config もしくは log) だけで原因の当たりをつける
       │
       ▼
    人間による必須承認 (advance / abort)
       │
       ▼
    Stage 2: もう一方を加えて事実確認する

2 段階の順序は ``stage_one_kind`` / ``stage_two_kind`` で指定する:

- ``config`` → ``log`` (既定): コンフィグで仮説 → ログで検証
- ``log`` → ``config``:        ログで仮説 → コンフィグで裏取り

各 Stage の内部は既存 ``run_rally_stream`` をそのまま再利用する (プロンプトは変えない)。
Stage 1 と Stage 2 の差は **integrator に渡される log_text** だけ。具体的な合成は
呼び出し側 (api.py) が ``stage_one_log_text`` と ``stage_two_log_text_template`` で用意し、
本モジュールは段階遷移と decision_waiter 制御に専念する。

SSE 経由でブラウザに 2 段階分のイベントを順に流す。Stage 1 完了時に
``stage_one_complete`` を emit して decision_waiter で人間入力を待つ。
"""
from __future__ import annotations

from typing import Any, AsyncIterator, Callable, Awaitable

from log_analyzer.rally import source_tools
from log_analyzer.rally_agent import StreamEvent, run_rally_stream
from log_analyzer.schema import (
    AnalysisResult,
    ConfigId,
    StageOutput,
)

# Phase A の decision_waiter は 4 アクションを受ける:
#   {"action": "continue", "extend_by": int}     rally_max_rounds 延長 (既存)
#   {"action": "stop"}                            integrator 直行 (既存)
#   {"action": "advance"}                         Stage 1 → Stage 2 へ進む (新)
#   {"action": "abort"}                           Stage 1 で終了 (新)
TwoStageDecisionWaiter = Callable[[], Awaitable[dict[str, Any]]]


# データ種別 (config / log / both) ごとの日本語ラベル
_KIND_LABELS = {
    "config": "コンフィグ",
    "log": "ログ",
    "both": "コンフィグ + ログ",
}
# 仮説ブロックの「検証対象」表現
_VERIFY_TARGET_LABELS = {
    "config": "設定情報",
    "log": "実ログ",
}
# 仮説ブロックの「仮説の出所」表現
_SOURCE_LABELS = {
    "config": "コンフィグ解析",
    "log": "ログ解析",
}


def _stage_label(ordinal: int, kind: str) -> str:
    """Stage 表示ラベルを順序とデータ種別から組み立てる。

    1 段目は「解析」(当たりをつける)、2 段目は「検証」(事実確認) と表現する。
    """
    base = _KIND_LABELS.get(kind, kind)
    suffix = "解析" if ordinal <= 1 else "検証"
    return f"Stage {ordinal}: {base}{suffix}"


def _build_stage_one_hypothesis_block(
    stage_one: StageOutput,
    source_kind: str = "config",
    target_kind: str = "log",
) -> str:
    """Stage 1 の結果を Stage 2 の log_text 先頭に挿入する仮説ブロック。

    LLM (integrator / 監視) は自然にこのブロックを読み「仮説を検証する」モードになる。
    ``source_kind`` は仮説の出所 (config / log)、``target_kind`` は検証に使うデータ種別。
    """
    if not stage_one.suspected_node_findings and not stage_one.summary:
        return ""
    source_label = _SOURCE_LABELS.get(source_kind, source_kind)
    target_label = _VERIFY_TARGET_LABELS.get(target_kind, target_kind)
    lines: list[str] = []
    lines.append(f"## Stage 1 仮説 ({source_label}より)")
    if stage_one.summary:
        lines.append(stage_one.summary)
    if stage_one.suspected_node_findings:
        lines.append("")
        lines.append(f"{source_label}から推定された障害候補ノード:")
        for f in stage_one.suspected_node_findings:
            sev = f.severity or "?"
            lines.append(f"- {f.node_id} [{sev}]: {f.summary or '(詳細未記載)'}")
    lines.append("")
    lines.append(f"以下の{target_label}でこれらの仮説を **検証** してください。")
    lines.append("仮説が裏付けられれば確証を強め、矛盾があれば修正してください。")
    lines.append("")
    return "\n".join(lines) + "\n"


def _result_to_stage_output(
    stage: str, result: AnalysisResult, *, stage_label: str | None = None
) -> StageOutput:
    """``run_rally_stream`` の最終 result を StageOutput に変換。"""
    summary_parts: list[str] = []
    for c in result.root_cause_candidates[:2]:
        summary_parts.append(f"{c.category.value if hasattr(c.category, 'value') else c.category}: {c.summary}")
    summary = " / ".join(summary_parts) if summary_parts else ""
    return StageOutput(
        stage=stage,
        stage_label=stage_label or _stage_label(1, stage),
        confidence=result.confidence,
        summary=summary,
        suspected_node_ids=list(result.suspected_node_ids),
        suspected_node_findings=list(result.suspected_node_findings),
        delegation_rounds=result.delegation_rounds,
        delegation_max_rounds=result.delegation_max_rounds,
        delegation_history=list(result.delegation_history),
        trace_id=result.trace_id,
        tokens_in=result.metrics.tokens_in,
        tokens_out=result.metrics.tokens_out,
        latency_ms_total=result.metrics.latency_ms_total,
        cost_usd=result.metrics.cost_usd,
        root_cause_candidates=list(result.root_cause_candidates),
        recommended_actions=list(result.recommended_actions),
        round_metrics=list(result.round_metrics),
        monitor_reports=list(result.monitor_reports),
    )


async def _run_one_stage(
    *,
    stage: str,
    ordinal: int,
    log_text: str,
    log_ref: str,
    topology_context: dict,
    prompt_overrides: dict[str, str],
    model_overrides: dict[str, str],
    rally_max_rounds: int,
    decision_waiter: TwoStageDecisionWaiter | None,
    bq_sources: dict[str, dict] | None = None,
    evidence_sink: list[dict] | None = None,
    source_index: Any = None,
    source_db_schema: Any = None,
    source_codebase: str = "",
) -> AsyncIterator[StreamEvent | AnalysisResult]:
    """単一 Stage の rally を実行し、SSE イベントを順次 yield する。

    各 ``run_rally_stream`` のイベントに ``stage`` (データ種別) と ``stage_ordinal``
    (1 / 2) を付加して UI 側で Stage の区別をできるようにする。``final`` イベントの
    代わりに最終 ``AnalysisResult`` を最後の要素として yield する (呼び出し側で
    StageOutput に変換)。

    ``evidence_sink`` を渡すと、内側 rally が BigQuery から取得した実ログ
    (final イベント同梱の ``bq_evidence``) をそこへ蓄積する。監査へ渡す証拠用。
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
        bq_sources=bq_sources,
        source_index=source_index,
        source_db_schema=source_db_schema,
        source_codebase=source_codebase,
    ):
        # stage 情報を data に注入 (UI 側で Stage 1/2 の区別に使う)
        if "stage" not in ev.data:
            ev.data["stage"] = stage
        ev.data.setdefault("stage_ordinal", ordinal)
        if ev.kind == "final":
            # final は本ラッパが最終 AnalysisResult に統合してから上層に流すため、
            # ここでは生 final を上に流さない。result と bq_evidence を捕捉。
            payload = ev.data.get("result")
            if isinstance(payload, dict):
                final_result = AnalysisResult.model_validate(payload)
            elif isinstance(payload, AnalysisResult):
                final_result = payload
            if evidence_sink is not None:
                evidence_sink.extend(ev.data.get("bq_evidence") or [])
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
    # 推定コストも tokens / latency と同じく両 Stage の合算 (確認事項 D-2)
    total_cost = sum(s.cost_usd for s in stage_outputs if s.cost_usd is not None)
    from log_analyzer.schema import Metrics  # 循環回避のため遅延 import
    metrics = Metrics(
        tokens_in=total_tokens_in,
        tokens_out=total_tokens_out,
        cost_usd=total_cost,
        latency_ms_total=total_latency_ms,
    )
    info_loss: list[str] = []
    info_loss.append(f"two_stage_mode: stages_completed={len(stage_outputs)}")
    for s in stage_outputs:
        info_loss.append(
            f"{s.stage}: rounds={s.delegation_rounds} confidence={s.confidence:.2f}"
        )
    # round_metrics は両 Stage を直列に連結 (Stage 1 のラウンド → Stage 2 のラウンド)
    combined_rounds: list = []
    for s in stage_outputs:
        combined_rounds.extend(list(s.round_metrics))
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
        # 上限を引き継がないと UI が「N ラウンド / 上限 0」になる (確認事項 D-1)
        delegation_max_rounds=primary.delegation_max_rounds,
        delegation_history=list(primary.delegation_history),
        suspected_node_ids=list(primary.suspected_node_ids),
        suspected_node_findings=list(primary.suspected_node_findings),
        stage_outputs=stage_outputs,
        round_metrics=combined_rounds,
        # 監視の調査根拠 (A-3) は delegation_history と同じく主 Stage 分をトップレベルに。
        # Stage 別は stage_outputs[].monitor_reports に残る。
        monitor_reports=list(primary.monitor_reports),
    )


async def run_two_stage_stream(
    *,
    stage_one_log_text: str,
    stage_two_log_text_template: Callable[[StageOutput], str],
    log_ref: str,
    topology_context: dict,
    stage_one_kind: str = "config",
    stage_two_kind: str = "log",
    prompt_overrides: dict[str, str] | None = None,
    model_overrides: dict[str, str] | None = None,
    rally_max_rounds: int = 3,
    decision_waiter: TwoStageDecisionWaiter | None = None,
    audit_after_integrator: bool = False,
    audit_system_prompt: str | None = None,
    require_approval: bool = False,
    bq_sources: dict[str, dict] | None = None,
    source_index: Any = None,
    source_db_schema: Any = None,
    source_codebase: str = "",
) -> AsyncIterator[StreamEvent]:
    """config-log 解析の 2 段階 SSE ストリーミング実行。

    呼び出し側 (api.py) が ``stage_one_log_text`` (Stage 1 のデータ種別だけで合成済み)
    と ``stage_two_log_text_template`` (Stage 1 結果を受けて Stage 2 ログを返す関数) を
    用意することで、本関数は段階遷移と decision_waiter 制御に専念する。

    ``stage_one_kind`` / ``stage_two_kind`` で順序を指定する:
    ``config`` → ``log`` (既定) または ``log`` → ``config``。

    ``require_approval`` が False (既定) のときは Stage 1 完了後に人間承認を挟まず、
    そのまま自動で Stage 2 へ進む。True のときのみ ``decision_waiter`` で advance/abort を待つ。
    (rally_max_rounds 上限到達時の continue/stop は require_approval に依らず各 Stage 内で機能する。)

    SSE シーケンス:
        stage_one_start
        (run_rally_stream のイベント群、stage=stage_one_kind, stage_ordinal=1)
        stage_one_complete
        user_decision (require_approval=False のときは {"action":"advance","auto":true})
        stage_two_start → (rally events stage=stage_two_kind) → final
        [require_approval=True かつ abort 選択時]: stage_two をスキップして final
    """
    p_overrides = prompt_overrides or {}
    m_overrides = model_overrides or {}
    bq = bq_sources or {}
    # BigQuery はログ取得ルート。ログを含む Stage だけ tool を有効化する。
    # (config 始動の Stage 1 はログを含まないので BQ tool は不要)
    stage_one_bq = bq if stage_one_kind == "log" else {}
    stage_two_bq = bq  # Stage 2 は両データ種別を投入するため常にログを含む
    stage_outputs: list[StageOutput] = []
    overall_trace_id: str = ""
    stage_one_label = _stage_label(1, stage_one_kind)
    stage_two_label = _stage_label(2, stage_two_kind)

    # ─── Stage 1 ────────────────────────────────────────────────
    yield StreamEvent(
        "stage_one_start",
        {"stage": stage_one_kind, "stage_ordinal": 1, "stage_label": stage_one_label},
    )

    # 両 Stage で BigQuery から取得した実ログ (監査の証拠) を蓄積する
    bq_evidence: list[dict] = []

    stage_one_result: AnalysisResult | None = None
    stage_one_ctx = dict(topology_context)
    stage_one_ctx["stage"] = stage_one_kind
    async for item in _run_one_stage(
        stage=stage_one_kind,
        ordinal=1,
        log_text=stage_one_log_text,
        log_ref=f"{log_ref}::stage1",
        topology_context=stage_one_ctx,
        prompt_overrides=p_overrides,
        model_overrides=m_overrides,
        rally_max_rounds=rally_max_rounds,
        decision_waiter=decision_waiter,
        bq_sources=stage_one_bq,
        evidence_sink=bq_evidence,
        source_index=source_index,
        source_db_schema=source_db_schema,
        source_codebase=source_codebase,
    ):
        if isinstance(item, AnalysisResult):
            stage_one_result = item
        else:
            yield item

    if stage_one_result is None:
        yield StreamEvent("error", {"stage": stage_one_kind, "message": "Stage 1 が結果を返しませんでした"})
        return

    stage_one_output = _result_to_stage_output(
        stage_one_kind, stage_one_result, stage_label=stage_one_label
    )
    stage_outputs.append(stage_one_output)
    overall_trace_id = stage_one_output.trace_id

    # ─── Stage 1 完了通知 ───────────────────────────────────────
    _verify_word = _VERIFY_TARGET_LABELS.get(stage_two_kind, stage_two_kind)
    _auto = not require_approval
    yield StreamEvent(
        "stage_one_complete",
        {
            "stage": stage_one_kind,
            "stage_ordinal": 1,
            "stage_output": stage_one_output.model_dump(mode="json"),
            "auto_advance": _auto,
            "message": f"{_SOURCE_LABELS.get(stage_one_kind, stage_one_kind)}が完了しました。"
                       + (f"そのまま{_verify_word}で事実確認に進みます。" if _auto
                          else f"{_verify_word}で事実確認に進むか選択してください。"),
        },
    )

    if require_approval:
        # 人間承認モード: decision_waiter で advance/abort を待つ
        if decision_waiter is None:
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
            final.source_context = source_tools.merge_source_contexts(
                [stage_one_result.source_context]
            )
            if audit_after_integrator:
                async for ev in _attach_audit(final, stage_one_log_text, topology_context,
                                              audit_system_prompt, bq_evidence):
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
    else:
        # 自動進行: 人間承認を挟まずそのまま Stage 2 へ
        yield StreamEvent("user_decision", {"action": "advance", "auto": True})

    # ─── Stage 2 ────────────────────────────────────────────────
    stage_two_log_text = stage_two_log_text_template(stage_one_output)
    yield StreamEvent(
        "stage_two_start",
        {
            "stage": stage_two_kind,
            "stage_ordinal": 2,
            "stage_label": stage_two_label,
            "prior_hypothesis_summary": stage_one_output.summary,
            "prior_suspected_node_ids": list(stage_one_output.suspected_node_ids),
        },
    )

    stage_two_result: AnalysisResult | None = None
    stage_two_ctx = dict(topology_context)
    stage_two_ctx["stage"] = stage_two_kind
    stage_two_ctx["prior_hypothesis"] = [
        f.model_dump() for f in stage_one_output.suspected_node_findings
    ]
    async for item in _run_one_stage(
        stage=stage_two_kind,
        ordinal=2,
        log_text=stage_two_log_text,
        log_ref=f"{log_ref}::stage2",
        topology_context=stage_two_ctx,
        prompt_overrides=p_overrides,
        model_overrides=m_overrides,
        rally_max_rounds=rally_max_rounds,
        decision_waiter=decision_waiter,
        bq_sources=stage_two_bq,
        evidence_sink=bq_evidence,
        source_index=source_index,
        source_db_schema=source_db_schema,
        source_codebase=source_codebase,
    ):
        if isinstance(item, AnalysisResult):
            stage_two_result = item
        else:
            yield item

    if stage_two_result is None:
        yield StreamEvent("error", {"stage": stage_two_kind, "message": "Stage 2 が結果を返しませんでした"})
        return

    stage_two_output = _result_to_stage_output(
        stage_two_kind, stage_two_result, stage_label=stage_two_label
    )
    stage_outputs.append(stage_two_output)

    final = _build_final_result(
        stage_outputs=stage_outputs,
        trace_id=overall_trace_id or stage_two_output.trace_id,
        log_ref=log_ref,
    )
    final.source_context = source_tools.merge_source_contexts(
        [stage_one_result.source_context, stage_two_result.source_context]
    )
    if audit_after_integrator:
        # Stage 2 まで進んだ場合は Stage 2 のログテキストで監査
        async for ev in _attach_audit(final, stage_two_log_text, topology_context,
                                      audit_system_prompt, bq_evidence):
            yield ev
    yield StreamEvent("final", {"result": final.model_dump(mode="json")})


async def _attach_audit(
    final: AnalysisResult,
    log_text_for_audit: str,
    topology_context: dict,
    audit_system_prompt: str | None = None,
    bq_evidence: list[dict] | None = None,
) -> AsyncIterator[StreamEvent]:
    """最終 AnalysisResult に対して監査 (Phase C) を 1 回実行する補助関数。

    GPT-5.5 で独立検証し、結果を ``final.audit_report`` にセットする。
    ``bq_evidence`` には rally が BigQuery から取得した実ログを渡し、監査が
    BQ ノードの参照実態を検証できるようにする。
    SSE イベントは audit_start / audit_done。失敗時は uncertain で続行
    (本流の final emit を妨げない)。
    """
    import asyncio as _aio
    from log_analyzer.audit_agent import run_audit

    yield StreamEvent("audit_start", {"model_hint": "gpt-5.5"})
    loop = _aio.get_running_loop()
    try:
        audit = await loop.run_in_executor(
            None,
            lambda: run_audit(
                log_text_for_audit, topology_context, final,
                system_prompt=audit_system_prompt, bq_evidence=bq_evidence,
                # 監査を Langfuse の Generation として記録する (確認事項 B-1)。
                # 2 段階では監査対象が最終 Stage の結果なので、その trace に紐付く。
                trace_id=final.trace_id or None,
            ),
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
