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
from datetime import datetime, timezone

import anthropic

from log_analyzer.rally._helpers import safe_extract_json
from log_analyzer.tracing import usage_components

# 方針プランナーのデフォルトモデル。config-log 解析の評価方針 (2026-06) に合わせ
# Claude 系は Opus に統一。RALLY_PLANNER_MODEL で個別に上書き可能。
_DEFAULT_PLANNER_MODEL = "claude-opus-4-7"

# 方針 JSON の出力上限。複雑なインシデントでは situation_summary + 想定原因 +
# 多段の investigation_plan で出力が伸び、1500 では途中切断 → JSON パース失敗 →
# 既定フォールバックが多発する。余裕を持たせる (RALLY_PLANNER_MAX_TOKENS で調整可)。
_DEFAULT_PLANNER_MAX_TOKENS = 4000


def _planner_max_tokens() -> int:
    raw = os.environ.get("RALLY_PLANNER_MAX_TOKENS")
    if raw:
        try:
            return int(raw)
        except ValueError:
            pass
    return _DEFAULT_PLANNER_MAX_TOKENS

VALID_FIRST_NODES: set[str] = {"fw", "routing", "app", "dns", "sec"}

PLANNER_PROMPT = """\
あなたはネットワーク／システムインフラ障害解析の方針プランナーです。
この解析の依頼者はジュニアエンジニアです。問診票の記載は「訓練途上の担当者による
観測の申告」であり、貴重な一次情報ですが、構成図・ログ・設定という機械データと
突き合わせて初めて事実として扱えます。

与えられた情報（ネットワーク構成図(Mermaid) / 各機器のログ・設定 / 問診票）を読み、
これから行う障害解析の方針を立て、人間の確認を得るために提案してください。
あなたは原因を断定するのではなく、「何を裏取りし、どこから・どの順で・何に着目して
調べるか」の当たり（方針）を示すのが役割です。

方針立案の手順（必ずこの順で行うこと）:

1. 申告の突合（デスクチェック）:
   問診票の記載（事象 / いつから / 変わったこと / 効く・効かないの対 / 確認OK /
   未確認 / 見立て / 打ち手 / 迷い）を、手元の構成図・ログ・設定と突き合わせる。
   - 申告どおりの痕跡が機械データで確認できたもの → 確認済みの事実として方針の土台にする
   - 機械データと食い違う申告、または申告内部で矛盾する記載 → そのまま採用せず、
     最優先の確認対象として investigation_plan の先頭に置く
   - 手元のデータでは裏取りできない申告（例: 本文が BigQuery にあるログ、未提供の機器設定）
     → 裏取りの手段を investigation_plan に書き、提供自体が不足しているものは
     missing_data_notes に書く

2. 影響範囲の構成図上の特定:
   申告された「影響あり／なし」の対（例: 無線NG/有線OK、特定フロアのみ、特定設定のPCのみ）を
   構成図上の経路・機器に写像し、症状がどのレイヤ・どの経路で起きていそうかを推定する。
   写像できない場合（該当機器が構成図にない等）は、その旨を missing_data_notes に書く。

3. 仮説と調査順序:
   1・2 の結果を踏まえて原因の方向性を挙げ、調査の起点と順序を決める。
   検証結果がどちらに出ても仮説を大きく絞れる確認を先に置くこと。
   問診票に「いま一番迷っていること」の記載があれば、解析の終点でその迷いに回答できるよう、
   回答に必要な確認を investigation_plan に含めること。

利用できる監視エージェント (5 種類):
- fw: ファイアウォール（policy / DENY / ACL）
- routing: ルーティング・接続性（タイムアウト / 経路 / TCP 再送 / 帯域）
- app: アプリケーション層（5xx / プロセス / OOM / 502）
- dns: DNS 解決（SERVFAIL / NXDOMAIN / ゾーン転送 / 上流タイムアウト）
- sec: セキュリティ（侵入 / 特権昇格 / C2 / 既知 IOC）

BigQuery 取得を含む方針を立てる場合の注意 (重要):
- ログ取得元が BigQuery のノードについては、**的を絞った最小限の取得**を方針に書くこと。
  「期間横断で全件取得」「全ポートを総当たりで時系列集計」のような広範・大量取得は避ける。
- 具体的には: 疑わしい時間帯に期間を絞る / キーワード (contains) で本文を絞る /
  対象を最も疑わしい 1〜数ホスト・ポートに限定する、を investigation_plan に明記する。
- 取得は「まず少量で当たりを確認し、必要なら絞って追加取得する」段階的な方針とする
  (一度に大量取得させない)。

出力 (JSON のみ、コードフェンス不要):
{
  "situation_summary": "現象を 1-2 文で要約 (日本語)。申告と機械データの突合結果（一致 / 不一致 / 未裏取り）を含めること",
  "primary_hypotheses": ["想定される原因の方向性 (日本語、最大 3 件)。各件、どの確認済み事実に支えられ、何が未裏取りかを一言添える"],
  "investigation_plan": ["調査の起点と順序の方針 (日本語、ステップごと)。先頭は原則、突合で残った最大の未確認・矛盾の検証とする"],
  "suggested_first_node": "routing",
  "focus": "最初の監視に当てる観点 (日本語)。問診票の迷いを引き継ぐ場合はここに含める",
  "data_to_use": ["解析に使う入力データ (例: fw-01 のログ, 問診票の事象)"],
  "missing_data_notes": "不足データや前提 (なければ空文字)"
}

ルール:
- suggested_first_node は ["fw", "routing", "app", "dns", "sec"] のいずれか
- 決め手が薄い場合は、固定のデフォルトに逃げないこと。突合で残った最大の未確認事項を
  最初に検証できる監視を選び、決め手が薄いこと自体を focus に明記する
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

    Langfuse への記録用に ``started_at`` / ``ended_at`` (UTC datetime) と
    ``user_input`` も返す (確認事項 B-1)。プランナーは rally のトレース生成より
    前に走るため、ここでは送信せず、呼び出し側が trace_id 確定後に
    Generation として記録する。これらは保存対象の proposal からは除外される。
    """
    chosen_model = model or os.environ.get("RALLY_PLANNER_MODEL", _DEFAULT_PLANNER_MODEL)
    sys_prompt = (system_prompt or "").strip() or PLANNER_PROMPT
    user_input = f"## 与えられた情報（構成図 / ログ / 設定 / 問診票）\n{log_text}\n"
    max_toks = _planner_max_tokens()
    system = [{"type": "text", "text": sys_prompt, "cache_control": {"type": "ephemeral"}}]
    _FALLBACK = {
        "situation_summary": "方針 JSON の解析に失敗しました。既定方針 (fw 起点) で進めます。",
        "primary_hypotheses": [],
        "investigation_plan": [],
        "suggested_first_node": "fw",
        "focus": "",
        "data_to_use": [],
        "missing_data_notes": "",
    }

    client = anthropic.Anthropic()
    messages: list[dict] = [{"role": "user", "content": user_input}]
    total_in = 0
    total_out = 0
    started = time.perf_counter()
    started_at = datetime.now(timezone.utc)

    response = client.messages.create(
        model=chosen_model, max_tokens=max_toks, system=system, messages=messages
    )
    raw = response.content[0].text
    uc = usage_components(response.usage)
    total_in += uc["input"] + uc["cache_creation"] + uc["cache_read"]
    total_out += uc["output"]
    raw_proposal, parse_error = safe_extract_json(raw, fallback=_FALLBACK)

    # パース失敗（多くは max_tokens 切断 or 散文混入）→ JSON のみ・簡潔に 1 回だけ再生成
    if parse_error:
        messages.append({"role": "assistant", "content": raw or "(空応答)"})
        messages.append({
            "role": "user",
            "content": (
                "前の応答から JSON を抽出できませんでした（途中で切れた可能性があります）。"
                "primary_hypotheses は最大3件、investigation_plan は最大5件に抑え、"
                "situation_summary / primary_hypotheses / investigation_plan / "
                "suggested_first_node / focus / data_to_use / missing_data_notes だけを、"
                "前置き・説明文・コードフェンスを一切付けず JSON オブジェクトのみで簡潔に出力してください。"
            ),
        })
        try:
            retry = client.messages.create(
                model=chosen_model, max_tokens=max_toks, system=system, messages=messages
            )
            rtext = retry.content[0].text
            ruc = usage_components(retry.usage)
            total_in += ruc["input"] + ruc["cache_creation"] + ruc["cache_read"]
            total_out += ruc["output"]
            retry_proposal, retry_err = safe_extract_json(rtext, fallback=_FALLBACK)
            if retry_err is None:
                raw_proposal, parse_error, raw = retry_proposal, None, rtext
        except Exception:  # noqa: BLE001 — 再試行失敗時は初回フォールバックのまま継続
            pass

    latency_ms = int((time.perf_counter() - started) * 1000)
    proposal = _normalize_proposal(raw_proposal)
    proposal.update(
        {
            "model": chosen_model,
            "tokens_in": total_in,
            "tokens_out": total_out,
            "latency_ms": latency_ms,
            "raw_output": raw,
            "parse_error": parse_error,
            # Langfuse 記録用 (確認事項 B-1)。保存対象の proposal からは除外される。
            "started_at": started_at,
            "ended_at": datetime.now(timezone.utc),
            "user_input": user_input,
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
