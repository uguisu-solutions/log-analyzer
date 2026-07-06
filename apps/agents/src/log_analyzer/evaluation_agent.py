"""評価エージェント (評価機能 Phase 2)。

解析レポート (AnalysisResult) を、エンジニアの模範解答 (Excel「テストケース2」の
①〜⑦) と突き合わせ、**真因 (⑥) にどれだけ到達したか** を 10 段階で採点する。
⑦ (ジュニアの落とし穴) を踏んだ / 避けたも評価軸にする。手採点の基準 = 真因到達度。

判定モデルは既定 ``claude-opus-4-7`` (解析ノードと同一)。``EVAL_MODEL`` で上書き可
(独立性が欲しければ別ベンダーモデルに切替可能)。入力は最終結果＋解答のみで小さく、
巨大な log_text は渡さないため入力上限の問題は起きない。

失敗時 (API キー無し等) は score=0 のフォールバック EvaluationResult を返し、
上層には例外を伝播させない。
"""
from __future__ import annotations

import json
import os
import time

import anthropic

from log_analyzer.rally._helpers import safe_extract_json
from log_analyzer.schema import EvaluationResult

_DEFAULT_EVAL_MODEL = "claude-opus-4-7"


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


SYSTEM_PROMPT = """\
あなたは障害解析レポートの採点者です。
このPoCで評価するのは「AIが正解（真因）を当てられたか」ではなく、
「不完全な情報しか持たないジュニアエンジニアの思考を、正しい方向へ広げられたか
（＝考え方・推論の道筋を見せられたか）」です。

模範解答（エンジニアの①トリガー〜⑥結論、⑦ジュニアの落とし穴）は、
「熟練者ならどう考え、どの罠を避けたか」の参照点として使います。
AIがその思考の道筋をどれだけ再現・補完し、ジュニアの視野を広げられたかを採点してください。
⑥（真因）そのものへの一致は「方向が合っているかの参考」に留め、一致を高得点の条件にしないこと。

採点の6観点（満たすほど高評価。採点根拠として axis_assessment に各観点の評価を必ず書く）:
1. 推論の道筋・複数パス: 単一の断定ではなく「こういう調査パスがあり、こういう可能性が
   あり、それぞれの場合こう動く」という推論のメニューを示せているか。
2. 視野の拡張: 「あなたが見ていない可能性はこれ」「この方向の調査はしたか」と、
   ジュニアの考察範囲を広げる問いかけができているか。
3. 除外理由・思考の再現: なぜその方向に至ったか、なぜ他の仮説を退けたかの根拠を示し、
   模範解答の①〜⑥に相当する思考構造を再現できているか。
4. 「わからない」の明示: 未確認・未検証の点、前提（設定値・構成）の妥当性を確認できて
   いない点を正直に明示できているか（例:「タイムアウト値の妥当性は未確認」）。
5. 前提ズレの指摘: 問診票の申告やジュニアの思い込みと、実際の構成図・ログとのズレを
   AI側から指摘できているか（人間が見落とす前提の食い違い）。
6. 初手バイアス非固定: 初手で立てた仮説に最後まで引きずられず、途中で「これは違うかも」と
   方向転換・再検討できているか（推論チェーンが与えられている場合）。

重要な原則:
- 真因に到達していても、単一の断定で道筋を見せていなければ高得点を与えない。
- 真因を外していても、正しい方向へ視野を広げ「わからない」を明示できていれば中〜高評価。
- 現フェーズは「推論支援」であり「解決支援」ではない。暫定/本質アクションの具体的正確性
  よりも、そこに至る推論過程を重視すること。
- 「推論チェーン(reasoning_chain)」が与えられている場合、推論過程では良い視点が出ている
  のに最終結果(root_cause_candidates)で単一の断定に潰れていないかを見て、その差分を
  bad_points に明記すること（IBCが最も問題視している点）。

スコア（1-10, 推論支援価値。これのみで総合評価）:
- 9-10: 複数の調査パス・可能性を根拠付きで提示し、未確認/前提ズレも明示。視野を確実に広げる。
- 7-8:  主要な推論の道筋と除外理由を示し、不足も一部明示。おおむね思考を前進させる。
- 5-6:  一定の推論はあるが単一結論に寄る／視野拡張・不足明示が弱い。
- 3-4:  断定的で別可能性・根拠が乏しい。思考を広げない。
- 1-2:  単一の（しばしば初手バイアスの）結論のみ。別案・不足・根拠の明示なし。

出力 (JSON のみ、コードフェンスなし):
{
  "score": 1-10 の整数,
  "axis_assessment": [
    "推論の道筋・複数パス: <この観点の評価>",
    "視野の拡張: <評価>",
    "除外理由・思考の再現: <評価>",
    "わからないの明示: <評価>",
    "前提ズレの指摘: <評価>",
    "初手バイアス非固定: <評価>"
  ],
  "good_points": ["思考支援として良かった点（どの観点かを明示）", "..."],
  "bad_points": ["視野・道筋・不足明示の観点で足りない点（推論と最終出力の差分も）", "..."],
  "pitfalls_avoided": ["⑦ジュニアの落とし穴のうち、AIが回避を助けたもの"],
  "pitfalls_hit": ["⑦のうち、AIが自ら踏んでいた/助けられなかったもの"],
  "summary": "総評（1-3文。思考支援価値と、推論過程が最終出力に反映されているか）"
}

ルール:
- axis_assessment は必ず 6 観点すべてを書く（採点根拠の明示）。
- 絵文字・装飾記号（✓ ✗ ● ★ ▲ 🔴 等）を一切使わない。プレーンテキストの日本語のみ。
- 模範解答の丸数字（①〜⑦）を単独で使わず、必ず意味を併記すること。読み手が番号体系を
  知らなくても分かるように書く。対応: ①（トリガー）②（初期仮説）③（辿った経路）
  ④（決断点・除外理由）⑤（根拠の出所）⑥（結論・真因）⑦（ジュニアの落とし穴）。
  例:「⑥（結論・真因）の核心に未到達」「④（決断点）の除外理由が薄い」。
- 自然文はすべて日本語。出力は JSON のみ。
"""


def _compact_report(result: dict) -> dict:
    """解析結果 dict から採点に必要な要素だけ抜き出す (嵩む手順/リスクは落とす)。"""
    max_ev = _env_int("EVAL_MAX_EVIDENCE_PER_CAUSE", 5)
    rcc = []
    for c in (result.get("root_cause_candidates") or []):
        if not isinstance(c, dict):
            continue
        rcc.append({
            "category": c.get("category"),
            "summary": c.get("summary"),
            "evidence": (c.get("evidence") or [])[:max_ev],
        })
    ra = []
    for a in (result.get("recommended_actions") or []):
        if not isinstance(a, dict):
            continue
        ra.append({k: a.get(k) for k in ("action", "kind", "confidence")})
    # 推論チェーン (思考の跡): 委譲履歴の rationale / focus_hint を渡し、
    # 「推論過程は良いのに最終出力で潰れた」を判定できるようにする (IBC 要求)。
    chain = []
    for d in (result.get("delegation_history") or []):
        if not isinstance(d, dict):
            continue
        rationale = str(d.get("rationale") or "").strip()
        focus = str(d.get("focus_hint") or "").strip()
        if not rationale and not focus:
            continue
        chain.append({
            "from": d.get("from_node"), "to": d.get("to_node"),
            "confidence": d.get("confidence"),
            "rationale": rationale,
            "focus_hint": focus,
        })
    return {
        "reasoning_chain": chain,
        "root_cause_candidates": rcc,
        "recommended_actions": ra,
        "confidence": result.get("confidence"),
    }


def _format_answer(scenario: dict) -> str:
    """解答シナリオ (①〜⑦＋補足) を採点用テキストに整形する。⑥⑦を強調。"""
    def g(k: str) -> str:
        return str(scenario.get(k) or "").strip()

    lines = [
        f"# 模範解答: {scenario.get('scenario_key', '')} {g('title')}",
        "（熟練者の思考の道筋。①〜⑤＝どう考え何を辿ったか、⑥＝方向の参照点、⑦＝避けるべき罠）",
        "",
        "## ①トリガー", g("trigger"),
        "## ②初期仮説", g("initial_hypothesis"),
        "## ③辿った経路 ★思考の道筋の参照", g("path"),
        "## ④決断点 (何をなぜ除外したか) ★除外理由の参照", g("decision_points"),
        "## ⑤根拠の出所", g("evidence_source"),
        "## ⑥結論 (真因・対処) ※方向の参照点。一致は必須でない", g("conclusion"),
        "## ⑦ジュニアの落とし穴 ★AIが回避を助けられたかを見る", g("junior_pitfall"),
    ]
    notes = g("notes")
    if notes:
        lines += ["## 補足", notes]
    return "\n".join(lines)


def _build_user_input(result: dict, scenario: dict) -> str:
    parts = [
        _format_answer(scenario),
        "",
        "---",
        "",
        "# 採点対象: AI の解析レポート",
        "（reasoning_chain＝推論の跡、root_cause_candidates／recommended_actions＝最終出力）",
        json.dumps(_compact_report(result), ensure_ascii=False, indent=2),
        "",
        "上記の模範解答（熟練者の思考の道筋）を参照点に、レポートが**ジュニアの思考を"
        "正しい方向へ広げられたか（推論支援価値）**を6観点で採点してください。"
        "真因への一致そのものではなく、道筋・複数パス・不足の明示・前提ズレの指摘を重視すること。",
    ]
    return "\n".join(parts)


def run_evaluation(
    result: dict,
    scenario: dict,
    *,
    model: str | None = None,
    system_prompt: str | None = None,
) -> EvaluationResult:
    """解析結果 dict と解答シナリオ dict を比較採点する。同期実行。

    ``result``   : analysis_history の result (AnalysisResult 相当の dict)
    ``scenario`` : answer_scenarios の 1 行 (①〜⑦＋補足)
    失敗時は score=0 のフォールバックを返す (例外は伝播させない)。
    """
    chosen_model = model or os.environ.get("EVAL_MODEL") or _DEFAULT_EVAL_MODEL
    sys_prompt = (system_prompt or "").strip() or SYSTEM_PROMPT
    scenario_key = str(scenario.get("scenario_key") or "")

    started = time.perf_counter()
    try:
        client = anthropic.Anthropic()
        # 注: claude-opus-4-7 は temperature 非対応 (指定すると 400)。採点のブレは
        # モデル既定の挙動に委ねる。厳密な再現性が要る場合は temperature 対応モデルを
        # EVAL_MODEL に指定する運用とする。
        response = client.messages.create(
            model=chosen_model,
            max_tokens=_env_int("EVAL_MAX_TOKENS", 3000),
            system=sys_prompt,
            messages=[{"role": "user", "content": _build_user_input(result, scenario)}],
        )
        latency_ms = int((time.perf_counter() - started) * 1000)
        raw = response.content[0].text if response.content else ""
        parsed, parse_error = safe_extract_json(
            raw,
            fallback={"score": 0, "axis_assessment": [], "good_points": [], "bad_points": [],
                      "pitfalls_avoided": [], "pitfalls_hit": [], "summary": "評価 JSON パース失敗"},
        )
        try:
            score = int(round(float(parsed.get("score") or 0)))
        except (TypeError, ValueError):
            score = 0
        score = max(0, min(10, score))

        def _strlist(v) -> list[str]:
            if not isinstance(v, list):
                return []
            return [str(x).strip() for x in v if str(x).strip()]

        summary = str(parsed.get("summary") or "").strip()
        if parse_error:
            summary = (summary + f" [parse_error: {parse_error[:120]}]").strip()
        usage = getattr(response, "usage", None)
        return EvaluationResult(
            scenario_key=scenario_key,
            score=score,
            axis_assessment=_strlist(parsed.get("axis_assessment")),
            good_points=_strlist(parsed.get("good_points")),
            bad_points=_strlist(parsed.get("bad_points")),
            pitfalls_avoided=_strlist(parsed.get("pitfalls_avoided")),
            pitfalls_hit=_strlist(parsed.get("pitfalls_hit")),
            summary=summary,
            model=chosen_model,
            tokens_in=int(getattr(usage, "input_tokens", 0) or 0) if usage else 0,
            tokens_out=int(getattr(usage, "output_tokens", 0) or 0) if usage else 0,
            latency_ms=latency_ms,
        )
    except Exception as e:  # noqa: BLE001 — 評価は補助機能、本流を壊さない
        return EvaluationResult(
            scenario_key=scenario_key,
            score=0,
            summary=f"評価エージェント呼び出しでエラー: {e}",
            model=chosen_model,
            latency_ms=int((time.perf_counter() - started) * 1000),
        )
