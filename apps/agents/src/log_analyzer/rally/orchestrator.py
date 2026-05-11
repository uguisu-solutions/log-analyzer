"""構成4 オーケストレータノード（再入対応版）。

役割:
- 初回呼出: ログを薄く読み、最初に呼ぶ監視を決める
- 再入時: 監視結果と過去判断を見て、追加調査が必要か判断する
  - 必要なら ``action="invoke"`` で監視を呼び直す（``focus_hints`` で観点を変える）
  - 不要なら ``action="finalize"`` で integrator へ進む

安全装置:
- ``rally_round >= rally_max_rounds`` に達したら LLM を呼ばずに強制 finalize
- 同一判断内で重複した監視名は dedup
- ``action="invoke"`` だが ``invoke`` が空なら finalize に正規化
"""
from __future__ import annotations

import json
import os
import time

import anthropic

from log_analyzer.rally._helpers import safe_extract_json
from log_analyzer.rally.state import Config4State

ORCHESTRATOR_PROMPT = """\
あなたはネットワーク／システムインフラのトリアージ・オーケストレータです。
ログと、これまでに各監視エージェントから得た結果を見て、次に何をすべきかを判断してください。

監視エージェント (5 種類):
- fw: ファイアウォール関連（policy / DENY / ACL の異常）
- routing: ルーティング・接続性関連（タイムアウト / 経路 / TCP 再送 / 帯域）
- app: アプリケーション層（5xx / プロセスエラー / OOM / 502 bad gateway）
- dns: DNS 解決の異常（SERVFAIL / NXDOMAIN / ゾーン転送失敗 / 上流タイムアウト）
- sec: セキュリティ（侵入 / 特権昇格 / C2 通信 / 既知 IOC 接触）

選択肢:
- action="invoke": さらに監視エージェントを呼ぶ（observation を絞る focus_hints を渡せる）
- action="finalize": 統合段階に進む

判断ルール:
- 初回（監視結果が空）: ログの語彙（kernel/iptables→fw, named/SERVFAIL→dns, sshd/sudo→sec 等）から
  関連する監視を 1 つ以上選ぶ。判断不能なら 5 つすべて
- 監視結果がある場合:
  - finding が薄い／confidence が低い／矛盾がある → 該当監視を再呼出（focus_hints で観点を変える）
  - 既存結果から別レイヤの調査が必要と判明（例: dns 失敗 → app 502 の連鎖）→ 該当監視を追加呼出
  - すべての主要原因が裏付けられた・これ以上情報が増えない → finalize
- 同じ監視を同じ観点で再呼出するのは禁止。focus_hints は前回と必ず異なる切り口にする
- 履歴 (orchestrator_history) を確認し、過去と同じ判断を繰り返さない
- ラウンド上限が近い場合は積極的に finalize する（無駄な再呼出を避ける）

出力 (JSON のみ、コードフェンス不要):
追加調査する場合:
{
  "action": "invoke",
  "invoke": ["fw", "routing"],
  "focus_hints": {"fw": "DENY が連続している policy ID 別に集計して報告"},
  "rationale": "FW で DENY 多発、その下流で upstream timeout が出ているため両方起動"
}

統合に進む場合:
{
  "action": "finalize",
  "rationale": "DNS 解決失敗が App 502 の根本原因と裏付けられた。追加調査の価値が低い"
}
"""

VALID_MONITORS = {"fw", "routing", "app", "dns", "sec"}


def _build_user_input(state: Config4State, current_round: int, max_rounds: int) -> str:
    parts: list[str] = [f"## ログ\n{state['log_text']}"]

    monitor_results = state.get("monitor_results") or {}
    if monitor_results:
        parts.append("## これまでの監視結果")
        parts.append(json.dumps(monitor_results, ensure_ascii=False, indent=2))
    else:
        parts.append("## これまでの監視結果\n(まだ無し — 初回判断)")

    history = state.get("orchestrator_history") or []
    if history:
        parts.append("## オーケストレータの過去判断")
        parts.append(json.dumps(history, ensure_ascii=False, indent=2))

    next_round = current_round + 1
    parts.append(
        f"## ラウンド情報\n"
        f"これまで完了したラウンド: {current_round}, 上限: {max_rounds}\n"
        f"次のラウンド番号: {next_round}"
        + (" （上限到達後の最終判断 — 必要性が低ければ必ず finalize）"
           if next_round >= max_rounds else "")
    )

    return "\n\n".join(parts)


def _normalize_decision(raw_decision: dict, next_round: int) -> dict:
    action = raw_decision.get("action", "finalize")
    invoke_raw = raw_decision.get("invoke") or []

    # 同一判断内で同じ監視名の重複は除去（順序保持）
    invoke: list[str] = []
    for m in invoke_raw:
        if m in VALID_MONITORS and m not in invoke:
            invoke.append(m)

    focus_hints_raw = raw_decision.get("focus_hints") or {}
    focus_hints = {
        k: str(v) for k, v in focus_hints_raw.items()
        if k in VALID_MONITORS and v
    }

    # action=invoke だが invoke 配列が空 → finalize 扱い（暴走防止）
    if action == "invoke" and not invoke:
        action = "finalize"

    if action == "finalize":
        invoke = []
        focus_hints = {}

    return {
        "action": action,
        "invoke": invoke,
        "focus_hints": focus_hints,
        "rationale": raw_decision.get("rationale", ""),
        "round": next_round,
        "forced": False,
    }


def _force_invoke_decision(next_round: int, min_rounds: int) -> dict:
    """force_min_rounds 強制時に使う「全監視を雑観点で再呼出」の決定。

    PoC のデモで「ラリーが起きている様子」を確実に見せるための仕組み。
    """
    return {
        "action": "invoke",
        "invoke": ["fw", "routing", "app", "dns", "sec"],
        "focus_hints": {
            "fw": f"観点 round={next_round}: 既出と異なる切り口で policy / DENY を再点検",
            "routing": f"観点 round={next_round}: 既出と異なる切り口で経路 / 再送 / 帯域を再点検",
            "app": f"観点 round={next_round}: 既出と異なる切り口で 5xx / プロセス / OOM を再点検",
            "dns": f"観点 round={next_round}: 既出と異なる切り口で SERVFAIL / ゾーン転送を再点検",
            "sec": f"観点 round={next_round}: 既出と異なる切り口で 認証失敗 / 特権昇格 / C2 を再点検",
        },
        "rationale": (
            f"rally_force_min_rounds={min_rounds} 未到達のため、"
            f"LLM が finalize と判断した結果を override してラリーを継続"
        ),
        "round": next_round,
        "forced": True,
        "forced_kind": "min_rounds",
    }


def orchestrator_node(state: Config4State) -> dict:
    p_overrides = state.get("prompt_overrides", {}) or {}
    m_overrides = state.get("model_overrides", {}) or {}
    model = m_overrides.get("orchestrator") or os.environ.get("BASELINE_MODEL", "claude-sonnet-4-5")
    system_prompt = p_overrides.get("orchestrator", ORCHESTRATOR_PROMPT)

    current_round = state.get("rally_round", 0)
    max_rounds = state.get("rally_max_rounds") or int(
        os.environ.get("RALLY_MAX_ROUNDS", "3")
    )
    force_min_rounds = state.get("rally_force_min_rounds") or int(
        os.environ.get("RALLY_FORCE_MIN_ROUNDS", "0")
    )
    # min が max を超えるのは不整合なので clamp
    force_min_rounds = min(force_min_rounds, max_rounds)
    next_round = current_round + 1

    # 上限到達: LLM を呼ばずに強制 finalize
    if current_round >= max_rounds:
        forced_decision = {
            "action": "finalize",
            "invoke": [],
            "focus_hints": {},
            "rationale": (
                f"rally_max_rounds={max_rounds} に到達したため強制 finalize"
                "（LLM 呼び出しを抑制）"
            ),
            "round": next_round,
            "forced": True,
            "forced_kind": "max_rounds",
        }
        return {
            "orchestrator_decision": forced_decision,
            "orchestrator_history": [forced_decision],
            "rally_round": next_round,
            "rally_max_rounds": max_rounds,
            "focus_hints": {},
        }

    user_input = _build_user_input(state, current_round, max_rounds)

    client = anthropic.Anthropic()
    started = time.perf_counter()
    response = client.messages.create(
        model=model,
        max_tokens=1200,
        system=system_prompt,
        messages=[{"role": "user", "content": user_input}],
    )
    latency_ms = int((time.perf_counter() - started) * 1000)
    raw = response.content[0].text
    # parse 失敗 → 安全側で finalize を選ぶ。次ラウンドが回らない代わりに
    # ループ全体は完走できる
    raw_decision, parse_error = safe_extract_json(
        raw,
        fallback={
            "action": "finalize",
            "rationale": "orchestrator JSON parse 失敗のためフォールバックで finalize",
        },
    )
    decision = _normalize_decision(raw_decision, next_round)
    if parse_error:
        decision["parse_error"] = parse_error

    # force_min_rounds 未達なのに finalize を選んだら invoke に override
    if (
        decision["action"] == "finalize"
        and current_round < force_min_rounds
        and current_round > 0  # 初回 finalize は許す（ログが本当に空のケース）
    ):
        decision = _force_invoke_decision(next_round, force_min_rounds)

    out: dict = {
        "orchestrator_decision": decision,
        "orchestrator_history": [decision],
        "rally_round": next_round,
        "rally_max_rounds": max_rounds,
        "rally_force_min_rounds": force_min_rounds,
        "focus_hints": decision["focus_hints"],
        "token_log": [
            {
                "role": "orchestrator",
                "model": model,
                "tokens_in": response.usage.input_tokens,
                "tokens_out": response.usage.output_tokens,
                "latency_ms": latency_ms,
                "input": user_input[:2000],
                "raw_output": raw,
                "round": next_round,
            }
        ],
    }
    # 初回呼出時のみ monitor_results を空 dict で初期化
    if current_round == 0:
        out["monitor_results"] = {}
    return out
