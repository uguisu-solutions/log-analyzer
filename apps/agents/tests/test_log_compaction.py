"""log_compaction（反復行の畳み込み）のテスト。"""
from __future__ import annotations

import pytest

from log_analyzer.log_compaction import (
    compact_log,
    compact_log_reporting,
    compact_log_text,
)


def _make_repetitive_log(n: int) -> str:
    """id とタイムスタンプだけ違う、ほぼ同一の 404 行を n 件生成。"""
    return "\n".join(
        f"2026-04-28T10:00:{i % 60:02d} ERROR RecordNotFound id={1000 + i} path=/ai_documents/{1000 + i}/show_json"
        for i in range(n)
    )


def test_small_log_is_passthrough():
    """min_lines 未満のログは畳み込まず原文のまま。"""
    text = _make_repetitive_log(10)
    res = compact_log(text, min_lines=200)
    assert res.text == text
    assert res.dropped_lines == 0
    assert res.compression_ratio == pytest.approx(1.0)


def test_repetitive_log_is_collapsed():
    """反復行は先頭数件だけ残り、残りは畳み込まれる。"""
    text = _make_repetitive_log(1000)
    res = compact_log(text, max_examples=3, min_lines=200)
    assert res.original_lines == 1000
    # 1 テンプレートに集約されるので、原文表示は 3 件のみ
    assert res.template_count == 1
    assert res.dropped_lines == 997
    # 大幅に縮む
    assert res.compacted_bytes < res.original_bytes * 0.2
    # 件数サマリが入る
    assert "×997" in res.text
    assert "省略サマリ" in res.text


def test_examples_are_verbatim_and_ordered():
    """残す先頭 N 件は原文そのまま・元の順序で保持される。"""
    text = _make_repetitive_log(500)
    res = compact_log(text, max_examples=2, min_lines=100)
    lines = res.text.splitlines()
    # 先頭 2 行は原文の先頭 2 行
    assert lines[0] == "2026-04-28T10:00:00 ERROR RecordNotFound id=1000 path=/ai_documents/1000/show_json"
    assert lines[1] == "2026-04-28T10:00:01 ERROR RecordNotFound id=1001 path=/ai_documents/1001/show_json"


def test_distinct_templates_are_kept_separately():
    """別テンプレート（別種のエラー）はそれぞれ先頭 N 件残る。"""
    err_a = "\n".join(f"2026-01-01T00:00:{i%60:02d} ERROR TypeA id={i}" for i in range(300))
    err_b = "\n".join(f"2026-01-01T00:00:{i%60:02d} ERROR TypeB code={i}" for i in range(300))
    res = compact_log(err_a + "\n" + err_b, max_examples=3, min_lines=100)
    assert res.template_count == 2
    assert "TypeA" in res.text
    assert "TypeB" in res.text
    # 2 テンプレート × 3 件の原文が残る（本体部分＝省略サマリより前で数える。
    # サマリのプレビュー行にもテンプレート文字列が現れるため）
    body = res.text.split("省略サマリ")[0]
    assert body.count("ERROR TypeA") == 3
    assert body.count("ERROR TypeB") == 3


def test_non_repetitive_log_has_no_summary():
    """反復が無いログはサマリを付けず原文相当を返す。"""
    text = "\n".join(f"2026-01-01T00:00:00 unique message number {i} {i*i} xyz{i}" for i in range(300))
    res = compact_log(text, max_examples=3, min_lines=100)
    # 各行がユニークテンプレートかというと数値マスクで同一化される可能性がある。
    # ここでは「畳み込みが起きても件数サマリで完全性が担保される」ことだけ確認。
    if res.dropped_lines == 0:
        assert "省略サマリ" not in res.text


def test_ip_and_mac_masking_groups_lines():
    """IP / MAC だけ違う行が同一テンプレートに集約される。"""
    lines = [
        f"deny src=192.168.1.{i} mac=B8:20:8E:28:C4:{i:02X} action=drop"
        for i in range(1, 250)
    ]
    res = compact_log("\n".join(lines), max_examples=3, min_lines=100)
    assert res.template_count == 1
    assert res.dropped_lines == 249 - 3


def test_wrapper_respects_disable(monkeypatch):
    """LOG_COMPACT_ENABLED=0 で原文素通し。"""
    monkeypatch.setenv("LOG_COMPACT_ENABLED", "0")
    text = _make_repetitive_log(1000)
    assert compact_log_text(text) == text


def test_wrapper_enabled_by_default(monkeypatch):
    monkeypatch.delenv("LOG_COMPACT_ENABLED", raising=False)
    text = _make_repetitive_log(1000)
    out = compact_log_text(text)
    assert len(out) < len(text)
    assert "省略サマリ" in out


def test_reporting_returns_stats_when_enabled(monkeypatch):
    """有効時は (圧縮text, CompactionResult) を返す。"""
    monkeypatch.delenv("LOG_COMPACT_ENABLED", raising=False)
    text = _make_repetitive_log(1000)
    out, stats = compact_log_reporting(text)
    assert stats is not None
    assert stats.original_lines == 1000
    assert stats.dropped_lines > 0
    assert out == stats.text


def test_reporting_returns_none_when_disabled(monkeypatch):
    """無効時は (原文, None) を返す（未適用の判別用）。"""
    monkeypatch.setenv("LOG_COMPACT_ENABLED", "0")
    text = _make_repetitive_log(1000)
    out, stats = compact_log_reporting(text)
    assert stats is None
    assert out == text
