"""監視ノードの調査根拠の保存 (確認事項 A-3) のテスト。

監視の findings / evidence / tool_calls を MonitorReport として結果に残す処理:
    - 正規化 (LLM 出力のブレを吸収)
    - 上限による切り詰めと truncation_note
    - AnalysisResult / StageOutput への格納と後方互換
"""
from __future__ import annotations

from log_analyzer.rally_agent import (
    _MAX_EVIDENCE_CHARS,
    _MAX_EVIDENCE_PER_FINDING,
    _MAX_FINDINGS_PER_MONITOR,
    _MAX_TOOL_CALLS,
    _build_analysis_result,
    _build_monitor_report,
)
from log_analyzer.schema import AnalysisResult, MonitorReport


def _monitor_result(**overrides) -> dict:
    """監視ノード (monitors._make_monitor) の戻り値を模した dict。"""
    base = {
        "role": "fw",
        "model": "claude-opus-4-7",
        "confidence": 0.8,
        "findings": [
            {
                "category": "FW",
                "summary": "inside_out ACL で 10.1.2.21 宛が DENY",
                "evidence": ["2026-08-01 10:00:00 deny tcp 10.1.1.5 -> 10.1.2.21:443"],
            }
        ],
        "tool_calls_made": ["read_topology(10.1.2.21)", "get_config(fw-01)"],
        "rationale": "DENY を確認したので routing で影響範囲を裏取りする",
        "focus_hint_received": "FW の DENY を確認してほしい",
        "focus_hint_for_next": "DENY 宛先の経路と再送を見てほしい",
    }
    base.update(overrides)
    return base


def test_build_monitor_report_keeps_findings_and_evidence():
    """findings / evidence / tool_calls / rationale がそのまま保存されること。"""
    rep = _build_monitor_report(_monitor_result(), round_no=1)
    assert isinstance(rep, MonitorReport)
    assert rep.round == 1 and rep.role == "fw" and rep.confidence == 0.8
    assert len(rep.findings) == 1
    assert rep.findings[0].category == "FW"
    assert "DENY" in rep.findings[0].summary
    assert rep.findings[0].evidence == [
        "2026-08-01 10:00:00 deny tcp 10.1.1.5 -> 10.1.2.21:443"
    ]
    assert rep.tool_calls == ["read_topology(10.1.2.21)", "get_config(fw-01)"]
    assert rep.focus_hint_received.startswith("FW の DENY")
    assert rep.focus_hint_for_next.startswith("DENY 宛先")
    assert rep.truncation_note == ""
    assert rep.parse_error is None


def test_build_monitor_report_normalizes_llm_variations():
    """evidence が文字列 1 件 / findings が文字列配列でも落ちずに正規化されること。"""
    rep = _build_monitor_report(
        _monitor_result(
            findings=[
                {"category": "Net", "summary": "再送多発", "evidence": "retransmit x120"},
                "evidence を持たない素の文字列所見",
                {"summary": "evidence キー自体が無い"},
            ],
            tool_calls_made=None,
        ),
        round_no=2,
    )
    assert [f.evidence for f in rep.findings] == [["retransmit x120"], [], []]
    assert rep.findings[1].summary == "evidence を持たない素の文字列所見"
    assert rep.tool_calls == []


def test_build_monitor_report_truncates_and_notes():
    """上限を超えた findings / evidence / tool_calls が切り詰められ、注記が残ること。"""
    long_evidence = "x" * (_MAX_EVIDENCE_CHARS + 100)
    findings = [
        {
            "category": "App",
            "summary": f"finding {i}",
            "evidence": [long_evidence] * (_MAX_EVIDENCE_PER_FINDING + 2),
        }
        for i in range(_MAX_FINDINGS_PER_MONITOR + 3)
    ]
    rep = _build_monitor_report(
        _monitor_result(
            findings=findings,
            tool_calls_made=[f"bigquery_query(host='h{i}')" for i in range(_MAX_TOOL_CALLS + 5)],
        ),
        round_no=1,
    )
    assert len(rep.findings) == _MAX_FINDINGS_PER_MONITOR
    assert all(len(f.evidence) == _MAX_EVIDENCE_PER_FINDING for f in rep.findings)
    # 切り詰めた evidence は末尾に … が付き、上限 + 1 文字に収まる
    ev = rep.findings[0].evidence[0]
    assert ev.endswith("…") and len(ev) == _MAX_EVIDENCE_CHARS + 1
    assert len(rep.tool_calls) == _MAX_TOOL_CALLS
    assert "findings" in rep.truncation_note
    assert "evidence" in rep.truncation_note
    assert "tool_calls" in rep.truncation_note


def test_build_monitor_report_keeps_parse_error():
    rep = _build_monitor_report(
        _monitor_result(_parse_error="JSON 抽出に失敗", findings=[]), round_no=3
    )
    assert rep.parse_error == "JSON 抽出に失敗"
    assert rep.findings == []


def test_monitor_reports_attached_to_analysis_result():
    """_build_analysis_result に渡した MonitorReport が結果に載ること。"""
    reports = [_build_monitor_report(_monitor_result(), round_no=1)]
    ar = _build_analysis_result(
        log_ref="test",
        trace_id="t-1",
        integrator_result={
            "root_cause_candidates": [],
            "recommended_actions": [],
            "confidence": 0.7,
        },
        token_log=[
            {"role": "fw", "round": 1, "model": "m", "tokens_in": 1, "tokens_out": 1, "latency_ms": 1},
        ],
        delegation_history=[],
        rally_round=1,
        rally_max_rounds=3,
        wall_ms=100,
        monitor_reports=reports,
    )
    assert len(ar.monitor_reports) == 1
    assert ar.monitor_reports[0].role == "fw"
    assert ar.monitor_reports[0].findings[0].evidence


def test_monitor_reports_in_streamed_final_result(monkeypatch):
    """ストリーム実行の final イベントに、監視 2 ノード分の根拠が載ること。

    LLM はモックし、fw → routing → integrator のチェーンを 1 本流す
    (モックの組み立ては test_rally_orchestrator と同じ方式)。
    """
    import asyncio

    from test_rally_orchestrator import _setup_three_step_chain

    from log_analyzer.rally_agent import run_rally_stream

    _setup_three_step_chain(monkeypatch)

    async def _collect() -> list:
        return [
            ev
            async for ev in run_rally_stream(
                "dummy log dst=10.0.20.5", "test", rally_max_rounds=5, decision_waiter=None
            )
        ]

    events = asyncio.run(_collect())
    result = events[-1].data["result"]
    reports = result["monitor_reports"]
    assert [r["role"] for r in reports] == ["fw", "routing"]
    assert [r["round"] for r in reports] == [1, 2]
    # 監視が出した所見・根拠・ツール・委譲理由がそのまま残る
    assert reports[0]["findings"][0]["summary"] == "DENY 多発"
    assert reports[0]["findings"][0]["evidence"] == ["..."]
    assert reports[0]["tool_calls"] == ["read_topology(...)"]
    assert reports[0]["rationale"].startswith("FW は判明")
    assert reports[0]["focus_hint_for_next"] == "影響範囲を Routing で"
    assert reports[1]["findings"][0]["category"] == "Net"


def test_monitor_reports_default_empty_for_old_results():
    """monitor_reports を持たない旧 JSON もそのままパースできること (後方互換)。"""
    old = {
        "trace_id": "t-old",
        "config_id": "config4",
        "input_log_ref": "inline",
        "root_cause_candidates": [],
        "recommended_actions": [],
        "confidence": 0.5,
        "stage_outputs": [
            {"stage": "log", "confidence": 0.5},
        ],
    }
    ar = AnalysisResult.model_validate(old)
    assert ar.monitor_reports == []
    assert ar.stage_outputs[0].monitor_reports == []
