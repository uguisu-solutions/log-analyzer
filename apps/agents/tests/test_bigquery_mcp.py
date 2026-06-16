"""bigquery_mcp の単体テスト (実サーバー / mcp パッケージ不要)。

CallToolResult の解釈・行正規化・execute_sql の引数組み立てを検証する。
ランタイム (stdio サブプロセス) はフェイクに差し替える。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from log_analyzer import bigquery_mcp as mcp


def _text_block(text):
    return SimpleNamespace(type="text", text=text)


def test_parse_result_json_array():
    result = SimpleNamespace(content=[_text_block('[{"a": 1}, {"a": 2}]')], isError=False)
    assert mcp._parse_result(result) == [{"a": 1}, {"a": 2}]


def test_parse_result_collects_per_row_blocks():
    # toolbox は 1 行 = 1 ブロック (JSON オブジェクト) で返す
    result = SimpleNamespace(
        content=[_text_block('{"a": 1}'), _text_block('{"a": 2}')], isError=False
    )
    assert mcp._parse_result(result) == [{"a": 1}, {"a": 2}]


def test_parse_result_prefers_structured_content():
    result = SimpleNamespace(content=[_text_block("ignored")],
                             isError=False, structuredContent=[{"a": 1}])
    assert mcp._parse_result(result) == [{"a": 1}]


def test_parse_result_raises_on_error():
    result = SimpleNamespace(content=[_text_block("boom")], isError=True)
    with pytest.raises(RuntimeError) as ei:
        mcp._parse_result(result)
    assert "boom" in str(ei.value)


def test_parse_result_returns_raw_text_when_not_json():
    result = SimpleNamespace(content=[_text_block("not json")], isError=False)
    assert mcp._parse_result(result) == "not json"


def test_rows_normalizes_list_and_wrapped_dict():
    assert mcp._rows([{"a": 1}, "skip", {"b": 2}]) == [{"a": 1}, {"b": 2}]
    assert mcp._rows({"rows": [{"a": 1}]}) == [{"a": 1}]
    assert mcp._rows({"results": [{"a": 1}]}) == [{"a": 1}]
    assert mcp._rows("nope") == []


def test_execute_sql_passes_dry_run_flag(monkeypatch):
    calls = []

    class _FakeRuntime:
        def call_tool(self, name, arguments):
            calls.append((name, arguments))
            return [{"host": "fw-01"}]

    monkeypatch.setattr(mcp, "_runtime", lambda: _FakeRuntime())
    monkeypatch.setenv("BIGQUERY_MCP_EXECUTE_TOOL", "bigquery-execute-sql")

    out = mcp.execute_sql("SELECT 1")
    assert out == [{"host": "fw-01"}]
    name, args = calls[-1]
    assert name == "bigquery-execute-sql"
    assert args == {"sql": "SELECT 1"}  # dry_run なしでは付かない

    mcp.execute_sql("SELECT 1", dry_run=True)
    _, args2 = calls[-1]
    assert args2["dry_run"] is True


def test_args_list_default_and_custom(monkeypatch):
    # 環境変数未設定なら既定 (prebuilt)、設定時はそれを分割
    monkeypatch.delenv("BIGQUERY_MCP_ARGS", raising=False)
    assert mcp.args_list() == ["--prebuilt", "bigquery", "--stdio"]
    monkeypatch.setenv("BIGQUERY_MCP_ARGS", "--config foo.yaml --stdio")
    assert mcp.args_list() == ["--config", "foo.yaml", "--stdio"]
