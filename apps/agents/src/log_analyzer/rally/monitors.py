"""構成4 監視エージェント (FW / Routing / App / DNS / Sec)。

5 監視はモデル（Sonnet 4.5）と全体構造を共有し、System Prompt と
ツール呼び出しの観点だけが異なる。各監視は ``read_topology`` と
``get_config`` を 1 回ずつ叩き、ネットワーク構成とサービス構成を
LLM のコンテキストに含める（議事録 L2: 構成図・コンフィグも見る設計）。

設計メモ（再入対応版）:
- 監視は ``escalate_to`` を返さない。判断はオーケストレータに集約済み。
- ``state["focus_hints"][role]`` がある場合、その自然文を user input に
  注入し、観点を絞った追加分析を行う。
"""
from __future__ import annotations

import json
import os
import re
import time
from typing import Callable

import anthropic

from log_analyzer.rally._helpers import safe_extract_json
from log_analyzer.rally.state import Config4State
from log_analyzer.rally.tools import extract_target_service, get_config, read_topology


# 各監視のデフォルト System Prompt は公開シンボル（prompt_slots からも参照される）
FW_PROMPT = """\
あなたはファイアウォール監視エージェントです。
与えられたログとトポロジ情報から、FW レイヤの異常（policy / DENY / ACL）を検出し、
構造化 JSON で報告してください。
オーケストレータから「観点指示」が与えられた場合は、その観点に沿って分析してください。

出力 (JSON のみ):
{
  "findings": [
    {"category": "FW|Net|App|DNS|Sec|Unknown", "summary": "...", "evidence": ["log line excerpt"]}
  ],
  "tool_calls_made": ["read_topology(<ip>)"],
  "confidence": 0.0
}

ルール:
- findings は最大 3 件、確度の高い順
- summary の自然文は日本語、フィールド名・enum 値は英語
- トポロジ情報を根拠に使う場合は evidence にトポロジ由来であることを明記
- 観点指示があった場合、その観点で検出できなければ findings を空配列にしてもよい（誇張しない）
"""

ROUTING_PROMPT = """\
あなたはルーティング・接続性の監視エージェントです。
与えられたログとトポロジ情報から、L3-L4 の異常（タイムアウト / 再送 / 経路 / 帯域）を
検出し、構造化 JSON で報告してください。
オーケストレータから「観点指示」が与えられた場合は、その観点に沿って分析してください。

出力 (JSON のみ):
{
  "findings": [
    {"category": "Net|FW|App|DNS|Sec|Unknown", "summary": "...", "evidence": ["log line excerpt"]}
  ],
  "tool_calls_made": ["read_topology(<ip>)"],
  "confidence": 0.0
}

ルール:
- findings は最大 3 件、確度の高い順
- summary の自然文は日本語、フィールド名・enum 値は英語
- 観点指示があった場合、その観点で検出できなければ findings を空配列にしてもよい
"""

APP_PROMPT = """\
あなたはアプリケーション層の監視エージェントです。
与えられたログとトポロジ情報、サービス設定から L7 の異常（5xx / プロセス / OOM / バックエンド応答）を
検出し、構造化 JSON で報告してください。
オーケストレータから「観点指示」が与えられた場合は、その観点に沿って分析してください。

出力 (JSON のみ):
{
  "findings": [
    {"category": "App|Net|FW|DNS|Sec|Unknown", "summary": "...", "evidence": ["log line excerpt"]}
  ],
  "tool_calls_made": ["read_topology(<ip>)", "get_config(<service>)"],
  "confidence": 0.0
}

ルール:
- findings は最大 3 件、確度の高い順
- summary の自然文は日本語、フィールド名・enum 値は英語
- サービス設定（max_heap_mb / known_issue / depends_on 等）が根拠なら evidence にその旨を併記
- 観点指示があった場合、その観点で検出できなければ findings を空配列にしてもよい
"""

DNS_PROMPT = """\
あなたは DNS の監視エージェントです。
与えられたログとトポロジ情報、サービス設定から DNS 解決の異常（SERVFAIL / NXDOMAIN /
ゾーン転送失敗 / 上流タイムアウト / 解決遅延）を検出し、構造化 JSON で報告してください。
オーケストレータから「観点指示」が与えられた場合は、その観点に沿って分析してください。

出力 (JSON のみ):
{
  "findings": [
    {"category": "DNS|Net|App|FW|Sec|Unknown", "summary": "...", "evidence": ["log line excerpt"]}
  ],
  "tool_calls_made": ["read_topology(<ip>)", "get_config(<service>)"],
  "confidence": 0.0
}

ルール:
- findings は最大 3 件、確度の高い順
- summary の自然文は日本語、フィールド名・enum 値は英語
- 解決失敗が App 層の 502 / 503 を誘発している場合は因果関係を summary に明示
- 観点指示があった場合、その観点で検出できなければ findings を空配列にしてもよい
"""

SEC_PROMPT = """\
あなたはセキュリティ監視エージェントです。
与えられたログとトポロジ情報、サービス設定から侵入・特権昇格・C2 通信・既知 IOC 接触などの
セキュリティ異常を検出し、構造化 JSON で報告してください。
オーケストレータから「観点指示」が与えられた場合は、その観点に沿って分析してください。

出力 (JSON のみ):
{
  "findings": [
    {"category": "Sec|FW|Net|App|DNS|Unknown", "summary": "...", "evidence": ["log line excerpt"]}
  ],
  "tool_calls_made": ["read_topology(<ip>)", "get_config(<service>)"],
  "confidence": 0.0
}

ルール:
- findings は最大 3 件、確度の高い順
- summary の自然文は日本語、フィールド名・enum 値は英語
- 推奨アクションが「アカウント無効化／プロセス kill／NW 隔離」等の取り返しがつかない操作の場合、
  上位の integrator で human_judgment_required=true が立つよう「人間判断必須相当」と summary に明示
- 観点指示があった場合、その観点で検出できなければ findings を空配列にしてもよい
"""


def _extract_target_ip(log: str) -> str:
    """ログから「分析対象の宛先 IP」を雑に拾う。

    最初に見つかった ``dst=...`` を採用。見つからなければ ``"unknown"``。
    """
    match = re.search(r"dst=(\d+\.\d+\.\d+\.\d+)", log)
    return match.group(1) if match else "unknown"


def _build_user_input(
    log: str, topology: dict, service_config: dict, focus_hint: str | None
) -> str:
    parts = [
        f"## ログ\n{log}",
        f"## ツール read_topology の結果\n"
        f"{json.dumps(topology, ensure_ascii=False, indent=2)}",
        f"## ツール get_config の結果\n"
        f"{json.dumps(service_config, ensure_ascii=False, indent=2)}",
    ]
    if focus_hint:
        parts.append(
            f"## 今ラウンドの観点指示（オーケストレータより）\n{focus_hint}"
        )
    return "\n\n".join(parts) + "\n"


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
        target_service = extract_target_service(log)
        service_config = get_config(target_service)
        focus_hint = (state.get("focus_hints") or {}).get(role)
        user_input = _build_user_input(log, topology, service_config, focus_hint)

        client = anthropic.Anthropic()
        started = time.perf_counter()
        response = client.messages.create(
            model=model,
            max_tokens=2000,
            system=system_prompt,
            messages=[{"role": "user", "content": user_input}],
        )
        latency_ms = int((time.perf_counter() - started) * 1000)
        raw = response.content[0].text
        parsed, parse_error = safe_extract_json(
            raw,
            fallback={"findings": [], "tool_calls_made": [], "confidence": 0.0},
        )
        if parse_error:
            parsed["_parse_error"] = parse_error

        return {
            "monitor_results": {role: parsed},
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
                    "tool_target_service": target_service,
                    "round": state.get("rally_round", 1),
                    "focus_hint": focus_hint,
                }
            ],
        }

    _monitor.__name__ = f"{role}_monitor"
    return _monitor


fw_monitor = _make_monitor("fw", FW_PROMPT)
routing_monitor = _make_monitor("routing", ROUTING_PROMPT)
app_monitor = _make_monitor("app", APP_PROMPT)
dns_monitor = _make_monitor("dns", DNS_PROMPT)
sec_monitor = _make_monitor("sec", SEC_PROMPT)


MONITOR_FNS: dict[str, Callable[[Config4State], dict]] = {
    "fw": fw_monitor,
    "routing": routing_monitor,
    "app": app_monitor,
    "dns": dns_monitor,
    "sec": sec_monitor,
}

# prompt_slots からの参照用（slot_id → デフォルト System Prompt）
DEFAULT_MONITOR_PROMPTS: dict[str, str] = {
    "fw_monitor": FW_PROMPT,
    "routing_monitor": ROUTING_PROMPT,
    "app_monitor": APP_PROMPT,
    "dns_monitor": DNS_PROMPT,
    "sec_monitor": SEC_PROMPT,
}
