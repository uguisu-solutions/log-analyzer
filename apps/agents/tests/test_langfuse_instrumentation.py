"""Langfuse 計装のテスト (確認事項 B-3 / B-1)。

B-3: Generation に start_time / end_time を渡していないため Langfuse の
     Latency が 0.00s になっていた (計装漏れ) の修正。
B-1: 方針プランナー / 監査GPT を Generation 化する対応。
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from test_rally_orchestrator import _setup_three_step_chain


class _FakeTrace:
    """Langfuse の trace を模したスパイ。generation の引数を全部記録する。"""

    def __init__(self) -> None:
        self.id = "trace-test-1"
        self.generations: list[dict] = []
        self.updates: list[dict] = []

    def generation(self, **kwargs):
        self.generations.append(kwargs)
        return SimpleNamespace(id=f"gen-{len(self.generations)}")

    def update(self, **kwargs):
        self.updates.append(kwargs)


@pytest.fixture
def fake_langfuse(monkeypatch):
    """rally_agent / audit_agent / api が使う Langfuse クライアントを差し替える。"""
    trace = _FakeTrace()
    client = MagicMock()
    client.trace.return_value = trace
    client.generation.side_effect = lambda **kw: trace.generations.append(kw)
    from log_analyzer import rally_agent, tracing

    monkeypatch.setattr(rally_agent, "get_client", lambda: client)
    monkeypatch.setattr(rally_agent, "flush", lambda: None)
    monkeypatch.setattr(tracing, "get_client", lambda: client)
    return trace


# ─── B-3: start_time / end_time ────────────────────────────────────


def test_generations_carry_start_and_end_time(monkeypatch, fake_langfuse):
    """全 Generation に開始/終了の絶対時刻が入り、end >= start であること。"""
    from log_analyzer.rally_agent import run_rally_stream

    _setup_three_step_chain(monkeypatch)

    async def _run():
        return [
            ev
            async for ev in run_rally_stream(
                "dummy log dst=10.0.20.5", "test", rally_max_rounds=5, decision_waiter=None
            )
        ]

    asyncio.run(_run())

    gens = fake_langfuse.generations
    # orchestrator + fw + routing + integrator
    assert len(gens) == 4
    for g in gens:
        assert isinstance(g["start_time"], datetime)
        assert isinstance(g["end_time"], datetime)
        assert g["end_time"] >= g["start_time"]
        assert g["start_time"].tzinfo is not None  # UTC aware
    # 実行順どおりに並ぶ (orchestrator が最初、integrator が最後)
    assert gens[0]["start_time"] <= gens[-1]["start_time"]


def test_parse_error_marks_generation_warning(monkeypatch, fake_langfuse):
    """JSON パースに失敗した監視は WARNING + status_message が付くこと。"""
    from log_analyzer.rally_agent import run_rally_stream

    _setup_three_step_chain(monkeypatch)
    # fw 監視だけ JSON にならない応答を返させる
    from log_analyzer.rally import monitors as mon_mod

    original = mon_mod.anthropic.Anthropic

    def _factory():
        client = original()
        create = client.messages.create

        def _create(model, max_tokens, system, messages, **kwargs):
            text = system if isinstance(system, str) else "".join(
                b.get("text", "") for b in system if isinstance(b, dict)
            )
            if "FW レイヤ" in text:
                return SimpleNamespace(
                    content=[SimpleNamespace(text="JSON ではない散文の応答")],
                    usage=SimpleNamespace(input_tokens=10, output_tokens=5),
                )
            return create(model=model, max_tokens=max_tokens, system=system, messages=messages)

        client.messages.create = _create
        return client

    monkeypatch.setattr(mon_mod.anthropic, "Anthropic", _factory)

    async def _run():
        return [
            ev
            async for ev in run_rally_stream(
                "dummy log dst=10.0.20.5", "test", rally_max_rounds=5, decision_waiter=None
            )
        ]

    asyncio.run(_run())
    warned = [g for g in fake_langfuse.generations if g.get("level") == "WARNING"]
    assert warned, "parse_error のノードに WARNING が付いていない"
    assert "parse_error" in warned[0]["status_message"]


# ─── B-1: 監査GPT の Generation 化 ──────────────────────────────────


def test_audit_emits_generation(monkeypatch, fake_langfuse):
    """run_audit が trace_id 付きで呼ばれると Generation を 1 件送ること。"""
    import log_analyzer.audit_agent as audit_mod
    from log_analyzer.schema import AnalysisResult, ConfigId

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr(audit_mod, "get_client", lambda: MagicMock(
        generation=lambda **kw: fake_langfuse.generations.append(kw)
    ))
    fake_openai = MagicMock()
    fake_openai.responses.create.return_value = SimpleNamespace(
        output_text='{"verdict": "agree", "confidence": 0.8, "summary": "妥当", '
                    '"concerns": [], "alternative_hypotheses": []}',
        usage=SimpleNamespace(input_tokens=1200, output_tokens=300),
    )
    monkeypatch.setattr(audit_mod.openai, "OpenAI", lambda: fake_openai)

    result = AnalysisResult(
        config_id=ConfigId.CONFIG4, input_log_ref="inline",
        root_cause_candidates=[], recommended_actions=[], confidence=0.8,
    )
    report = audit_mod.run_audit("log", None, result, trace_id="trace-test-1")

    assert report.verdict == "agree"
    gens = [g for g in fake_langfuse.generations if "audit" in g.get("name", "")]
    assert len(gens) == 1
    g = gens[0]
    assert g["trace_id"] == "trace-test-1"
    assert g["usage"]["input"] == 1200 and g["usage"]["output"] == 300
    # gpt-5.5 は価格表に登録済みなのでコストも出る
    assert g["usage"]["total_cost"] > 0
    assert isinstance(g["start_time"], datetime) and isinstance(g["end_time"], datetime)


def test_audit_without_trace_id_does_not_emit(monkeypatch, fake_langfuse):
    """trace_id 未指定 (CLI 等) では Generation を送らないこと。"""
    import log_analyzer.audit_agent as audit_mod
    from log_analyzer.schema import AnalysisResult, ConfigId

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    fake_openai = MagicMock()
    fake_openai.responses.create.return_value = SimpleNamespace(
        output_text='{"verdict": "agree", "confidence": 0.5, "summary": "", '
                    '"concerns": [], "alternative_hypotheses": []}',
        usage=SimpleNamespace(input_tokens=10, output_tokens=5),
    )
    monkeypatch.setattr(audit_mod.openai, "OpenAI", lambda: fake_openai)
    result = AnalysisResult(
        config_id=ConfigId.CONFIG4, input_log_ref="inline",
        root_cause_candidates=[], recommended_actions=[], confidence=0.5,
    )
    audit_mod.run_audit("log", None, result)
    assert not [g for g in fake_langfuse.generations if "audit" in g.get("name", "")]


def test_audit_failure_is_recorded(monkeypatch, fake_langfuse):
    """監査が例外で落ちた場合も、失敗として Generation に残ること。"""
    import log_analyzer.audit_agent as audit_mod
    from log_analyzer.schema import AnalysisResult, ConfigId

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr(audit_mod, "get_client", lambda: MagicMock(
        generation=lambda **kw: fake_langfuse.generations.append(kw)
    ))
    fake_openai = MagicMock()
    fake_openai.responses.create.side_effect = RuntimeError("boom")
    monkeypatch.setattr(audit_mod.openai, "OpenAI", lambda: fake_openai)
    result = AnalysisResult(
        config_id=ConfigId.CONFIG4, input_log_ref="inline",
        root_cause_candidates=[], recommended_actions=[], confidence=0.5,
    )
    report = audit_mod.run_audit("log", None, result, trace_id="trace-test-1")
    assert report.verdict == "uncertain"
    gens = [g for g in fake_langfuse.generations if "audit" in g.get("name", "")]
    assert len(gens) == 1
    assert gens[0]["level"] == "WARNING"
    assert "audit failed" in gens[0]["status_message"]


# ─── B-1: プランナーの計測値 ────────────────────────────────────────


def test_planner_returns_timestamps(monkeypatch):
    """plan_policy が Langfuse 用の started_at / ended_at / user_input を返すこと。"""
    import json

    from log_analyzer.rally import planner as planner_mod

    payload = {
        "situation_summary": "s",
        "primary_hypotheses": ["h"],
        "investigation_plan": ["p"],
        "suggested_first_node": "fw",
        "focus": "f",
        "data_to_use": [],
        "missing_data_notes": "",
    }
    fake = MagicMock()
    fake.messages.create.return_value = SimpleNamespace(
        content=[SimpleNamespace(text=json.dumps(payload, ensure_ascii=False))],
        usage=SimpleNamespace(input_tokens=500, output_tokens=100),
    )
    monkeypatch.setattr(planner_mod.anthropic, "Anthropic", lambda: fake)

    proposal = planner_mod.plan_policy("ログ本文")
    assert isinstance(proposal["started_at"], datetime)
    assert isinstance(proposal["ended_at"], datetime)
    assert proposal["ended_at"] >= proposal["started_at"]
    assert proposal["started_at"].tzinfo == timezone.utc
    assert "ログ本文" in proposal["user_input"]
    assert proposal["tokens_in"] == 500 and proposal["tokens_out"] == 100
