"""構成4 解析方針プランナー (Phase 2・解析前に 1 回だけ実行)。

役割:
    解析を始める前に、与えられた全入力（mermaid 構成図 / ログ / 設定 / 問診票が
    合成された log_text）を読み、**障害解析の方針**を提案する。
    提案はユーザーに提示し、承認を得てから本解析 (orchestrator → 監視 → integrator)
    に進む。orchestrator が「最初の 1 監視」を選ぶより前段の、人間向け方針合意ステップ。

出力 JSON:
    {
      "situation_summary": "現象の要約",
      "primary_hypotheses": ["想定される原因の方向性", ...],
      "investigation_plan": ["調査の起点と順序の方針", ...],
      "suggested_first_node": "fw|routing|app|dns|sec",
      "focus": "最初に当てる観点",
      "data_to_use": ["解析に使う入力データ", ...],
      "missing_data_notes": "不足データ・前提 (なければ空文字)"
    }

本モジュールは LLM を 1 回呼ぶだけで、状態 (Config4State) は変更しない。
"""
from __future__ import annotations

import os
import time

import anthropic

from log_analyzer.rally._helpers import safe_extract_json
from log_analyzer.tracing import usage_components

# 方針プランナーのデフォルトモデル。config-log 解析の評価方針 (2026-06) に合わせ
# Claude 系は Opus に統一。RALLY_PLANNER_MODEL で個別に上書き可能。
_DEFAULT_PLANNER_MODEL = "claude-opus-4-7"

VALID_FIRST_NODES: set[str] = {"fw", "routing", "app", "dns", "sec"}

PLANNER_PROMPT = """\
あなたはネットワーク／システムインフラ障害解析の方針プランナーです。
与えられた情報（ネットワーク構成図(Mermaid) / 各機器のログ・設定 / 問診票の事象）を読み、
これから行う障害解析の**方針**を立て、人間の確認を得るために提案してください。
あなたは原因を断定するのではなく、「どこから・どの順で・何に着目して調べるか」の
当たり（方針）を示すのが役割です。

利用できる監視エージェント (5 種類):
- fw: ファイアウォール（policy / DENY / ACL）
- routing: ルーティング・接続性（タイムアウト / 経路 / TCP 再送 / 帯域）
- app: アプリケーション層（5xx / プロセス / OOM / 502）
- dns: DNS 解決（SERVFAIL / NXDOMAIN / ゾーン転送 / 上流タイムアウト）
- sec: セキュリティ（侵入 / 特権昇格 / C2 / 既知 IOC）

方針立案の観点:
- 問診票の「事象」と構成図から、症状がどのレイヤ・どの経路で起きていそうかを推定する
- 提供された各機器のログ／設定のうち、どれが手がかりになりそうかを挙げる
- 最初に当てるべき監視 (suggested_first_node) と、その観点 (focus) を 1 つ決める
- 解析に必要なのに不足しているデータがあれば missing_data_notes に書く

BigQuery 取得を含む方針を立てる場合の注意 (重要):
- ログ取得元が BigQuery のノードについては、**的を絞った最小限の取得**を方針に書くこと。
  「期間横断で全件取得」「全ポートを総当たりで時系列集計」のような広範・大量取得は避ける。
- 具体的には: 疑わしい時間帯に期間を絞る / キーワード (contains) で本文を絞る /
  対象を最も疑わしい 1〜数ホスト・ポートに限定する、を investigation_plan に明記する。
- 取得は「まず少量で当たりを確認し、必要なら絞って追加取得する」段階的な方針とする
  (一度に大量取得させない)。

出力 (JSON のみ、コードフェンス不要):
{
  "situation_summary": "現象を 1-2 文で要約 (日本語)",
  "primary_hypotheses": ["想定される原因の方向性 (日本語、最大 3 件)"],
  "investigation_plan": ["調査の起点と順序の方針 (日本語、ステップごと)"],
  "suggested_first_node": "fw",
  "focus": "最初の監視に当てる観点 (日本語)",
  "data_to_use": ["解析に使う入力データ (例: fw-01 のログ, 問診票の事象)"],
  "missing_data_notes": "不足データや前提 (なければ空文字)"
}

ルール:
- suggested_first_node は ["fw", "routing", "app", "dns", "sec"] のいずれか
- 決め手が薄い場合は最も汎用的に当てやすい "fw" を選ぶ
- 自然文 (summary / hypotheses / plan 等) は日本語
- 出力は JSON のみ。前置き・説明文・コードフェンスを付けない
"""


def _normalize_proposal(raw: dict) -> dict:
    """LLM 出力を方針提案スキーマに正規化する。"""
    def _str_list(v) -> list[str]:
        if not isinstance(v, list):
            return []
        return [str(x).strip() for x in v if str(x).strip()]

    first = raw.get("suggested_first_node")
    if first not in VALID_FIRST_NODES:
        first = "fw"  # 安全なフォールバック
    return {
        "situation_summary": str(raw.get("situation_summary", "") or ""),
        "primary_hypotheses": _str_list(raw.get("primary_hypotheses")),
        "investigation_plan": _str_list(raw.get("investigation_plan")),
        "suggested_first_node": first,
        "focus": str(raw.get("focus", "") or ""),
        "data_to_use": _str_list(raw.get("data_to_use")),
        "missing_data_notes": str(raw.get("missing_data_notes", "") or ""),
    }


def plan_policy(
    log_text: str,
    *,
    model: str | None = None,
    system_prompt: str | None = None,
) -> dict:
    """解析方針を 1 回だけ提案する。同期実行。

    返り値: 正規化済み proposal に計測 (model / tokens_in / tokens_out / latency_ms /
    raw_output / parse_error) を加えた dict。
    """
    chosen_model = model or os.environ.get("RALLY_PLANNER_MODEL", _DEFAULT_PLANNER_MODEL)
    sys_prompt = (system_prompt or "").strip() or PLANNER_PROMPT
    user_input = f"## 与えられた情報（構成図 / ログ / 設定 / 問診票）\n{log_text}\n"

    client = anthropic.Anthropic()
    started = time.perf_counter()
    response = client.messages.create(
        model=chosen_model,
        max_tokens=1500,
        system=[
            {
                "type": "text",
                "text": sys_prompt,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        messages=[{"role": "user", "content": user_input}],
    )
    latency_ms = int((time.perf_counter() - started) * 1000)
    raw = response.content[0].text
    raw_proposal, parse_error = safe_extract_json(
        raw,
        fallback={
            "situation_summary": "方針 JSON の解析に失敗しました。既定方針 (fw 起点) で進めます。",
            "primary_hypotheses": [],
            "investigation_plan": [],
            "suggested_first_node": "fw",
            "focus": "",
            "data_to_use": [],
            "missing_data_notes": "",
        },
    )
    proposal = _normalize_proposal(raw_proposal)
    uc = usage_components(response.usage)
    proposal.update(
        {
            "model": chosen_model,
            "tokens_in": uc["input"] + uc["cache_creation"] + uc["cache_read"],
            "tokens_out": uc["output"],
            "latency_ms": latency_ms,
            "raw_output": raw,
            "parse_error": parse_error,
        }
    )
    return proposal


def build_approved_policy_block(proposal: dict, edited_focus: str | None = None) -> str:
    """承認された方針を log_text 先頭に差し込むテキストブロックに整形する。

    ``edited_focus`` が与えられればユーザーが修正した観点で focus を上書きする。
    """
    focus = (edited_focus or "").strip() or str(proposal.get("focus", "") or "")
    lines: list[str] = ["## 承認済み解析方針（ユーザー確認済み）"]
    summary = str(proposal.get("situation_summary", "") or "").strip()
    if summary:
        lines.append(f"- 現象: {summary}")
    hyps = [str(x) for x in (proposal.get("primary_hypotheses") or []) if str(x).strip()]
    if hyps:
        lines.append("- 想定原因: " + " / ".join(hyps))
    plan = [str(x) for x in (proposal.get("investigation_plan") or []) if str(x).strip()]
    if plan:
        lines.append("- 調査方針:")
        for i, p in enumerate(plan, 1):
            lines.append(f"    {i}. {p}")
    if focus:
        lines.append(f"- 着目観点: {focus}")
    lines.append("この方針はユーザーが確認・承認済みです。これに沿って解析を進めてください"
                 "（新たな証拠があれば方針を更新して構いません）。")
    lines.append("")
    return "\n".join(lines) + "\n"
