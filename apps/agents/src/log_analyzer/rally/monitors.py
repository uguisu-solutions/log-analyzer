"""構成4 監視エージェント (委譲チェーン型)。

5 監視 (FW / Routing / App / DNS / Sec) はモデルと全体構造を共有し、
System Prompt と観点だけが異なる。各監視は ``read_topology`` / ``get_config``
を 1 回ずつ叩いてからログを分析し、findings を返すと同時に
**次に処理を委譲するノード** を 1 つ指名する。

監視の出力 JSON:
    {
      "findings": [...],
      "tool_calls_made": [...],
      "confidence": 0.0,
      "next": "fw|routing|app|dns|sec|integrator",
      "focus_hint_for_next": "次ノードに渡す観点指示 (next が integrator なら空)",
      "rationale": "なぜこの next を選んだか"
    }

遷移制約 (rally_agent 側で検証):
    - 自己遷移禁止 (例: fw → fw)
    - 直前ノードへの遷移禁止 (即時 ping-pong 防止)
    - 違反時は強制的に integrator にフォールバック
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

VALID_NEXT_NODES: set[str] = {"fw", "routing", "app", "dns", "sec", "integrator"}

# config4 監視のデフォルトモデル。
# config-log 解析の評価方針 (2026-06) で Claude 系ノードは Opus に統一。
# RALLY_MONITOR_MODEL で個別に上書き可能。
_DEFAULT_MONITOR_MODEL = "claude-opus-4-7"


# ─── System Prompts ──────────────────────────────────────────────────

_NEXT_NODE_GUIDANCE = """\

委譲ルール（重要）:
- あなたは自分の観点で分析を終えたら、必ず "next" に次のノードを指名すること
- 候補: "fw" / "routing" / "app" / "dns" / "sec" / "integrator"
- "integrator" を選ぶと最終統合に進む（ラリー終了）
- 自分自身への遷移は禁止（即 ping-pong 防止のため）
- 直前に処理したノードへの遷移も禁止（rally_agent が自動的に integrator にフォールバックする）
- 既存の所見と矛盾しない、別観点で深掘りが必要な監視を選ぶ
- 主要原因が裏付けられた・追加調査の価値が低い → "integrator" を選ぶ
- 「解析中に追加投入されたログ」が user 入力にあれば、それも分析と次ノード判断に踏まえる
- "focus_hint_for_next" は次ノードに渡す観点（next=integrator なら空文字）

出力 (JSON のみ、コードフェンス不要):
{
  "findings": [
    {"category": "FW|Net|App|DNS|Sec|Unknown", "summary": "...", "evidence": ["..."]}
  ],
  "tool_calls_made": ["read_topology(<ip>)", "get_config(<service>)"],
  "confidence": 0.0,
  "next": "routing",
  "focus_hint_for_next": "FW で DENY を検出した宛先 IP の経路 / 再送状況を調べてほしい",
  "rationale": "FW 側で DENY は確認できたので、影響範囲を Routing で裏取りする"
}

ルール:
- findings は最大 3 件、確度の高い順
- summary の自然文は日本語、フィールド名・enum 値は英語
"""

FW_PROMPT = (
    """\
あなたはファイアウォール監視エージェントです。
与えられたログとトポロジ情報から、FW レイヤの異常（policy / DENY / ACL）を検出し、
構造化 JSON で報告してください。
オーケストレータまたは前段の監視から「観点指示」が与えられた場合は、その観点に沿って分析してください。
"""
    + _NEXT_NODE_GUIDANCE
)

ROUTING_PROMPT = (
    """\
あなたはルーティング・接続性の監視エージェントです。
与えられたログとトポロジ情報から、L3-L4 の異常（タイムアウト / 再送 / 経路 / 帯域）を
検出し、構造化 JSON で報告してください。
オーケストレータまたは前段の監視から「観点指示」が与えられた場合は、その観点に沿って分析してください。
"""
    + _NEXT_NODE_GUIDANCE
)

APP_PROMPT = (
    """\
あなたはアプリケーション層の監視エージェントです。
与えられたログとトポロジ情報、サービス設定から L7 の異常（5xx / プロセス / OOM / バックエンド応答）を
検出し、構造化 JSON で報告してください。
オーケストレータまたは前段の監視から「観点指示」が与えられた場合は、その観点に沿って分析してください。
"""
    + _NEXT_NODE_GUIDANCE
)

DNS_PROMPT = (
    """\
あなたは DNS の監視エージェントです。
与えられたログとトポロジ情報、サービス設定から DNS 解決の異常（SERVFAIL / NXDOMAIN /
ゾーン転送失敗 / 上流タイムアウト / 解決遅延）を検出し、構造化 JSON で報告してください。
オーケストレータまたは前段の監視から「観点指示」が与えられた場合は、その観点に沿って分析してください。
"""
    + _NEXT_NODE_GUIDANCE
)

SEC_PROMPT = (
    """\
あなたはセキュリティ監視エージェントです。
与えられたログとトポロジ情報、サービス設定から侵入・特権昇格・C2 通信・既知 IOC 接触などの
セキュリティ異常を検出し、構造化 JSON で報告してください。
オーケストレータまたは前段の監視から「観点指示」が与えられた場合は、その観点に沿って分析してください。

特記事項:
- 推奨アクションが「アカウント無効化／プロセス kill／NW 隔離」等の取り返しがつかない操作の場合、
  上位の integrator で human_judgment_required=true が立つよう「人間判断必須相当」と summary に明示
"""
    + _NEXT_NODE_GUIDANCE
)


def _extract_target_ip(log: str) -> str:
    """ログから「分析対象の宛先 IP」を雑に拾う。"""
    match = re.search(r"dst=(\d+\.\d+\.\d+\.\d+)", log)
    return match.group(1) if match else "unknown"


def _build_user_blocks(
    log: str,
    topology: dict,
    service_config: dict,
    focus_hint: str | None,
    previous_node: str | None,
    monitor_results: dict,
    appended_logs: list[dict] | None = None,
) -> list[dict]:
    """user メッセージを安定部分 + 動的部分の 2 ブロックに分けて返す。

    安定部分（元 log + tool 結果）に ``cache_control`` を立てる。元 log と tool
    結果は実行中変化しないので、同じ監視を委譲チェーン内で複数回呼ぶ場合 /
    同じログに対する別構成での再実行でキャッシュヒットが発生する。

    実行中にユーザーが投入した追加ログは **動的ブロック側** に入れることで
    キャッシュ無効化を避けつつ、以降の監視へ確実に伝える。
    """
    stable = (
        f"## ログ\n{log}\n\n"
        f"## ツール read_topology の結果\n"
        f"{json.dumps(topology, ensure_ascii=False, indent=2)}\n\n"
        f"## ツール get_config の結果\n"
        f"{json.dumps(service_config, ensure_ascii=False, indent=2)}\n"
    )
    dynamic_parts: list[str] = []
    if appended_logs:
        appended_text = "\n\n".join(
            f"### 追加ログ #{i + 1} (round {a.get('round_added', '?')} で投入、source={a.get('source', '?')})\n"
            f"{a.get('content', '')}"
            for i, a in enumerate(appended_logs)
        )
        dynamic_parts.append(
            "## 解析中に追加投入されたログ\n"
            "以下は実行中にユーザーが追加で渡したログです。元ログと合わせて分析し、"
            "次ノード判断にも反映してください。\n\n"
            + appended_text
        )
    if focus_hint:
        dynamic_parts.append(f"## 今ラウンドの観点指示\n{focus_hint}")
    if previous_node:
        dynamic_parts.append(
            f"## 委譲元\n直前に処理したのは {previous_node} です。"
            f"そのノードへの遷移は禁止されています（即 ping-pong 防止）。"
        )
    if monitor_results:
        dynamic_parts.append(
            "## 過去の監視結果（参考）\n"
            + json.dumps(monitor_results, ensure_ascii=False, indent=2)
        )
    dynamic = "\n\n".join(dynamic_parts) if dynamic_parts else "（動的な観点指示なし）"

    return [
        {"type": "text", "text": stable, "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": dynamic},
    ]


def _blocks_to_text(blocks: list[dict]) -> str:
    """token_log への保存用にブロック群を 1 つの文字列に連結。"""
    return "\n\n".join(b.get("text", "") for b in blocks)


def _normalize_monitor_output(
    raw: dict,
    role: str,
    previous_node: str | None,
) -> tuple[dict, str | None]:
    """LLM 出力を正規化し、遷移制約を検証。違反時は integrator にフォールバック。

    返り値: (正規化された出力, 違反理由 or None)
    """
    next_node = raw.get("next")
    violation: str | None = None
    if next_node not in VALID_NEXT_NODES:
        violation = f"unknown next='{next_node}', fallback to integrator"
        next_node = "integrator"
    elif next_node == role:
        violation = f"self-delegation forbidden ({role} → {role}), fallback to integrator"
        next_node = "integrator"
    elif previous_node and next_node == previous_node:
        violation = (
            f"ping-pong forbidden ({role} → {previous_node}, just came from there), "
            "fallback to integrator"
        )
        next_node = "integrator"

    focus_hint_for_next = str(raw.get("focus_hint_for_next", "") or "")
    if next_node == "integrator":
        focus_hint_for_next = ""

    return (
        {
            "findings": raw.get("findings", []) or [],
            "tool_calls_made": raw.get("tool_calls_made", []) or [],
            "confidence": float(raw.get("confidence", 0.0) or 0.0),
            "next": next_node,
            "focus_hint_for_next": focus_hint_for_next,
            "rationale": str(raw.get("rationale", "") or ""),
        },
        violation,
    )


def _make_monitor(
    role: str, default_prompt: str
) -> Callable[[Config4State], dict]:
    slot_id = f"{role}_monitor"

    def _monitor(state: Config4State) -> dict:
        p_overrides = state.get("prompt_overrides", {}) or {}
        m_overrides = state.get("model_overrides", {}) or {}
        model = m_overrides.get(slot_id) or os.environ.get(
            "RALLY_MONITOR_MODEL", _DEFAULT_MONITOR_MODEL
        )
        system_prompt = p_overrides.get(slot_id, default_prompt)
        log = state["log_text"]
        target_ip = _extract_target_ip(log)
        topology = read_topology(target_ip)
        target_service = extract_target_service(log)
        service_config = get_config(target_service)
        focus_hint = state.get("pending_focus_hint") or None
        previous_node = state.get("previous_node")
        # 自分自身は previous には含めない（オーケストレータからの初回呼出のため）
        previous_for_prompt = previous_node if previous_node and previous_node != role else None

        user_blocks = _build_user_blocks(
            log,
            topology,
            service_config,
            focus_hint,
            previous_for_prompt,
            state.get("monitor_results", {}) or {},
            state.get("appended_logs") or [],
        )
        user_input = _blocks_to_text(user_blocks)

        client = anthropic.Anthropic()
        started = time.perf_counter()
        # system と user の安定ブロックに ephemeral キャッシュを設定。
        # 同一監視が複数ラウンド呼ばれる際 / 同一ログの再実行で大幅に高速化される。
        response = client.messages.create(
            model=model,
            max_tokens=2000,
            system=[
                {
                    "type": "text",
                    "text": system_prompt,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[{"role": "user", "content": user_blocks}],
        )
        latency_ms = int((time.perf_counter() - started) * 1000)
        raw = response.content[0].text
        parsed_raw, parse_error = safe_extract_json(
            raw,
            fallback={
                "findings": [],
                "tool_calls_made": [],
                "confidence": 0.0,
                "next": "integrator",
                "focus_hint_for_next": "",
                "rationale": "monitor JSON parse 失敗のため integrator にフォールバック",
            },
        )
        normalized, violation = _normalize_monitor_output(
            parsed_raw, role, previous_for_prompt
        )
        if parse_error:
            normalized["_parse_error"] = parse_error
        if violation:
            normalized["_routing_violation"] = violation

        return {
            **normalized,
            "role": role,
            "model": model,
            "tokens_in": response.usage.input_tokens,
            "tokens_out": response.usage.output_tokens,
            "latency_ms": latency_ms,
            "raw_output": raw,
            "user_input": user_input,
            "tool_target_ip": target_ip,
            "tool_target_service": target_service,
            "focus_hint_received": focus_hint,
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
