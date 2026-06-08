"""監査エージェント (Phase C)。

議事録 2026-05-26「監査エージェント (GPT想定)」に対応。
Claude 系で動いた構成4 (rally) の結論を **独立した別モデル (GPT-4o-mini)** で
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
import os
import time

import openai

from log_analyzer.rally._helpers import safe_extract_json
from log_analyzer.schema import AnalysisResult, AuditReport


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


def _build_user_input(
    log_text: str, topology_context: dict | None, analysis_result: AnalysisResult
) -> str:
    """監査向け user メッセージを組み立てる。"""
    parts: list[str] = []
    parts.append("## 与えられたログ・コンフィグ・(問診票・トポロジー)")
    parts.append(log_text)
    parts.append("")
    if topology_context:
        parts.append("## トポロジー (参考)")
        parts.append(json.dumps(topology_context, ensure_ascii=False, indent=2))
        parts.append("")
    parts.append("## 監査対象: Claude 系で出された分析結果")
    # 監査の認知バイアスを下げるため、原文 JSON を素のまま提示する
    parts.append(json.dumps(
        {
            "root_cause_candidates": [c.model_dump() for c in analysis_result.root_cause_candidates],
            "recommended_actions": [a.model_dump() for a in analysis_result.recommended_actions],
            "confidence": analysis_result.confidence,
            "suspected_node_ids": list(analysis_result.suspected_node_ids),
            "suspected_node_findings": [f.model_dump() for f in analysis_result.suspected_node_findings],
        },
        ensure_ascii=False,
        indent=2,
    ))
    parts.append("")
    parts.append("以上の証拠だけから、上記分析結果を独立に検証してください。")
    return "\n".join(parts)


def run_audit(
    log_text: str,
    topology_context: dict | None,
    analysis_result: AnalysisResult,
    *,
    model: str | None = None,
    system_prompt: str | None = None,
) -> AuditReport:
    """同期的に GPT 監査を 1 回実行し、AuditReport を返す。

    ``system_prompt`` を渡すと既定の :data:`SYSTEM_PROMPT` の代わりに使う
    (UI から監査プロンプトを編集する用途)。空文字 / None なら既定にフォールバック。

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
    try:
        client = openai.OpenAI()
        # GPT-5 系は max_tokens 非対応 (max_completion_tokens を使う) かつ temperature は既定(1)のみ。
        # 新旧両対応のため max_completion_tokens を使い、temperature は非 GPT-5 のときだけ付ける。
        create_kwargs: dict = {
            "model": chosen_model,
            "max_completion_tokens": 1500,
            "messages": [
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": _build_user_input(log_text, topology_context, analysis_result)},
            ],
        }
        if not chosen_model.startswith("gpt-5"):
            create_kwargs["temperature"] = 0.1
        response = client.chat.completions.create(**create_kwargs)
        latency_ms = int((time.perf_counter() - started) * 1000)
        raw = response.choices[0].message.content or ""
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
        tokens_in = int(getattr(usage, "prompt_tokens", 0) or 0) if usage else 0
        tokens_out = int(getattr(usage, "completion_tokens", 0) or 0) if usage else 0
        summary = str(parsed.get("summary") or "").strip()
        if parse_error:
            summary = (summary + f" [parse_error: {parse_error[:120]}]").strip()
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
        return AuditReport(
            verdict="uncertain",
            confidence=0.0,
            summary=f"監査エージェント呼び出しでエラー: {e}",
            model=chosen_model,
            latency_ms=int((time.perf_counter() - started) * 1000),
        )
