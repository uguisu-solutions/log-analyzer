"""構成4 監視エージェント (FW / Routing / App)。

3 監視はモデル（Sonnet 4.5）と全体構造を共有し、System Prompt と
ツール呼び出しの観点だけが異なる。各監視は ``read_topology`` を 1 回叩いて
トポロジ情報を LLM のコンテキストに含める。
"""
from __future__ import annotations

import json
import os
import re
import time
from typing import Callable

import anthropic

from log_analyzer.rally._helpers import extract_json
from log_analyzer.rally.state import Config4State
from log_analyzer.rally.tools import read_topology

_VALID_ESCALATIONS = {"fw", "routing", "app"}


# 各監視のデフォルト System Prompt は公開シンボル（prompt_slots からも参照される）
FW_PROMPT = """\
あなたはファイアウォール監視エージェントです。
与えられたログとトポロジ情報から、FW レイヤの異常（policy / DENY / ACL）を検出し、
構造化 JSON で報告してください。

出力 (JSON のみ):
{
  "findings": [
    {"category": "FW|Net|App|DNS|Sec|Unknown", "summary": "...", "evidence": ["log line excerpt"]}
  ],
  "escalate_to": ["routing"],
  "tool_calls_made": ["read_topology(<ip>)"],
  "confidence": 0.0
}

ルール:
- findings は最大 3 件、確度の高い順
- escalate_to: 自レイヤの所見から、別の監視レイヤ (`routing` / `app`) で
  追加調査が必要だと判断した場合のみ列挙。なければ空配列
- 例: 「FW の DENY が連続 → 下流で upstream timeout の可能性 → routing を escalate」
- summary の自然文は日本語、フィールド名・enum 値は英語
- トポロジ情報を根拠に使う場合は evidence にトポロジ由来であることを明記
"""

ROUTING_PROMPT = """\
あなたはルーティング・接続性の監視エージェントです。
与えられたログとトポロジ情報から、L3-L4 の異常（タイムアウト / 再送 / 経路 / 帯域）を
検出し、構造化 JSON で報告してください。

出力 (JSON のみ):
{
  "findings": [
    {"category": "Net|FW|App|DNS|Sec|Unknown", "summary": "...", "evidence": ["log line excerpt"]}
  ],
  "escalate_to": ["fw"],
  "tool_calls_made": ["read_topology(<ip>)"],
  "confidence": 0.0
}

ルール:
- findings は最大 3 件、確度の高い順
- escalate_to: タイムアウトや再送の根本原因が FW のポリシー変更や App 側の応答遅延に
  起因する疑いがあれば、それぞれ `fw` / `app` を入れる。なければ空配列
- summary の自然文は日本語、フィールド名・enum 値は英語
"""

APP_PROMPT = """\
あなたはアプリケーション層の監視エージェントです。
与えられたログとトポロジ情報から、L7 の異常（5xx / プロセス / OOM / バックエンド応答）を
検出し、構造化 JSON で報告してください。

出力 (JSON のみ):
{
  "findings": [
    {"category": "App|Net|FW|DNS|Sec|Unknown", "summary": "...", "evidence": ["log line excerpt"]}
  ],
  "escalate_to": ["routing"],
  "tool_calls_made": ["read_topology(<ip>)"],
  "confidence": 0.0
}

ルール:
- findings は最大 3 件、確度の高い順
- escalate_to: 502/503 や upstream timeout から FW / routing 側に原因がありそうなら escalate
- summary の自然文は日本語、フィールド名・enum 値は英語
"""


def _extract_target_ip(log: str) -> str:
    """ログから「分析対象の宛先 IP」を雑に拾う。

    最初に見つかった ``dst=...`` を採用。見つからなければ ``"unknown"``。
    """
    match = re.search(r"dst=(\d+\.\d+\.\d+\.\d+)", log)
    return match.group(1) if match else "unknown"


def _build_user_input(log: str, topology: dict) -> str:
    return (
        f"## ログ\n{log}\n\n"
        f"## ツール read_topology の結果\n"
        f"{json.dumps(topology, ensure_ascii=False, indent=2)}\n"
    )


def _make_monitor(role: str, default_prompt: str) -> Callable[[Config4State], dict]:
    slot_id = f"{role}_monitor"

    def _monitor(state: Config4State) -> dict:
        p_overrides = state.get("prompt_overrides", {}) or {}
        m_overrides = state.get("model_overrides", {}) or {}
        model = m_overrides.get(slot_id) or os.environ.get("BASELINE_MODEL", "claude-sonnet-4-5")
        system_prompt = p_overrides.get(slot_id, default_prompt)
        log = state["log_text"]
        target_ip = _extract_target_ip(log)
        topology = read_topology(target_ip)
        user_input = _build_user_input(log, topology)

        client = anthropic.Anthropic()
        started = time.perf_counter()
        response = client.messages.create(
            model=model,
            max_tokens=1500,
            system=system_prompt,
            messages=[{"role": "user", "content": user_input}],
        )
        latency_ms = int((time.perf_counter() - started) * 1000)
        raw = response.content[0].text
        parsed = extract_json(raw)

        # 自分自身への escalate は意味が無いので除外
        escalate_to = [
            e for e in parsed.get("escalate_to", [])
            if e in _VALID_ESCALATIONS and e != role
        ]

        return {
            "monitor_results": {role: parsed},
            "escalations": escalate_to,
            "token_log": [
                {
                    "role": f"{role}_monitor",
                    "model": model,
                    "tokens_in": response.usage.input_tokens,
                    "tokens_out": response.usage.output_tokens,
                    "latency_ms": latency_ms,
                    "input": user_input[:2000],
                    "raw_output": raw,
                    "tool_target_ip": target_ip,
                    "round": state.get("rally_round", 1),
                }
            ],
        }

    _monitor.__name__ = f"{role}_monitor"
    return _monitor


fw_monitor = _make_monitor("fw", FW_PROMPT)
routing_monitor = _make_monitor("routing", ROUTING_PROMPT)
app_monitor = _make_monitor("app", APP_PROMPT)


MONITOR_FNS: dict[str, Callable[[Config4State], dict]] = {
    "fw": fw_monitor,
    "routing": routing_monitor,
    "app": app_monitor,
}

# prompt_slots からの参照用（slot_id → デフォルト System Prompt）
DEFAULT_MONITOR_PROMPTS: dict[str, str] = {
    "fw_monitor": FW_PROMPT,
    "routing_monitor": ROUTING_PROMPT,
    "app_monitor": APP_PROMPT,
}
