"""ラウンド単位集計 (Phase D) のスモークテスト。

token_log → RoundMetrics の組み立てを検証。
"""
from __future__ import annotations

from log_analyzer.rally_agent import _build_round_metrics
from log_analyzer.schema import RoundMetrics


def test_build_round_metrics_basic_order():
    """orchestrator → 監視 (round 順) → integrator の順に並ぶこと。"""
    token_log = [
        {"role": "orchestrator", "model": "haiku", "tokens_in": 100, "tokens_out": 30, "latency_ms": 800},
        {"role": "fw", "round": 1, "model": "haiku", "tokens_in": 500, "tokens_out": 150, "latency_ms": 2000},
        {"role": "routing", "round": 2, "model": "haiku", "tokens_in": 600, "tokens_out": 180, "latency_ms": 2200},
        {"role": "integrator", "model": "sonnet", "tokens_in": 2000, "tokens_out": 400, "latency_ms": 4500},
    ]
    rounds = _build_round_metrics(token_log)
    # 4 件、orchestrator(round=0) → fw(round=1) → routing(round=2) → integrator(round=3)
    assert len(rounds) == 4
    assert [r.role for r in rounds] == ["orchestrator", "fw", "routing", "integrator"]
    assert [r.round for r in rounds] == [0, 1, 2, 3]
    assert rounds[0].tokens_in == 100 and rounds[0].model == "haiku"
    assert rounds[3].model == "sonnet"


def test_build_round_metrics_handles_unsorted_input():
    """token_log が time 順だが round 順でない場合も、round 順に並べ直されること。"""
    token_log = [
        {"role": "orchestrator", "model": "haiku", "tokens_in": 100, "tokens_out": 30, "latency_ms": 800},
        {"role": "routing", "round": 2, "model": "haiku", "tokens_in": 600, "tokens_out": 180, "latency_ms": 2200},
        {"role": "fw", "round": 1, "model": "haiku", "tokens_in": 500, "tokens_out": 150, "latency_ms": 2000},
        {"role": "integrator", "model": "sonnet", "tokens_in": 2000, "tokens_out": 400, "latency_ms": 4500},
    ]
    rounds = _build_round_metrics(token_log)
    assert [r.role for r in rounds] == ["orchestrator", "fw", "routing", "integrator"]


def test_build_round_metrics_empty_input():
    assert _build_round_metrics([]) == []


def test_round_metrics_attached_in_analysis_result():
    """rally_agent の _build_analysis_result を直接叩いて round_metrics が入ることを確認。"""
    from log_analyzer.rally_agent import _build_analysis_result
    token_log = [
        {"role": "orchestrator", "model": "haiku", "tokens_in": 100, "tokens_out": 30, "latency_ms": 800},
        {"role": "fw", "round": 1, "model": "haiku", "tokens_in": 500, "tokens_out": 150, "latency_ms": 2000},
        {"role": "integrator", "model": "sonnet", "tokens_in": 2000, "tokens_out": 400, "latency_ms": 4500},
    ]
    integrator_result = {
        "root_cause_candidates": [],
        "recommended_actions": [],
        "confidence": 0.7,
    }
    ar = _build_analysis_result(
        log_ref="test",
        trace_id="t-1",
        integrator_result=integrator_result,
        token_log=token_log,
        delegation_history=[],
        rally_round=1,
        rally_max_rounds=3,
        wall_ms=7300,
    )
    assert len(ar.round_metrics) == 3
    assert isinstance(ar.round_metrics[0], RoundMetrics)
    assert ar.round_metrics[0].role == "orchestrator"
    assert ar.round_metrics[1].role == "fw"
    assert ar.round_metrics[2].role == "integrator"
