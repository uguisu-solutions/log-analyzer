"""構成4 オーケストレータ (委譲チェーン型・初回 1 回のみ実行)。

役割:
    ログを薄く読み、最初に呼ぶ監視を **1 つだけ** 選ぶ。
    以降の遷移判断は各監視自身が行う（monitors.py 参照）。

出力 JSON:
    {"first_node": "fw|routing|app|dns|sec",
     "focus_hint": "最初の監視に渡す観点指示 (任意)",
     "rationale": "..."}

無効な first_node を返した場合は ``"fw"`` にフォールバックする
（5 監視の中で最も汎用的に当てやすい）。
"""
from __future__ import annotations

import os
import time

import anthropic

from log_analyzer.rally._helpers import safe_extract_json
from log_analyzer.rally.state import Config4State
from log_analyzer.tracing import usage_components

# config4 のロール別デフォルトモデル。
# config-log 解析の評価方針 (2026-06) で Claude 系ノードは Opus に統一。
# 個別に上書きしたい場合は環境変数 RALLY_ORCHESTRATOR_MODEL で変更可能。
_DEFAULT_ORCHESTRATOR_MODEL = "claude-opus-4-7"

ORCHESTRATOR_PROMPT = """\
あなたはネットワーク／システムインフラの障害切り分けにおける初動トリアージ担当です。
この一連の解析の依頼者と読み手はジュニアエンジニアであり、目的は正解の言い当てではなく、
事実確認と仮説の絞り込みによって、考察を正しい方向へ進める支援です。

システムの仕様上、あなたは初回に 1 回だけ呼ばれ、最初に解析を担当する監視エージェントを
1 つ選びます。以降の遷移は各監視自身が判断します。

監視エージェント (5 種類):
- fw: ファイアウォール関連（policy / DENY / ACL の異常）
- routing: ルーティング・接続性関連（タイムアウト / 経路 / TCP 再送 / 帯域）
- app: アプリケーション層（5xx / プロセスエラー / OOM / 502 bad gateway）
- dns: DNS 解決の異常（SERVFAIL / NXDOMAIN / ゾーン転送失敗 / 上流タイムアウト）
- sec: セキュリティ（侵入 / 特権昇格 / C2 通信 / 既知 IOC 接触）

判断の根拠（優先順）:
1. 方針プランナーの提案（investigation_plan / suggested_first_node / focus を含む方針）が
   入力に含まれる場合、それは人間の確認を経た方針であるため、first_node と focus_hint は
   原則としてこれを引き継ぐ。手元のログの実態が方針と明確に食い違う場合のみ変更してよいが、
   その場合は変更理由と食い違いの根拠を rationale に明記すること。
2. 方針が含まれない場合、問診票の記載（あれば）とログの語彙
   （kernel/iptables→fw, named/SERVFAIL→dns, sshd/sudo→sec 等）から選ぶ。
   ただし問診票の記載はジュニアエンジニアによる未裏取りの申告であり、
   あなたの初手選択はその申告に基づく暫定判断であることを自覚すること。
3. 決め手がない場合は、固定のデフォルトに逃げず、並立している仮説群を最も効率よく
   二分できる（＝検証結果がどちらに出ても仮説を大きく絞れる）監視を選び、
   「決め手がなかった」事実を rationale に明記する。

focus_hint には次を含めること:
- 最初の監視に見てほしい観点（方針プランナーの focus があればそれを引き継ぐ）
- 最初の監視への指示として「まず、問診票の申告のうち自レイヤに関係するものを
  ログ・Config・トポロジと突合し、一致／不一致を確認してから異常検出に入ること」
- 問診票③「いま一番迷っていること」の記載があれば、その内容
  （これは相談の核であり、最終統合まで引き継がれ、応答されるべき問いである）
- 問診票②「まだ確認していないこと」のうち、その監視で検証可能なもの

出力 (JSON のみ、コードフェンス不要):
{
  "first_node": "app",
  "focus_hint": "...",
  "rationale": "..."
}

ルール:
- first_node は ["fw", "routing", "app", "dns", "sec"] のいずれか
- focus_hint は最初の監視への観点指示（空文字でも可）
- rationale は短い日本語で。選定根拠とした事実（方針・問診票のどの欄か・どのログ行か）を
  挙げること
"""

VALID_MONITORS: set[str] = {"fw", "routing", "app", "dns", "sec"}


def _build_user_input(state: Config4State) -> str:
    return f"## ログ\n{state['log_text']}\n"


def _normalize_decision(raw: dict) -> dict:
    first = raw.get("first_node")
    if first not in VALID_MONITORS:
        first = "fw"  # 安全なフォールバック
    return {
        "first_node": first,
        "focus_hint": str(raw.get("focus_hint", "") or ""),
        "rationale": str(raw.get("rationale", "") or ""),
    }


def orchestrator_select_first(state: Config4State) -> dict:
    """初回 1 回のみ呼び出される。最初に実行する監視名を返す。

    返り値は ``{"first_node", "focus_hint", "rationale", "model", "tokens_in",
    "tokens_out", "latency_ms", "raw_output", "parse_error"}``。
    """
    p_overrides = state.get("prompt_overrides", {}) or {}
    m_overrides = state.get("model_overrides", {}) or {}
    model = m_overrides.get("orchestrator") or os.environ.get(
        "RALLY_ORCHESTRATOR_MODEL", _DEFAULT_ORCHESTRATOR_MODEL
    )
    system_prompt = p_overrides.get("orchestrator", ORCHESTRATOR_PROMPT)

    user_input = _build_user_input(state)
    client = anthropic.Anthropic()
    started = time.perf_counter()
    # system プロンプトに ephemeral キャッシュを設定。同一ログの再実行 / 同一プロンプトの
    # 連続テストで 2 回目以降の入力 token を大幅削減する。
    response = client.messages.create(
        model=model,
        max_tokens=600,
        system=[
            {
                "type": "text",
                "text": system_prompt,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        messages=[{"role": "user", "content": user_input}],
    )
    latency_ms = int((time.perf_counter() - started) * 1000)
    raw = response.content[0].text
    raw_decision, parse_error = safe_extract_json(
        raw,
        fallback={
            "first_node": "fw",
            "focus_hint": "",
            "rationale": "orchestrator JSON parse 失敗のため fw にフォールバック",
        },
    )
    decision = _normalize_decision(raw_decision)
    uc = usage_components(response.usage)
    return {
        **decision,
        "model": model,
        # tokens_in は cache 書込/読出を含む入力処理トークン総量
        "tokens_in": uc["input"] + uc["cache_creation"] + uc["cache_read"],
        "tokens_out": uc["output"],
        "cache_creation": uc["cache_creation"],
        "cache_read": uc["cache_read"],
        "latency_ms": latency_ms,
        "raw_output": raw,
        "user_input": user_input,
        "parse_error": parse_error,
    }
