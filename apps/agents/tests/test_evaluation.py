"""評価エージェント・評価ストレージ・評価APIのテスト。"""
from __future__ import annotations

import json

from fastapi.testclient import TestClient

from log_analyzer import api as api_mod
from log_analyzer import evaluation_agent, storage
from log_analyzer.schema import EvaluationResult


# ─── fake anthropic client ────────────────────────────────────────
class _FakeContent:
    def __init__(self, text: str):
        self.text = text


class _FakeUsage:
    input_tokens = 123
    output_tokens = 45


class _FakeResp:
    def __init__(self, text: str):
        self.content = [_FakeContent(text)]
        self.usage = _FakeUsage()


class _FakeMessages:
    def __init__(self, text: str):
        self._text = text

    def create(self, **kw):
        return _FakeResp(self._text)


class _FakeClient:
    def __init__(self, text: str):
        self.messages = _FakeMessages(text)


def _patch_anthropic(monkeypatch, text: str):
    monkeypatch.setattr(
        evaluation_agent.anthropic, "Anthropic", lambda *a, **k: _FakeClient(text)
    )


_RESULT = {"root_cause_candidates": [{"category": "App", "summary": "x", "evidence": ["e1"]}],
           "recommended_actions": [{"action": "fix", "kind": "permanent", "confidence": 0.8}],
           "confidence": 0.7}
_SCENARIO = {"scenario_key": "M", "title": "認証", "conclusion": "真因M", "junior_pitfall": "罠M"}


def test_run_evaluation_parses(monkeypatch):
    payload = json.dumps({
        "score": 8, "good_points": ["g1", "g2"], "bad_points": ["b1"],
        "pitfalls_avoided": ["p1"], "pitfalls_hit": [], "summary": "総評",
    })
    _patch_anthropic(monkeypatch, payload)
    ev = evaluation_agent.run_evaluation(_RESULT, _SCENARIO)
    assert ev.score == 8
    assert ev.good_points == ["g1", "g2"]
    assert ev.pitfalls_avoided == ["p1"]
    assert ev.scenario_key == "M"
    assert ev.tokens_in == 123 and ev.tokens_out == 45


def test_run_evaluation_clamps_score(monkeypatch):
    _patch_anthropic(monkeypatch, json.dumps({"score": 15, "summary": "s"}))
    assert evaluation_agent.run_evaluation(_RESULT, _SCENARIO).score == 10
    _patch_anthropic(monkeypatch, json.dumps({"score": -3, "summary": "s"}))
    assert evaluation_agent.run_evaluation(_RESULT, _SCENARIO).score == 0


def test_run_evaluation_fallback_on_error(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("api down")
    monkeypatch.setattr(evaluation_agent.anthropic, "Anthropic", _boom)
    ev = evaluation_agent.run_evaluation(_RESULT, _SCENARIO)
    assert ev.score == 0
    assert "エラー" in ev.summary


def test_storage_evaluation_roundtrip():
    hid = 999999  # 実在しない履歴IDでも評価行は保存できる (FK制約なし)
    try:
        saved = storage.insert_evaluation(
            analysis_history_id=hid, scenario_key="ZZ", score=6,
            axis_assessment=["推論の道筋: 良い"],
            good_points=["g"], bad_points=["b"], pitfalls_avoided=["pa"], pitfalls_hit=["ph"],
            summary="s", model="test", tokens_in=1, tokens_out=2, latency_ms=3,
        )
        assert saved["score"] == 6 and saved["good_points"] == ["g"]
        assert saved["axis_assessment"] == ["推論の道筋: 良い"]
        rows = storage.list_evaluations(hid)
        assert len(rows) == 1 and rows[0]["pitfalls_hit"] == ["ph"]
        assert storage.delete_evaluation(saved["id"]) is True
        assert storage.list_evaluations(hid) == []
    finally:
        for r in storage.list_evaluations(hid):
            storage.delete_evaluation(r["id"])


def test_evaluate_endpoint_roundtrip(monkeypatch):
    client = TestClient(api_mod.app)
    hid, created = storage.insert_analysis_history(
        run_id="eval-test-run-xyz", kind="config-log", config_id="config4",
        analysis_mode=None, single_source=None, stage_order=None, title="t",
        confidence=0.5, tokens_in=1, tokens_out=1, latency_ms=1,
        top_category="App", top_summary="s", trace_id="t1",
        request_json="{}", result_json=json.dumps(_RESULT),
    )
    storage.upsert_answer_scenario("ZZ", conclusion="真因Z")
    monkeypatch.setattr(
        api_mod.evaluation_agent, "run_evaluation",
        lambda result, scenario, model=None: EvaluationResult(
            scenario_key="ZZ", score=7, good_points=["g"], bad_points=["b"], summary="s", model="m"
        ),
    )
    try:
        r = client.post(f"/api/analysis-history/{hid}/evaluate", json={"scenario_key": "ZZ"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["score"] == 7 and body["scenario_key"] == "ZZ"
        eid = body["id"]

        r2 = client.get(f"/api/analysis-history/{hid}/evaluations")
        assert r2.status_code == 200 and len(r2.json()["evaluations"]) == 1

        # 存在しないシナリオ → 404
        assert client.post(
            f"/api/analysis-history/{hid}/evaluate", json={"scenario_key": "NOPE"}
        ).status_code == 404

        assert client.delete(f"/api/analysis-history/{hid}/evaluations/{eid}").status_code == 200
        assert client.get(f"/api/analysis-history/{hid}/evaluations").json()["evaluations"] == []
    finally:
        for r in storage.list_evaluations(hid):
            storage.delete_evaluation(r["id"])
        storage.delete_analysis_history(hid)
        storage.delete_answer_scenario("ZZ")
