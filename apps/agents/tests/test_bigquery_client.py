"""bigquery_client の単体テスト (MCP 実接続なし・モック)。

検証:
- build_query: host/期間/contains を安全にエスケープしてリテラル化する
  (列名は識別子検証、引用符はエスケープして breakout を防ぐ)
- limit のクランプ (既定 / 上限)
- テーブル名の正規化と不正名の拒否
- query_logs / sample_rows: dry_run でスキャン量上限を確認し MCP 経由で実行
- table_schema: INFORMATION_SCHEMA.COLUMNS を SELECT して列定義を返す
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from log_analyzer import bigquery_client as bq
from log_analyzer import bigquery_mcp as bq_mcp


# ─── build_query: リテラル化とエスケープ ────────────────────────────


def test_build_query_inlines_and_escapes_host_and_contains():
    sql = bq.build_query("fw-01", contains="DENY' OR 1=1")
    # host はリテラルとして埋まり、列名はバッククォートで囲まれる
    assert "`host` = 'fw-01'" in sql
    # contains の引用符はエスケープされ、breakout しない (\' に変換)
    assert "CONTAINS_SUBSTR(`message`, 'DENY\\' OR 1=1')" in sql


def test_build_query_time_window_literals():
    sql = bq.build_query("fw-01", start="2026-06-10T09:00:00Z", end="2026-06-10T10:00:00Z")
    assert "`timestamp` >= TIMESTAMP('2026-06-10T09:00:00Z')" in sql
    assert "`timestamp` <= TIMESTAMP('2026-06-10T10:00:00Z')" in sql


def test_build_query_requires_host():
    with pytest.raises(ValueError):
        bq.build_query("")


def test_limit_clamped_to_default_and_max(monkeypatch):
    monkeypatch.setenv("BIGQUERY_DEFAULT_LIMIT", "500")
    monkeypatch.setenv("BIGQUERY_MAX_LIMIT", "2000")
    # 未指定 → 既定
    assert "LIMIT 500" in bq.build_query("h")
    # 上限超 → クランプ
    assert "LIMIT 2000" in bq.build_query("h", limit=999999)
    # 範囲内 → そのまま
    assert "LIMIT 10" in bq.build_query("h", limit=10)


def test_normalize_table_default_and_explicit(monkeypatch):
    monkeypatch.delenv("BIGQUERY_PROJECT", raising=False)
    monkeypatch.setenv("BIGQUERY_DATASET", "network_logs")
    monkeypatch.setenv("BIGQUERY_LOGS_TABLE", "device_logs")
    assert "`network_logs.device_logs`" in bq.build_query("h")
    # project を付けると 3 階層に
    monkeypatch.setenv("BIGQUERY_PROJECT", "proj-x")
    assert "`proj-x.network_logs.device_logs`" in bq.build_query("h")


def test_normalize_table_rejects_injection():
    with pytest.raises(ValueError):
        bq.build_query("h", table="logs`; DROP TABLE x; --")


def test_build_query_no_host_column_skips_filter_and_maps_columns():
    # host 列が無いテーブル (1 表 1 機器): host で絞らず、別名の列で絞る
    sql = bq.build_query(
        "ADServer",
        host_column="", time_column="timestamp", text_column="log_message",
        contains="4768",
        select_columns=["timestamp", "log_source", "log_message"],
    )
    assert "host" not in sql  # host で絞らない (列名は日本語等もあるので host 不在を確認)
    assert "SELECT `timestamp`, `log_source`, `log_message`" in sql
    assert "CONTAINS_SUBSTR(`log_message`, '4768')" in sql
    assert "ORDER BY `timestamp`" in sql


def test_build_query_select_all_when_columns_omitted():
    assert "SELECT *" in bq.build_query("ADServer", host_column="")


def test_build_query_no_time_column_rejects_window():
    with pytest.raises(ValueError):
        bq.build_query("ADServer", host_column="", time_column="",
                       start="2025-11-12T00:00:00Z")


def test_build_query_rejects_backtick_identifiers():
    # 識別子はバッククォートで囲むため、バッククォートを含む名前はクォート破壊
    # =インジェクションになるので拒否する
    with pytest.raises(ValueError):
        bq.build_query("h", select_columns=["ok", "bad`; DROP TABLE x"])
    with pytest.raises(ValueError):
        bq.build_query("h", host_column="evil`col")


def test_build_query_quotes_nonascii_column():
    # 日本語 (半角カナ) 列名でもバッククォートで安全に埋め込める
    sql = bq.build_query("ADServer", host_column="", time_column="ﾀｲﾑｽﾀﾝﾌﾟ",
                         start="2025-11-12T00:00:00Z")
    assert "`ﾀｲﾑｽﾀﾝﾌﾟ` >= TIMESTAMP(" in sql


# ─── query_logs: dry_run 上限確認 + MCP 実行 ────────────────────────


def test_query_logs_runs_via_mcp_and_inlines_values(monkeypatch):
    monkeypatch.setenv("BIGQUERY_MAX_BYTES_BILLED", "1000000")
    captured = {}
    rows = [{"host": "fw-01", "timestamp": "2026-06-10T09:00:00", "message": "deny"}]

    def fake_execute(sql, *, dry_run=False):
        if dry_run:
            return {"totalBytesProcessed": 10}
        captured["sql"] = sql
        return rows

    monkeypatch.setattr(bq_mcp, "execute_sql", fake_execute)

    out = bq.query_logs("fw-01", contains="deny", limit=5)
    assert out == rows
    assert "`host` = 'fw-01'" in captured["sql"]
    assert "CONTAINS_SUBSTR(`message`, 'deny')" in captured["sql"]
    assert "LIMIT 5" in captured["sql"]


def test_query_logs_aborts_when_scan_exceeds_limit(monkeypatch):
    monkeypatch.setenv("BIGQUERY_MAX_BYTES_BILLED", "1000")

    def fake_execute(sql, *, dry_run=False):
        if dry_run:
            return {"totalBytesProcessed": 10**9}
        raise AssertionError("上限超過なら本実行されないはず")

    monkeypatch.setattr(bq_mcp, "execute_sql", fake_execute)
    with pytest.raises(RuntimeError) as ei:
        bq.query_logs("fw-01")
    assert "上限" in str(ei.value)


def test_query_logs_proceeds_when_dry_run_fails(monkeypatch):
    # dry_run が落ちても本実行に委ねる (過剰に止めない)
    rows = [{"host": "fw-01", "message": "x"}]

    def fake_execute(sql, *, dry_run=False):
        if dry_run:
            raise RuntimeError("dry_run 未対応")
        return rows

    monkeypatch.setattr(bq_mcp, "execute_sql", fake_execute)
    assert bq.query_logs("fw-01") == rows


def test_query_logs_isoformats_datetime(monkeypatch):
    rows = [{"host": "fw-01",
             "timestamp": SimpleNamespace(isoformat=lambda: "2026-06-10T09:00:00+00:00"),
             "message": "x"}]

    def fake_execute(sql, *, dry_run=False):
        return {} if dry_run else rows

    monkeypatch.setattr(bq_mcp, "execute_sql", fake_execute)
    out = bq.query_logs("fw-01")
    assert out[0]["timestamp"] == "2026-06-10T09:00:00+00:00"


# ─── スキーマ確認 / サンプル取得 ────────────────────────────────────


def test_table_schema_queries_information_schema(monkeypatch):
    monkeypatch.delenv("BIGQUERY_PROJECT", raising=False)
    captured = {}

    def fake_execute(sql, *, dry_run=False):
        captured["sql"] = sql
        return [{"name": "timestamp", "type": "TIMESTAMP"},
                {"name": "log_message", "type": "STRING"}]

    monkeypatch.setattr(bq_mcp, "execute_sql", fake_execute)
    out = bq.table_schema(table="logs.ad021_case2")
    assert out == [{"name": "timestamp", "type": "TIMESTAMP"},
                   {"name": "log_message", "type": "STRING"}]
    assert "`logs.INFORMATION_SCHEMA.COLUMNS`" in captured["sql"]
    assert "table_name = 'ad021_case2'" in captured["sql"]


def test_sample_rows_clamps_limit_and_runs_via_mcp(monkeypatch):
    monkeypatch.setenv("BIGQUERY_MAX_BYTES_BILLED", "999999")
    captured = {}
    rows = [{"timestamp": SimpleNamespace(isoformat=lambda: "2025-11-12T00:00:00+00:00"),
             "log_message": "x"}]

    def fake_execute(sql, *, dry_run=False):
        if dry_run:
            return {"totalBytesProcessed": 1}
        captured["sql"] = sql
        return rows

    monkeypatch.setattr(bq_mcp, "execute_sql", fake_execute)
    out = bq.sample_rows(table="logs.ad021_case2", limit=999)
    assert out[0]["timestamp"] == "2025-11-12T00:00:00+00:00"  # 日時は ISO 文字列化
    assert "LIMIT 10" in captured["sql"]  # 999 → 上限 10 にクランプ
    assert "logs.ad021_case2" in captured["sql"]  # dataset 込みで正規化されている
