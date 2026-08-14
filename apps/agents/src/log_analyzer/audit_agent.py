"""監査エージェント (Phase C)。

議事録 2026-05-26「監査エージェント (GPT想定)」に対応。
Claude 系で動いた構成4 (rally) の結論を **独立した別モデル (GPT-5.5)** で
ポストホック検証する。

入力:
    - log_text:        rally が読んだのと同じ合成ログ
    - topology_context: トポロジー JSON (任意)
    - analysis_result: Claude が出した AnalysisResult (root_cause_candidates / suspected_nodes 等)

出力:
    AuditReport (verdict: agree/partial/disagree/uncertain)

既定モデルは OpenAI の ``gpt-5.5``（Claude 系本体とは別ベンダーで独立検証する意図）。
``AUDIT_MODEL`` 環境変数で上書き可能。
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone

import openai

from log_analyzer.rally._helpers import safe_extract_json
from log_analyzer.schema import AnalysisResult, AuditReport
from log_analyzer.tracing import get_client, usage_for

_logger = logging.getLogger("uvicorn.error")

_DEFAULT_AUDIT_MODEL = "gpt-5.5"


SYSTEM_PROMPT = """\
あなたは独立した監査エージェントです。
別チーム (Claude 系 LLM) が出した障害原因分析の結論を、与えられたログと
トポロジー情報のみから **独立に検証** してください。

監査の観点:
1. 主原因 (root_cause_candidates の先頭) はログ・コンフィグの証拠と整合しているか
2. 見落とされた別の原因候補はないか
3. suspected_nodes の severity 区分は妥当か (primary / secondary の逆転がないか)
4. 推奨アクション (recommended_actions) で過剰 / 不足はないか
5. confidence は提示された証拠に対して妥当か

verdict の選択:
- "agree":     主原因も severity も推奨アクションも妥当。指摘事項なし or 軽微
- "partial":   主原因は妥当だが、副次的な抜け / 過大評価 がある
- "disagree":  主原因が別にあると判断。alternative_hypotheses に代替案
- "uncertain": 提供情報が薄く判断不能

出力 (JSON のみ、コードフェンスなし):
{
  "verdict": "agree|partial|disagree|uncertain",
  "confidence": 0.0,
  "summary": "監査の総評 (1-2 文、日本語)",
  "concerns": ["指摘事項 1", "指摘事項 2"],
  "alternative_hypotheses": ["別案 1 (主に disagree/partial のとき)"]
}

ルール:
- 同調圧力に屈しない。Claude 側の結論を盲信せず、証拠を独立に評価すること
- 自然文 (summary / concerns / alternative_hypotheses の要素) は日本語
- 出力は JSON のみ
"""


# ─── 監査入力の予算化 (トークン上限・コスト対策) ─────────────────────
# 監査 GPT には log_text 全文を渡していたため、入力上限超過・高コストの原因に
# なっていた。ノード単位で本文を頭/末尾に切り詰め、全体の文字数にも上限を設ける。
# いずれも環境変数で調整可能。
_NODE_DELIM = "=== NODE:"


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _truncate_lines(text: str, head: int, tail: int) -> str:
    """行単位で頭 head 行＋末尾 tail 行だけ残し、中間を省略注記に置き換える。"""
    lines = text.split("\n")
    if len(lines) <= head + tail + 1:
        return text
    omitted = len(lines) - head - tail
    return "\n".join(lines[:head] + [f"...（{omitted} 行省略）..."] + lines[-tail:])


def _budget_log_text(log_text: str, *, head_lines: int, tail_lines: int, max_chars: int) -> str:
    """監査向けに log_text を圧縮する。

    ``=== NODE:`` セクションごとに本文を頭/末尾へ切り詰め、最後に全体文字数の
    セーフティネットを掛ける。証拠の所在 (どのノードか) は残しつつ、巨大な生ログ
    本文だけを削ることで、独立検証に必要な情報を保ったまま入力量を抑える。
    """
    if _NODE_DELIM in log_text:
        # split は区切りを消すので、各セクションにマーカーを復元する
        preamble, *sections = log_text.split(_NODE_DELIM)
        rebuilt = [preamble.rstrip()]
        for sec in sections:
            rebuilt.append(_NODE_DELIM + _truncate_lines(sec, head_lines, tail_lines))
        out = "\n".join(rebuilt)
    else:
        out = _truncate_lines(log_text, head_lines, tail_lines)
    if len(out) > max_chars:  # 最終セーフティネット
        keep = max_chars // 2
        out = out[:keep] + f"\n...（全体で {len(out) - max_chars} 文字省略）...\n" + out[-keep:]
    return out


def _compact_analysis(ar: AnalysisResult) -> dict:
    """監査に渡す分析結果 JSON を圧縮する。

    検証に必要な要素 (主原因・証拠・severity・推奨アクションの過不足判断材料) は
    残し、ジュニア向けの詳細な実行手順 / リスク列挙 / ロールバック手順など、独立
    検証には不要で嵩む情報は落とす。
    """
    max_evidence = _env_int("AUDIT_MAX_EVIDENCE_PER_CAUSE", 5)
    rcc = []
    for c in ar.root_cause_candidates:
        d = c.model_dump(mode="json")
        rcc.append({
            "category": d.get("category"),
            "summary": d.get("summary"),
            "evidence": (d.get("evidence") or [])[:max_evidence],
        })
    ra = []
    for a in ar.recommended_actions:
        d = a.model_dump(mode="json")
        # 過剰/不足の判断に必要な最小限のみ (steps/risks/rollback_note は落とす)
        ra.append({k: d.get(k) for k in
                   ("action", "kind", "risk_level", "confidence", "human_judgment_required")})
    return {
        "root_cause_candidates": rcc,
        "recommended_actions": ra,
        "confidence": ar.confidence,
        "suspected_node_ids": list(ar.suspected_node_ids),
        "suspected_node_findings": [f.model_dump(mode="json") for f in ar.suspected_node_findings],
    }


def _format_bq_evidence(
    bq_evidence: list[dict], *, head_lines: int, tail_lines: int, max_chars: int
) -> str:
    """rally が BigQuery から取得した実ログ (証拠) を予算内に整形する。

    BQ ノードは log_text にマーカーしか入らないため、監査が「rally が実際に何を
    見たか」を検証できるよう、取得行をここで渡す。各取得ブロックを頭/末尾に
    切り詰め、全体にも文字上限を掛ける。
    """
    blocks: list[str] = []
    for e in bq_evidence or []:
        host = (e or {}).get("host") or "?"
        content = _truncate_lines(str((e or {}).get("content") or ""), head_lines, tail_lines)
        if content.strip():
            blocks.append(f"[host={host}]\n{content}")
    text = "\n\n".join(blocks)
    if len(text) > max_chars:
        keep = max_chars // 2
        text = text[:keep] + f"\n...（全体で {len(text) - max_chars} 文字省略）...\n" + text[-keep:]
    return text


def _build_user_input(
    log_text: str,
    topology_context: dict | None,
    analysis_result: AnalysisResult,
    bq_evidence: list[dict] | None = None,
) -> str:
    """監査向け user メッセージを組み立てる (入力量を予算内に圧縮する)。"""
    head = _env_int("AUDIT_NODE_HEAD_LINES", 40)
    tail = _env_int("AUDIT_NODE_TAIL_LINES", 20)
    bounded = _budget_log_text(
        log_text,
        head_lines=head,
        tail_lines=tail,
        max_chars=_env_int("AUDIT_MAX_INPUT_CHARS", 40000),
    )
    parts: list[str] = []
    parts.append("## 与えられたログ・コンフィグ・(問診票・トポロジー)")
    parts.append("（入力上限のため、各ノードのログは先頭/末尾を残して中間を省略しています）")
    parts.append(bounded)
    parts.append("")
    # rally が BigQuery から実際に取得した行を証拠として提示 (マーカーしか無い BQ
    # ノードでも、監査が rally の参照実態を検証できるようにする)。
    if bq_evidence:
        ev_text = _format_bq_evidence(
            bq_evidence, head_lines=head, tail_lines=tail,
            max_chars=_env_int("AUDIT_MAX_EVIDENCE_CHARS", 20000),
        )
        if ev_text.strip():
            parts.append("## rally が BigQuery から実際に取得したログ (証拠)")
            parts.append(ev_text)
            parts.append("")
    if topology_context:
        parts.append("## トポロジー (参考)")
        parts.append(json.dumps(topology_context, ensure_ascii=False, indent=2))
        parts.append("")
    parts.append("## 監査対象: Claude 系で出された分析結果")
    # 監査の認知バイアスを下げるため、原文 JSON を (圧縮しつつ) 素のまま提示する
    parts.append(json.dumps(_compact_analysis(analysis_result), ensure_ascii=False, indent=2))
    parts.append("")
    parts.append("以上の証拠だけから、上記分析結果を独立に検証してください。")
    return "\n".join(parts)


def _emit_generation(
    trace_id: str | None,
    *,
    model: str,
    user_input: str,
    output: str,
    tokens_in: int,
    tokens_out: int,
    started_at: datetime,
    ended_at: datetime,
    status_message: str | None = None,
) -> None:
    """監査 1 回分を Langfuse の Generation として記録する (確認事項 B-1)。

    監査は rally のトレース生成後に別実行されるため、``trace_id`` を指定して
    既存トレースにぶら下げる。監査は補助機能なので、記録に失敗しても
    本流 (AuditReport の返却) は止めない。
    """
    if not trace_id:
        return
    try:
        get_client().generation(
            trace_id=trace_id,
            name=f"{model}-audit",
            model=model,
            input=user_input[:2000],
            output=output,
            usage=usage_for(model, tokens_in, tokens_out),
            start_time=started_at,
            end_time=ended_at,
            **({"level": "WARNING", "status_message": status_message} if status_message else {}),
        )
    except Exception as e:  # noqa: BLE001 — 記録失敗で監査結果を捨てない
        _logger.warning("[audit] Langfuse への generation 記録に失敗: %s", e)


def run_audit(
    log_text: str,
    topology_context: dict | None,
    analysis_result: AnalysisResult,
    *,
    model: str | None = None,
    system_prompt: str | None = None,
    bq_evidence: list[dict] | None = None,
    trace_id: str | None = None,
) -> AuditReport:
    """同期的に GPT 監査を 1 回実行し、AuditReport を返す。

    ``system_prompt`` を渡すと既定の :data:`SYSTEM_PROMPT` の代わりに使う
    (UI から監査プロンプトを編集する用途)。空文字 / None なら既定にフォールバック。

    ``trace_id`` を渡すと、監査の入出力・トークン・所要時間を Langfuse の
    Generation としてそのトレースに記録する (確認事項 B-1)。省略時は記録しない
    (CLI / 単体テスト用)。

    API キーが無い等で例外が出た場合は ``verdict='uncertain'`` の
    フォールバック AuditReport を返し、上層には伝播させない。監査は
    補助情報であり、本流の AnalysisResult を壊さない方針。
    """
    chosen_model = model or os.environ.get("AUDIT_MODEL") or _DEFAULT_AUDIT_MODEL
    sys_prompt = (system_prompt or "").strip() or SYSTEM_PROMPT
    if not os.environ.get("OPENAI_API_KEY"):
        return AuditReport(
            verdict="uncertain",
            confidence=0.0,
            summary="OPENAI_API_KEY が未設定のため監査をスキップしました。",
            model=chosen_model,
        )

    started = time.perf_counter()
    started_at = datetime.now(timezone.utc)
    user_input = _build_user_input(log_text, topology_context, analysis_result, bq_evidence)
    try:
        client = openai.OpenAI()
        # GPT-5.x は Responses API + reasoning.effort / text.verbosity が推奨
        # (OpenAI 公式 GPT-5.5 ガイダンス)。監査は補助タスクなので effort=low / verbosity=low。
        # reasoning トークンが出力枠を食って空応答になるのを避けるため max_output_tokens は余裕を持たせる。
        response = client.responses.create(
            model=chosen_model,
            instructions=sys_prompt,
            input=user_input,
            max_output_tokens=4000,
            reasoning={"effort": "low"},
            text={"verbosity": "low"},
        )
        latency_ms = int((time.perf_counter() - started) * 1000)
        raw = (getattr(response, "output_text", None) or "")
        parsed, parse_error = safe_extract_json(
            raw,
            fallback={
                "verdict": "uncertain",
                "confidence": 0.0,
                "summary": "監査 JSON のパースに失敗",
                "concerns": [],
                "alternative_hypotheses": [],
            },
        )
        verdict = str(parsed.get("verdict") or "uncertain").strip().lower()
        if verdict not in {"agree", "partial", "disagree", "uncertain"}:
            verdict = "uncertain"
        usage = getattr(response, "usage", None)
        # Responses API の usage は input_tokens / output_tokens
        tokens_in = int(getattr(usage, "input_tokens", 0) or 0) if usage else 0
        tokens_out = int(getattr(usage, "output_tokens", 0) or 0) if usage else 0
        summary = str(parsed.get("summary") or "").strip()
        if parse_error:
            summary = (summary + f" [parse_error: {parse_error[:120]}]").strip()
        _emit_generation(
            trace_id,
            model=chosen_model,
            user_input=user_input,
            output=raw,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            started_at=started_at,
            ended_at=datetime.now(timezone.utc),
            status_message=f"parse_error: {parse_error[:200]}" if parse_error else None,
        )
        return AuditReport(
            verdict=verdict,
            confidence=float(parsed.get("confidence") or 0.0),
            summary=summary,
            concerns=[str(c) for c in (parsed.get("concerns") or []) if str(c).strip()],
            alternative_hypotheses=[str(h) for h in (parsed.get("alternative_hypotheses") or []) if str(h).strip()],
            model=chosen_model,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            latency_ms=latency_ms,
        )
    except Exception as e:
        # 失敗も Generation として残す (何も出ないと「監査が動いたか」すら追えないため)
        _emit_generation(
            trace_id,
            model=chosen_model,
            user_input=user_input,
            output="",
            tokens_in=0,
            tokens_out=0,
            started_at=started_at,
            ended_at=datetime.now(timezone.utc),
            status_message=f"audit failed: {str(e)[:200]}",
        )
        return AuditReport(
            verdict="uncertain",
            confidence=0.0,
            summary=f"監査エージェント呼び出しでエラー: {e}",
            model=chosen_model,
            latency_ms=int((time.perf_counter() - started) * 1000),
        )
