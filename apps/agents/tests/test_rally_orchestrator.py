"""構成4 委譲チェーン型ラリーの単体テスト。

LLM 呼び出しはモックし、orchestrator / monitor の正規化・遷移制約・
ストリーム生成を検証する。
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from log_analyzer.rally import monitors as mon_mod
from log_analyzer.rally import orchestrator as orch_mod
from log_analyzer.rally import integrator as integ_mod
from log_analyzer.rally.monitors import _normalize_monitor_output
from log_analyzer.rally.orchestrator import _normalize_decision, orchestrator_select_first
from log_analyzer.rally_agent import run_rally_stream


def _mock_resp(payload: dict, tokens_in: int = 50, tokens_out: int = 30):
    return SimpleNamespace(
        content=[SimpleNamespace(text=json.dumps(payload, ensure_ascii=False))],
        usage=SimpleNamespace(input_tokens=tokens_in, output_tokens=tokens_out),
    )


def _system_text(system) -> str:
    """anthropic.messages.create(system=...) は str / list[dict] の両形式を受ける。

    prompt caching 導入後は list 形式を渡しているのでテストでは平文に正規化して
    role 判別に使う。
    """
    if isinstance(system, str):
        return system
    if isinstance(system, list):
        return "".join(b.get("text", "") for b in system if isinstance(b, dict))
    return ""


# ─── orchestrator._normalize_decision ──────────────────────────────


def test_normalize_unknown_first_node_falls_back_to_fw():
    out = _normalize_decision({"first_node": "bogus", "rationale": "x"})
    assert out["first_node"] == "fw"


def test_normalize_keeps_valid_first_node():
    out = _normalize_decision({"first_node": "dns", "focus_hint": "h", "rationale": "r"})
    assert out["first_node"] == "dns"
    assert out["focus_hint"] == "h"
    assert out["rationale"] == "r"


# ─── monitor._normalize_monitor_output: 遷移制約 ─────────────────────


def test_monitor_normalize_self_delegation_blocked():
    """fw → fw は禁止 → integrator にフォールバック。"""
    raw = {"findings": [], "next": "fw", "focus_hint_for_next": "h", "confidence": 0.5, "rationale": ""}
    out, viol = _normalize_monitor_output(raw, role="fw", previous_node=None)
    assert out["next"] == "integrator"
    assert viol is not None
    assert "self-delegation" in viol


def test_monitor_normalize_ping_pong_blocked():
    """直前が fw のとき routing → fw を要求 → integrator にフォールバック。"""
    raw = {"findings": [], "next": "fw", "focus_hint_for_next": "h", "confidence": 0.5, "rationale": ""}
    out, viol = _normalize_monitor_output(raw, role="routing", previous_node="fw")
    assert out["next"] == "integrator"
    assert viol is not None
    assert "ping-pong" in viol


def test_monitor_normalize_unknown_next_blocked():
    raw = {"findings": [], "next": "bogus", "focus_hint_for_next": "h", "confidence": 0.5, "rationale": ""}
    out, viol = _normalize_monitor_output(raw, role="fw", previous_node=None)
    assert out["next"] == "integrator"
    assert viol is not None


def test_monitor_normalize_valid_delegation():
    raw = {
        "findings": [{"category": "FW", "summary": "x", "evidence": []}],
        "next": "routing",
        "focus_hint_for_next": "経路を見て",
        "confidence": 0.8,
        "rationale": "FW は判明したので Routing に移譲",
    }
    out, viol = _normalize_monitor_output(raw, role="fw", previous_node="orchestrator")
    assert out["next"] == "routing"
    assert out["focus_hint_for_next"] == "経路を見て"
    assert viol is None


def test_monitor_normalize_integrator_clears_focus_hint():
    """next=integrator のとき focus_hint_for_next は強制的に空。"""
    raw = {"findings": [], "next": "integrator", "focus_hint_for_next": "残しちゃダメ", "confidence": 0.9, "rationale": ""}
    out, viol = _normalize_monitor_output(raw, role="fw", previous_node="orchestrator")
    assert out["next"] == "integrator"
    assert out["focus_hint_for_next"] == ""
    assert viol is None


# ─── orchestrator_select_first: LLM 経路 ──────────────────────────────


def test_orchestrator_returns_chosen_first_node(monkeypatch):
    payload = {"first_node": "dns", "focus_hint": "SERVFAIL を最優先", "rationale": "dns 系のエラーが多い"}
    fake_client = MagicMock()
    fake_client.messages.create.return_value = _mock_resp(payload)
    monkeypatch.setattr(orch_mod.anthropic, "Anthropic", lambda: fake_client)

    out = orchestrator_select_first({"log_text": "named SERVFAIL", "prompt_overrides": {}, "model_overrides": {}})
    assert out["first_node"] == "dns"
    assert out["focus_hint"].startswith("SERVFAIL")


# ─── run_rally_stream: end-to-end (LLM モック) ─────────────────────


def _setup_three_step_chain(monkeypatch):
    """orchestrator → fw → routing → integrator の経路を LLM モックで仕込む。"""
    orchestrator_payload = {
        "first_node": "fw",
        "focus_hint": "DENY を最優先",
        "rationale": "初手 FW",
    }
    fw_payload = {
        "findings": [{"category": "FW", "summary": "DENY 多発", "evidence": ["..."]}],
        "tool_calls_made": ["read_topology(...)"],
        "confidence": 0.7,
        "next": "routing",
        "focus_hint_for_next": "影響範囲を Routing で",
        "rationale": "FW は判明、影響範囲を裏取り",
    }
    routing_payload = {
        "findings": [{"category": "Net", "summary": "再送あり", "evidence": ["..."]}],
        "tool_calls_made": ["read_topology(...)"],
        "confidence": 0.8,
        "next": "integrator",
        "focus_hint_for_next": "",
        "rationale": "Routing で裏取り済み、統合へ",
    }
    integrator_payload = {
        "root_cause_candidates": [
            {"category": "FW", "summary": "policy block", "evidence": ["..."]}
        ],
        "recommended_actions": [
            {"action": "review policy", "human_judgment_required": True, "risk_level": "mid"}
        ],
        "confidence": 0.9,
    }

    call_log: list[str] = []

    def _create(model, max_tokens, system, messages):
        system = _system_text(system)
        if "トリアージ" in system:
            call_log.append("orchestrator")
            return _mock_resp(orchestrator_payload)
        if "ファイアウォール" in system:
            call_log.append("fw")
            return _mock_resp(fw_payload)
        if "ルーティング" in system:
            call_log.append("routing")
            return _mock_resp(routing_payload)
        if "最終統合" in system:
            call_log.append("integrator")
            return _mock_resp(integrator_payload)
        raise AssertionError(f"unrecognized system prompt: {system[:80]}")

    fake_client = MagicMock()
    fake_client.messages.create.side_effect = _create
    factory = lambda: fake_client  # noqa: E731
    monkeypatch.setattr(orch_mod.anthropic, "Anthropic", factory)
    monkeypatch.setattr(mon_mod.anthropic, "Anthropic", factory)
    monkeypatch.setattr(integ_mod.anthropic, "Anthropic", factory)
    return call_log


def test_stream_runs_orchestrator_then_monitors_then_integrator(monkeypatch):
    call_log = _setup_three_step_chain(monkeypatch)

    async def _collect():
        events = []
        async for ev in run_rally_stream(
            "dummy log dst=10.0.20.5",
            "test",
            rally_max_rounds=5,
            decision_waiter=None,
        ):
            events.append(ev)
        return events

    events = asyncio.run(_collect())
    kinds = [e.kind for e in events]
    # 主要イベントが順番通り
    assert kinds[:3] == ["run_started", "orchestrator_start", "orchestrator_decision"]
    assert "monitor_start" in kinds
    assert "monitor_decision" in kinds
    assert "integrator_start" in kinds
    assert "integrator_done" in kinds
    assert kinds[-1] == "final"
    # orchestrator は 1 回だけ呼ばれる
    assert call_log.count("orchestrator") == 1
    # fw → routing → integrator の順
    assert call_log == ["orchestrator", "fw", "routing", "integrator"]

    final_ev = events[-1]
    result = final_ev.data["result"]
    assert result["config_id"] == "config4"
    assert result["delegation_rounds"] == 2  # fw + routing
    assert len(result["delegation_history"]) >= 3  # orch_initial + fw + routing


def test_stream_pauses_at_max_rounds_and_awaits_confirmation(monkeypatch):
    """rally_max_rounds 到達で await_confirmation が emit され、stop で integrator に移行。"""
    # orchestrator は fw を選び、fw は永遠に routing を指名、routing は永遠に fw を指名…
    # 自己 ping-pong は normalize で integrator に倒れるのでここでは A→C パターンで作る
    orchestrator_payload = {"first_node": "fw", "focus_hint": "", "rationale": ""}
    # fw は dns を指名、dns は app を指名、app は dns を指名…ping-pong は previous_node で防がれるので
    # ループを作るには 3 ノード以上で回す必要がある: fw → dns → app → fw → dns → ...
    routing_table = {
        "fw": "dns",
        "dns": "app",
        "app": "fw",
    }

    def _monitor_payload(role: str) -> dict:
        return {
            "findings": [],
            "tool_calls_made": [],
            "confidence": 0.3,
            "next": routing_table[role],
            "focus_hint_for_next": "",
            "rationale": "loop",
        }

    integrator_payload = {
        "root_cause_candidates": [],
        "recommended_actions": [],
        "confidence": 0.3,
    }

    def _create(model, max_tokens, system, messages):
        system = _system_text(system)
        if "トリアージ" in system:
            return _mock_resp(orchestrator_payload)
        if "ファイアウォール" in system:
            return _mock_resp(_monitor_payload("fw"))
        if "DNS の監視" in system:
            return _mock_resp(_monitor_payload("dns"))
        if "アプリケーション層" in system:
            return _mock_resp(_monitor_payload("app"))
        if "最終統合" in system:
            return _mock_resp(integrator_payload)
        raise AssertionError(f"unrecognized: {system[:80]}")

    fake_client = MagicMock()
    fake_client.messages.create.side_effect = _create
    factory = lambda: fake_client  # noqa: E731
    monkeypatch.setattr(orch_mod.anthropic, "Anthropic", factory)
    monkeypatch.setattr(mon_mod.anthropic, "Anthropic", factory)
    monkeypatch.setattr(integ_mod.anthropic, "Anthropic", factory)

    async def _stop_waiter() -> dict:
        return {"action": "stop"}

    async def _collect():
        events = []
        async for ev in run_rally_stream(
            "dummy log",
            "test",
            rally_max_rounds=2,
            decision_waiter=_stop_waiter,
        ):
            events.append(ev)
        return events

    events = asyncio.run(_collect())
    kinds = [e.kind for e in events]
    assert "await_confirmation" in kinds
    assert "user_decision" in kinds
    assert kinds[-1] == "final"
    final_result = events[-1].data["result"]
    # user_finalize が履歴に入っているはず
    histories = final_result["delegation_history"]
    assert any(h["kind"] == "user_finalize" for h in histories)


def test_stream_continues_when_user_extends(monkeypatch):
    """確認モーダルで continue を選ぶと rally_max_rounds が延長されて続行。"""
    orchestrator_payload = {"first_node": "fw", "focus_hint": "", "rationale": ""}

    # fw は 1 回目だけ呼ばれて integrator に進む、を想定する単純経路
    # 上限到達のテストなので: fw → dns → fw（ping-pong 防御で integrator に倒れる）
    # ここでは延長後に fw が 2 回呼ばれて integrator に行くシナリオ:
    # round1: fw → dns, round2: dns → app（max=2 で確認モーダル）, 延長 +2 で続行,
    # round3: app → integrator
    seq = {
        ("fw", 0): {"next": "dns", "focus_hint_for_next": "x", "rationale": ""},
        ("dns", 0): {"next": "app", "focus_hint_for_next": "y", "rationale": ""},
        ("app", 0): {"next": "integrator", "focus_hint_for_next": "", "rationale": "終了"},
    }
    call_counts: dict[str, int] = {"fw": 0, "dns": 0, "app": 0}

    def _payload(role: str) -> dict:
        n = call_counts[role]
        call_counts[role] += 1
        base = seq.get((role, n)) or {"next": "integrator", "focus_hint_for_next": "", "rationale": ""}
        return {
            "findings": [],
            "tool_calls_made": [],
            "confidence": 0.5,
            **base,
        }

    integrator_payload = {
        "root_cause_candidates": [],
        "recommended_actions": [],
        "confidence": 0.5,
    }

    def _create(model, max_tokens, system, messages):
        system = _system_text(system)
        if "トリアージ" in system:
            return _mock_resp(orchestrator_payload)
        if "ファイアウォール" in system:
            return _mock_resp(_payload("fw"))
        if "DNS の監視" in system:
            return _mock_resp(_payload("dns"))
        if "アプリケーション層" in system:
            return _mock_resp(_payload("app"))
        if "最終統合" in system:
            return _mock_resp(integrator_payload)
        raise AssertionError(f"unrecognized: {system[:80]}")

    fake_client = MagicMock()
    fake_client.messages.create.side_effect = _create
    factory = lambda: fake_client  # noqa: E731
    monkeypatch.setattr(orch_mod.anthropic, "Anthropic", factory)
    monkeypatch.setattr(mon_mod.anthropic, "Anthropic", factory)
    monkeypatch.setattr(integ_mod.anthropic, "Anthropic", factory)

    call_count = {"n": 0}

    async def _continue_then_stop() -> dict:
        call_count["n"] += 1
        if call_count["n"] == 1:
            return {"action": "continue", "extend_by": 2}
        return {"action": "stop"}

    async def _collect():
        events = []
        async for ev in run_rally_stream(
            "dummy log",
            "test",
            rally_max_rounds=2,
            decision_waiter=_continue_then_stop,
        ):
            events.append(ev)
        return events

    events = asyncio.run(_collect())
    kinds = [e.kind for e in events]
    # await_confirmation を 1 回経験
    assert kinds.count("await_confirmation") == 1
    final_result = events[-1].data["result"]
    # 延長 +2 されたので max は 4 になっているはず
    assert final_result["delegation_max_rounds"] == 4
    # user_extend が履歴に記録
    assert any(h["kind"] == "user_extend" for h in final_result["delegation_history"])
