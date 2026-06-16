"""tracing の usage 集計・コスト計算テスト (prompt caching 対応)。"""
from __future__ import annotations

from types import SimpleNamespace

from log_analyzer.tracing import usage_components, usage_for


def test_usage_components_with_cache_fields():
    u = SimpleNamespace(input_tokens=16, cache_creation_input_tokens=30801,
                        cache_read_input_tokens=0, output_tokens=4)
    assert usage_components(u) == {"input": 16, "cache_creation": 30801,
                                   "cache_read": 0, "output": 4}


def test_usage_components_defaults_when_no_cache():
    u = SimpleNamespace(input_tokens=10, output_tokens=2)  # cache フィールド無し
    assert usage_components(u) == {"input": 10, "cache_creation": 0,
                                   "cache_read": 0, "output": 2}


def test_usage_for_reports_total_input_including_cache():
    # tokens_in は cache 込みの総量。Langfuse の input にもその総量を出す。
    u = usage_for("claude-opus-4-7", 1_000_000, 0,
                  cache_creation=600_000, cache_read=300_000)
    assert u["input"] == 1_000_000
    assert u["unit"] == "TOKENS"


def test_usage_for_cache_write_costs_1_25x():
    # 全量が cache 書込: 1.25x。opus-4-7 は $5/1M → 1.25M*5/1M = $6.25
    u = usage_for("claude-opus-4-7", 1_000_000, 0, cache_creation=1_000_000)
    assert abs(u["total_cost"] - 6.25) < 1e-9


def test_usage_for_cache_read_costs_0_1x():
    # 全量が cache 読出: 0.1x。$5/1M → 0.1M*5/1M = $0.5
    u = usage_for("claude-opus-4-7", 1_000_000, 0, cache_read=1_000_000)
    assert abs(u["total_cost"] - 0.5) < 1e-9


def test_usage_for_backward_compatible_without_cache():
    # cache 指定なし: 従来どおり全量 ×1.0。haiku $1/$5
    u = usage_for("claude-haiku-4-5", 1_000_000, 1_000_000)
    assert abs(u["input_cost"] - 1.0) < 1e-9
    assert abs(u["output_cost"] - 5.0) < 1e-9
    assert abs(u["total_cost"] - 6.0) < 1e-9


def test_usage_for_unknown_model_omits_cost():
    u = usage_for("gpt-5.5", 100, 50)
    assert u == {"input": 100, "output": 50, "unit": "TOKENS"}
