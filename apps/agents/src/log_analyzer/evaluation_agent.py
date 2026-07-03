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
エンジニアの模範解答 (①〜⑦) と、AI が出した解析レポートを比較し、
**レポートが模範解答の真因 (⑥結論) にどれだけ到達したか** を 10 段階で採点してください。
**スコアは⑥真因への到達度だけで決めます。⑦などの副軸はスコアに一切反映しません**（後述）。

スコアの基準 = ⑥ (真因) への到達度（これのみでスコアを決める）:
- 9-10: 真因を的確に特定 (⑥の核心＝原因の機序に到達し、対処の方向も合う)
- 7-8:  主要因は正しいが、機序の一部や対処が不足
- 5-6:  方向は合うが真因の機序が違う / 部分的にしか当たっていない
- 3-4:  かすっている程度。派生症状や別要因に留まり真因を外している
- 1-2:  真因とは別物

参考情報 = ⑦ (ジュニアの落とし穴)  ※スコアには影響させない:
- ⑦には「同じ事案でジュニアが踏みそうな誤った経路」が列挙されている。
- レポートが避けた罠は pitfalls_avoided、踏んだ罠は pitfalls_hit に列挙する（参考表示用）。
- **⑦ は加点・減点の材料にしないこと。避けた罠が多くても踏んだ罠が多くても、
  score は⑥真因到達度だけで決め、⑦の巧拙で上下させてはならない。**

重要な原則:
- **模範解答 (特に⑥) を絶対基準にする。** レポートが解答と別の、一見もっともらしい
  結論を述べていても、⑥に到達していなければ高得点を与えないこと。
- レポートが「情報不足で断定できない」と正直に述べ、真因に踏み込めていない場合は、
  正直さは good_points に書いてよいが、⑥未到達なら score は中位以下に留める。
- 各 good_points / bad_points は⑥到達度の観点で具体的に書く (真因のどこに到達/未到達か)。
  ⑦の罠の扱いは good_points / bad_points ではなく pitfalls_avoided / pitfalls_hit に書く。

出力 (JSON のみ、コードフェンスなし):
{
  "score": 1-10 の整数,
  "good_points": ["良い点1", "..."],
  "bad_points": ["悪い点1", "..."],
  "pitfalls_avoided": ["⑦のうち避けられていた罠", "..."],
  "pitfalls_hit": ["⑦のうち踏んでいた罠", "..."],
  "summary": "総評 (1-3 文、日本語。到達度と主な差分)"
}

ルール:
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
    return {
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
        "",
        "## ①トリガー", g("trigger"),
        "## ②初期仮説", g("initial_hypothesis"),
        "## ③辿った経路", g("path"),
        "## ④決断点 (除外理由)", g("decision_points"),
        "## ⑤根拠の出所", g("evidence_source"),
        "## ⑥結論 (真因・対処) ★採点の主軸", g("conclusion"),
        "## ⑦ジュニアの落とし穴 ★踏んだ/避けたを評価", g("junior_pitfall"),
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
        "# 採点対象: AI の解析レポート (最終結果を抜粋)",
        json.dumps(_compact_report(result), ensure_ascii=False, indent=2),
        "",
        "上記の模範解答 (特に⑥真因) を基準に、レポートの到達度を採点してください。",
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
            max_tokens=_env_int("EVAL_MAX_TOKENS", 2000),
            system=sys_prompt,
            messages=[{"role": "user", "content": _build_user_input(result, scenario)}],
        )
        latency_ms = int((time.perf_counter() - started) * 1000)
        raw = response.content[0].text if response.content else ""
        parsed, parse_error = safe_extract_json(
            raw,
            fallback={"score": 0, "good_points": [], "bad_points": [],
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
