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

import json
import os
import time

import anthropic

from log_analyzer.rally._helpers import safe_extract_json
from log_analyzer.rally.state import Config4State

# config4 のロール別デフォルトモデル。
# orchestrator は「ログから初手を 1 つ選ぶ」分類タスクなので Haiku で十分速く・安く処理できる。
# 個別に上書きしたい場合は環境変数 RALLY_ORCHESTRATOR_MODEL で変更可能。
_DEFAULT_ORCHESTRATOR_MODEL = "claude-haiku-4-5"

ORCHESTRATOR_PROMPT = """\
あなたはネットワーク／システムインフラのトリアージ・オーケストレータです。
ログを読み、**最初に呼ぶべき監視エージェントを 1 つだけ** 選んでください。
以降の遷移は各監視自身が判断するため、あなたが呼ばれるのはこの初回 1 回だけです。

監視エージェント (5 種類):
- fw: ファイアウォール関連（policy / DENY / ACL の異常）
- routing: ルーティング・接続性関連（タイムアウト / 経路 / TCP 再送 / 帯域）
- app: アプリケーション層（5xx / プロセスエラー / OOM / 502 bad gateway）
- dns: DNS 解決の異常（SERVFAIL / NXDOMAIN / ゾーン転送失敗 / 上流タイムアウト）
- sec: セキュリティ（侵入 / 特権昇格 / C2 通信 / 既知 IOC 接触）

判断ルール:
- ログの語彙（kernel/iptables→fw, named/SERVFAIL→dns, sshd/sudo→sec 等）から
  最も関連性の高い 1 つを選ぶ
- ログから決め手がない場合は "fw" を選ぶ（最も汎用的に当てやすい）
- 初手で複数のレイヤを並列に当てたい場合でも、ここでは 1 つだけ選ぶこと
  （後段の監視が自分で次のノードを呼べる）

出力 (JSON のみ、コードフェンス不要):
{
  "first_node": "fw",
  "focus_hint": "DENY が連続している policy ID 別に集計して報告",
  "rationale": "kernel iptables の DENY が大量に出ているため、まず FW を当てる"
}

ルール:
- first_node は ["fw", "routing", "app", "dns", "sec"] のいずれか
- focus_hint は最初の監視への観点指示（空文字でも可）
- rationale は短い日本語で
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
    return {
        **decision,
        "model": model,
        "tokens_in": response.usage.input_tokens,
        "tokens_out": response.usage.output_tokens,
        "latency_ms": latency_ms,
        "raw_output": raw,
        "user_input": user_input,
        "parse_error": parse_error,
    }
