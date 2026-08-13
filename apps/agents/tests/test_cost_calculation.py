"""確認事項 D-2 (metrics.cost_usd の実計算) のテスト。

従来 cost_usd は常に 0.0 で「当てにならない」値だった。Langfuse 送信用にしか
コスト計算が無かったため、同じ計算を共有して結果にも載せる。
範囲は tokens と揃えて **本解析のみ** (プランナー・監査は UI が別枠表示)。
"""
from __future__ import annotations

from log_analyzer.rally_agent import _build_analysis_result, _build_round_metrics
from log_analyzer.tracing import cost_usd


def _entry(role: str, model: str, tin: int, tout: int, *, cc: int = 0, cr: int = 0, rnd: int | None = None):
    e = {
        "role": role, "model": model, "tokens_in": tin, "tokens_out": tout,
        "latency_ms": 1000, "cache_creation": cc, "cache_read": cr,
    }
    if rnd is not None:
        e["round"] = rnd
    return e


# ─── tracing.cost_usd ───────────────────────────────────────────────


def test_cost_usd_without_cache():
    """キャッシュ無し: opus は $5 / $25 per MTok。"""
    c = cost_usd("claude-opus-4-7", 1_000_000, 1_000_000)
    assert abs(c - 30.0) < 1e-9


def test_cost_usd_with_cache_is_cheaper():
    """キャッシュ読み出しは 1/10 単価。同じトークン数でも大幅に安くなる。"""
    plain = cost_usd("claude-opus-4-7", 1_000_000, 0)
    cached = cost_usd("claude-opus-4-7", 1_000_000, 0, cache_read=1_000_000)
    assert abs(plain - 5.0) < 1e-9
    assert abs(cached - 0.5) < 1e-9  # 5.0 の 1/10
    assert cached < plain


def test_cost_usd_cache_write_multiplier():
    """キャッシュ書き込みは 1.25 倍。"""
    c = cost_usd("claude-opus-4-7", 1_000_000, 0, cache_creation=1_000_000)
    assert abs(c - 6.25) < 1e-9


def test_cost_usd_unknown_model_returns_none():
    """単価未登録は None。0 (無料) と区別できるようにする。"""
    assert cost_usd("mystery-model-9", 1000, 1000) is None


# ─── RoundMetrics: キャッシュ内訳とノード別コスト (C-1) ─────────────


def test_round_metrics_carry_cache_breakdown_and_cost():
    token_log = [
        _entry("orchestrator", "claude-opus-4-7", 100_000, 1_000, cc=20_000, cr=70_000),
        _entry("fw", "claude-opus-4-7", 200_000, 2_000, cr=150_000, rnd=1),
        _entry("integrator", "claude-opus-4-7", 50_000, 3_000),
    ]
    rounds = _build_round_metrics(token_log)
    assert [r.role for r in rounds] == ["orchestrator", "fw", "integrator"]
    assert rounds[0].cache_creation == 20_000 and rounds[0].cache_read == 70_000
    assert rounds[1].cache_read == 150_000
    # ノード別コストが入り、キャッシュを踏まえた値になっている
    assert all(r.cost_usd is not None and r.cost_usd > 0 for r in rounds)
    naive = cost_usd("claude-opus-4-7", 200_000, 2_000)
    assert rounds[1].cost_usd < naive  # キャッシュ読み出し分だけ安い


# ─── metrics.cost_usd の集計 ────────────────────────────────────────


def _result(token_log: list[dict]):
    return _build_analysis_result(
        log_ref="test", trace_id="t-1",
        integrator_result={"root_cause_candidates": [], "recommended_actions": [], "confidence": 0.7},
        token_log=token_log, delegation_history=[], rally_round=1, rally_max_rounds=3, wall_ms=1000,
    )


def test_metrics_cost_is_sum_of_rounds():
    token_log = [
        _entry("orchestrator", "claude-opus-4-7", 100_000, 1_000),
        _entry("fw", "claude-opus-4-7", 200_000, 2_000, rnd=1),
        _entry("integrator", "claude-opus-4-7", 50_000, 3_000),
    ]
    ar = _result(token_log)
    expected = sum(r.cost_usd for r in ar.round_metrics)
    assert ar.metrics.cost_usd > 0
    assert abs(ar.metrics.cost_usd - expected) < 1e-9


def test_unpriced_model_is_flagged_not_silently_zero():
    """単価未登録のモデルが混ざったら info_loss_flags に残す (0 を無料と誤読させない)。"""
    token_log = [
        _entry("orchestrator", "claude-opus-4-7", 10_000, 100),
        _entry("fw", "mystery-model-9", 500_000, 5_000, rnd=1),
    ]
    ar = _result(token_log)
    flags = [f for f in ar.info_loss_flags if f.startswith("cost_unpriced_models:")]
    assert flags and "mystery-model-9" in flags[0]
    # 既知モデル分だけが合計に入る
    known = cost_usd("claude-opus-4-7", 10_000, 100)
    assert abs(ar.metrics.cost_usd - known) < 1e-9


def test_two_stage_sums_cost_of_both_stages():
    from log_analyzer.rally_two_stage import _build_final_result, _result_to_stage_output

    s1 = _result_to_stage_output("config", _result([_entry("fw", "claude-opus-4-7", 100_000, 1_000, rnd=1)]))
    s2 = _result_to_stage_output("log", _result([_entry("fw", "claude-opus-4-7", 300_000, 2_000, rnd=1)]))
    final = _build_final_result(stage_outputs=[s1, s2], trace_id="t", log_ref="inline")
    assert s1.cost_usd and s2.cost_usd
    assert abs(final.metrics.cost_usd - (s1.cost_usd + s2.cost_usd)) < 1e-9


def test_old_results_without_cost_fields_still_parse():
    """対応前の履歴 (cache 内訳・cost 無し) もそのまま読める。"""
    from log_analyzer.schema import AnalysisResult

    old = {
        "trace_id": "t", "config_id": "config4", "input_log_ref": "inline",
        "root_cause_candidates": [], "recommended_actions": [], "confidence": 0.5,
        "round_metrics": [{"round": 0, "role": "orchestrator", "model": "claude-opus-4-7",
                           "tokens_in": 100, "tokens_out": 10, "latency_ms": 5}],
    }
    ar = AnalysisResult.model_validate(old)
    assert ar.metrics.cost_usd == 0.0
    assert ar.round_metrics[0].cost_usd is None  # 未計算と分かる
    assert ar.round_metrics[0].cache_read == 0
