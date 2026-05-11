"""構成4 オーケストレータ駆動ラリーの単体テスト。

LLM 呼び出しはモックし、判定・正規化・ルーティングの純粋ロジックのみ検証する。
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from log_analyzer.rally import orchestrator as orch_mod
from log_analyzer.rally.orchestrator import (
    _normalize_decision,
    orchestrator_node,
)
from log_analyzer.rally_agent import _route_after_orchestrator, build_graph


def _mock_anthropic_response(payload: dict, tokens_in: int = 50, tokens_out: int = 30):
    """anthropic.Anthropic().messages.create が返す擬似 Response を作る。"""
    return SimpleNamespace(
        content=[SimpleNamespace(text=json.dumps(payload, ensure_ascii=False))],
        usage=SimpleNamespace(input_tokens=tokens_in, output_tokens=tokens_out),
    )


# ---------------- _normalize_decision の純粋ロジック ----------------


def test_normalize_dedup_duplicate_monitors_in_invoke():
    raw = {
        "action": "invoke",
        "invoke": ["fw", "routing", "fw", "app", "routing"],
        "focus_hints": {"fw": "観点1"},
        "rationale": "test",
    }
    out = _normalize_decision(raw, next_round=2)
    assert out["invoke"] == ["fw", "routing", "app"]
    assert out["round"] == 2
    assert out["forced"] is False


def test_normalize_invalid_monitor_names_are_dropped():
    """5 監視 (fw/routing/app/dns/sec) 以外の名前は除外される。"""
    raw = {
        "action": "invoke",
        "invoke": ["fw", "ipam", "bogus", "routing", "dns"],
        "focus_hints": {"fw": "x", "ipam": "ignored", "bogus": "ignored"},
        "rationale": "",
    }
    out = _normalize_decision(raw, next_round=1)
    assert out["invoke"] == ["fw", "routing", "dns"]
    assert "ipam" not in out["focus_hints"]
    assert "bogus" not in out["focus_hints"]


def test_normalize_invoke_with_empty_list_becomes_finalize():
    raw = {
        "action": "invoke",
        "invoke": [],
        "focus_hints": {"fw": "ignored"},
        "rationale": "no monitors selected",
    }
    out = _normalize_decision(raw, next_round=2)
    assert out["action"] == "finalize"
    assert out["invoke"] == []
    assert out["focus_hints"] == {}


def test_normalize_finalize_clears_invoke_and_focus_hints():
    raw = {
        "action": "finalize",
        "invoke": ["fw"],  # 無視されるべき
        "focus_hints": {"fw": "ignored"},
        "rationale": "done",
    }
    out = _normalize_decision(raw, next_round=3)
    assert out["action"] == "finalize"
    assert out["invoke"] == []
    assert out["focus_hints"] == {}


# ---------------- 上限到達時の強制 finalize（LLM 呼ばない） ----------------


def test_orchestrator_force_finalize_at_max_rounds_skips_llm(monkeypatch):
    """rally_round が rally_max_rounds 以上のとき LLM を呼ばず finalize する。"""
    # anthropic.Anthropic を呼ぼうとしたら必ずテスト失敗にする
    sentinel = MagicMock(side_effect=AssertionError("LLM should not be called at max rounds"))
    monkeypatch.setattr(orch_mod.anthropic, "Anthropic", sentinel)

    state = {
        "log_text": "dummy log",
        "rally_round": 3,
        "rally_max_rounds": 3,
        "monitor_results": {"fw": {"findings": [], "confidence": 0.5}},
        "orchestrator_history": [{"action": "invoke", "round": 1}],
    }
    out = orchestrator_node(state)

    assert out["orchestrator_decision"]["action"] == "finalize"
    assert out["orchestrator_decision"]["forced"] is True
    assert out["rally_round"] == 4
    # token_log は積まれない（LLM 呼んでないので）
    assert "token_log" not in out


# ---------------- LLM レスポンスから invoke が dedup される ----------------


def test_orchestrator_dedups_via_llm_response(monkeypatch):
    """LLM が同じ監視を複数回返しても dedup される。"""
    payload = {
        "action": "invoke",
        "invoke": ["fw", "fw", "routing", "fw"],
        "focus_hints": {"fw": "DENY 別観点", "routing": "再送パターン"},
        "rationale": "duplicate test",
    }

    fake_client = MagicMock()
    fake_client.messages.create.return_value = _mock_anthropic_response(payload)
    monkeypatch.setattr(orch_mod.anthropic, "Anthropic", lambda: fake_client)

    state = {
        "log_text": "log content",
        "rally_round": 1,
        "rally_max_rounds": 3,
        "monitor_results": {"fw": {"findings": [], "confidence": 0.6}},
    }
    out = orchestrator_node(state)

    decision = out["orchestrator_decision"]
    assert decision["action"] == "invoke"
    assert decision["invoke"] == ["fw", "routing"]
    assert decision["focus_hints"] == {"fw": "DENY 別観点", "routing": "再送パターン"}
    assert out["rally_round"] == 2
    assert len(out["token_log"]) == 1


# ---------------- 初回呼出時に monitor_results を空 dict 初期化 ----------------


def test_orchestrator_initializes_monitor_results_on_first_call(monkeypatch):
    payload = {
        "action": "invoke",
        "invoke": ["fw"],
        "focus_hints": {},
        "rationale": "initial",
    }
    fake_client = MagicMock()
    fake_client.messages.create.return_value = _mock_anthropic_response(payload)
    monkeypatch.setattr(orch_mod.anthropic, "Anthropic", lambda: fake_client)

    state = {"log_text": "first call"}  # rally_round が無い → 0 扱い
    out = orchestrator_node(state)

    assert out["rally_round"] == 1
    assert out["monitor_results"] == {}  # 初期化される
    assert out["orchestrator_decision"]["round"] == 1


def test_orchestrator_does_not_reset_monitor_results_on_subsequent_call(monkeypatch):
    payload = {"action": "finalize", "rationale": "done"}
    fake_client = MagicMock()
    fake_client.messages.create.return_value = _mock_anthropic_response(payload)
    monkeypatch.setattr(orch_mod.anthropic, "Anthropic", lambda: fake_client)

    state = {
        "log_text": "later call",
        "rally_round": 1,
        "monitor_results": {"fw": {"findings": [{"summary": "x"}]}},
    }
    out = orchestrator_node(state)

    # 2 回目以降は monitor_results キーを返さない（LangGraph reducer が既存値を保持）
    assert "monitor_results" not in out
    assert out["orchestrator_decision"]["action"] == "finalize"


# ---------------- ルーティングロジック ----------------


def test_route_invoke_returns_monitor_list():
    state = {
        "orchestrator_decision": {
            "action": "invoke",
            "invoke": ["fw", "routing"],
        }
    }
    assert _route_after_orchestrator(state) == ["fw", "routing"]


def test_route_finalize_returns_integrator():
    state = {"orchestrator_decision": {"action": "finalize", "invoke": []}}
    assert _route_after_orchestrator(state) == ["integrator"]


def test_route_invoke_with_empty_list_falls_back_to_integrator():
    state = {"orchestrator_decision": {"action": "invoke", "invoke": []}}
    assert _route_after_orchestrator(state) == ["integrator"]


def test_route_missing_decision_returns_integrator():
    assert _route_after_orchestrator({}) == ["integrator"]


# ---------------- グラフ構築の smoke test ----------------


def test_build_graph_compiles_without_error():
    """グラフ定義が壊れていないことだけを確認するスモークテスト。"""
    compiled = build_graph()
    assert compiled is not None


# ---------------- 統合: グラフ全体が orchestrator を再入するか ----------------


def _make_routed_anthropic_factory(orchestrator_responses, monitor_response, integrator_response):
    """role 別に異なる擬似 Response を返す Anthropic ファクトリを作る。

    orchestrator は呼出回数で順に異なる payload を返す（1 回目=invoke, 2 回目=finalize 等）。
    monitor / integrator は固定 payload を返す。
    どの role の呼び出しかを判別するために system prompt のキーワードを見る。
    """
    call_state = {"orchestrator_idx": 0}

    def _create(model, max_tokens, system, messages):
        # System prompt の冒頭文字列で role を判別
        if "オーケストレータ" in system and "トリアージ" in system:
            payload = orchestrator_responses[call_state["orchestrator_idx"]]
            call_state["orchestrator_idx"] += 1
        elif "監視エージェント" in system:
            payload = monitor_response
        elif "最終統合エージェント" in system:
            payload = integrator_response
        else:
            raise AssertionError(f"unrecognized system prompt: {system[:100]}")
        return _mock_anthropic_response(payload)

    fake_client = MagicMock()
    fake_client.messages.create.side_effect = _create
    return lambda: fake_client, call_state


def test_graph_loops_through_orchestrator_when_invoke_then_finalize(monkeypatch):
    """orchestrator が 1 回目=invoke、2 回目=finalize を返すと監視→orchestrator→integrator と回る。"""
    from log_analyzer.rally import integrator as integ_mod
    from log_analyzer.rally import monitors as mon_mod
    from log_analyzer.rally_agent import build_graph as bg

    orchestrator_responses = [
        # 1 回目: fw を呼ぶ
        {
            "action": "invoke",
            "invoke": ["fw"],
            "focus_hints": {"fw": "DENY を最優先で"},
            "rationale": "initial",
        },
        # 2 回目: 結果を見て finalize
        {
            "action": "finalize",
            "rationale": "fw の confidence が高いので統合へ",
        },
    ]
    monitor_response = {
        "findings": [{"category": "FW", "summary": "DENY 多数", "evidence": ["..."]}],
        "tool_calls_made": ["read_topology(...)"],
        "confidence": 0.9,
    }
    integrator_response = {
        "root_cause_candidates": [
            {"rank": 1, "category": "FW", "summary": "policy block", "evidence": ["..."]}
        ],
        "recommended_actions": [
            {"action": "review policy", "human_judgment_required": True, "risk_level": "mid"}
        ],
        "confidence": 0.9,
    }

    factory, call_state = _make_routed_anthropic_factory(
        orchestrator_responses, monitor_response, integrator_response
    )
    monkeypatch.setattr(orch_mod.anthropic, "Anthropic", factory)
    monkeypatch.setattr(mon_mod.anthropic, "Anthropic", factory)
    monkeypatch.setattr(integ_mod.anthropic, "Anthropic", factory)

    compiled = bg()
    final_state = compiled.invoke(
        {
            "log_text": "dummy log dst=10.0.20.5",
            "log_ref": "test",
            "prompt_overrides": {},
            "model_overrides": {},
        }
    )

    # orchestrator は 2 回呼ばれたはず
    assert call_state["orchestrator_idx"] == 2, (
        f"orchestrator should be invoked twice, got {call_state['orchestrator_idx']}"
    )
    # rally_round は 2 まで進んでいる
    assert final_state["rally_round"] == 2
    # 履歴も 2 件
    assert len(final_state["orchestrator_history"]) == 2
    assert final_state["orchestrator_history"][0]["action"] == "invoke"
    assert final_state["orchestrator_history"][1]["action"] == "finalize"
    # 最後は integrator まで到達
    assert final_state["integrator_result"]["confidence"] == 0.9


def test_orchestrator_force_min_rounds_overrides_finalize(monkeypatch):
    """LLM が finalize を返しても rally_force_min_rounds 未達なら invoke に override される。"""
    payload = {"action": "finalize", "rationale": "early done"}
    fake_client = MagicMock()
    fake_client.messages.create.return_value = _mock_anthropic_response(payload)
    monkeypatch.setattr(orch_mod.anthropic, "Anthropic", lambda: fake_client)

    state = {
        "log_text": "log content",
        "rally_round": 1,  # まだ 1 ラウンドしか終わっていない
        "rally_max_rounds": 3,
        "rally_force_min_rounds": 2,
        "monitor_results": {"fw": {"findings": [], "confidence": 0.6}},
    }
    out = orchestrator_node(state)

    decision = out["orchestrator_decision"]
    # finalize が override されて invoke になる
    assert decision["action"] == "invoke"
    assert decision["forced"] is True
    assert decision["forced_kind"] == "min_rounds"
    assert "fw" in decision["invoke"]
    assert "routing" in decision["invoke"]
    assert "app" in decision["invoke"]
    # focus_hints も生成される
    assert "fw" in decision["focus_hints"]


def test_orchestrator_force_min_rounds_does_not_apply_on_first_call(monkeypatch):
    """force_min_rounds が立っていても初回 finalize（current_round=0）は許す。"""
    payload = {"action": "finalize", "rationale": "trivial log"}
    fake_client = MagicMock()
    fake_client.messages.create.return_value = _mock_anthropic_response(payload)
    monkeypatch.setattr(orch_mod.anthropic, "Anthropic", lambda: fake_client)

    state = {
        "log_text": "trivial",
        "rally_force_min_rounds": 2,
        # rally_round 未設定 → 0
    }
    out = orchestrator_node(state)
    # 初回 finalize は override されない（ログが本当に空のケースを許す）
    assert out["orchestrator_decision"]["action"] == "finalize"
    assert out["orchestrator_decision"]["forced"] is False


def test_graph_force_finalizes_at_max_rounds(monkeypatch):
    """orchestrator が常に invoke を返しても rally_max_rounds で打ち切られる。"""
    from log_analyzer.rally import integrator as integ_mod
    from log_analyzer.rally import monitors as mon_mod
    from log_analyzer.rally_agent import build_graph as bg

    # 何度呼ばれても fw を invoke し続ける（force finalize で止まることを確認）
    always_invoke = {
        "action": "invoke",
        "invoke": ["fw"],
        "focus_hints": {"fw": "また別観点"},
        "rationale": "loop",
    }
    monitor_response = {
        "findings": [],
        "tool_calls_made": [],
        "confidence": 0.3,
    }
    integrator_response = {
        "root_cause_candidates": [],
        "recommended_actions": [],
        "confidence": 0.3,
    }

    factory, call_state = _make_routed_anthropic_factory(
        [always_invoke] * 10,  # 余裕を持たせる
        monitor_response,
        integrator_response,
    )
    monkeypatch.setattr(orch_mod.anthropic, "Anthropic", factory)
    monkeypatch.setattr(mon_mod.anthropic, "Anthropic", factory)
    monkeypatch.setattr(integ_mod.anthropic, "Anthropic", factory)

    compiled = bg()
    final_state = compiled.invoke(
        {
            "log_text": "dummy log dst=10.0.20.5",
            "log_ref": "test",
            "prompt_overrides": {},
            "model_overrides": {},
            "rally_max_rounds": 2,  # 2 ラウンドで強制 finalize
        }
    )

    # orchestrator は LLM 経由で 2 回呼ばれた（3 回目は強制 finalize で LLM スキップ）
    assert call_state["orchestrator_idx"] == 2
    # 履歴は 3 件（LLM 2 回 + 強制 finalize 1 回）
    assert len(final_state["orchestrator_history"]) == 3
    assert final_state["orchestrator_history"][-1]["forced"] is True
    assert final_state["rally_round"] == 3
