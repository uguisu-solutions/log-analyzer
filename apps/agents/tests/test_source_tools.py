"""ソース オンデマンド参照ツール（rally/source_tools.py）の単体テスト。

LLM は不要。runtime を直接組み立てて search/read/db_schema・予算・重複ガード・
注入ブロック・SourceContext 生成を検証する。
"""
from __future__ import annotations

from pathlib import Path

from log_analyzer.rally import source_tools as st
from log_analyzer.source.db_schema import extract_db_schema
from log_analyzer.source.indexer import build_source_index


def _codebase(root: Path) -> None:
    (root / "app").mkdir(parents=True, exist_ok=True)
    (root / "app" / "charge.py").write_text(
        "class Payment:\n"
        "    def charge(self, amount):\n"
        "        return amount\n"
        "def helper(x):\n"
        "    return x * 2\n",
        encoding="utf-8",
    )
    (root / "schema.sql").write_text(
        "CREATE TABLE payments (id BIGINT PRIMARY KEY, "
        "user_id BIGINT NOT NULL REFERENCES users(id));",
        encoding="utf-8",
    )


def _runtime(tmp_path: Path) -> dict:
    _codebase(tmp_path)
    index = build_source_index(tmp_path)
    schema = extract_db_schema(tmp_path)
    return st.make_source_runtime(index, schema, codebase="cb")


def test_has_source_tools_and_schemas(tmp_path: Path):
    rt = _runtime(tmp_path)
    assert st.has_source_tools(rt) is True
    names = {t["name"] for t in st.source_tool_schemas(rt)}
    assert names == {"source_search", "source_read", "db_schema"}
    assert st.has_source_tools(None) is False
    # 空 runtime（index も schema も無し）はツール無効
    empty = st.make_source_runtime(None, None)
    assert st.has_source_tools(empty) is False
    assert st.source_tool_schemas(empty) == []


def test_source_search_returns_signatures_only(tmp_path: Path):
    rt = _runtime(tmp_path)
    out = st.run_source_search({"query": "charge payment"}, rt)
    assert "app/charge.py" in out
    assert "Payment.charge" in out
    # 本文は返さない
    assert "return amount" not in out
    assert rt["tool_calls"][-1]["tool"] == "source_search"


def test_source_read_slices_symbol_and_records(tmp_path: Path):
    rt = _runtime(tmp_path)
    rt["current_node"] = "app"
    rt["current_round"] = 2
    out = st.run_source_read({"path": "app/charge.py", "symbol": "Payment.charge"}, rt)
    assert "def charge" in out
    assert "def helper" not in out
    call = rt["tool_calls"][-1]
    assert call["tool"] == "source_read"
    assert call["node"] == "app"
    assert call["round"] == 2
    assert call["result_chars"] > 0


def test_source_read_dedup_guard(tmp_path: Path):
    rt = _runtime(tmp_path)
    first = st.run_source_read({"path": "app/charge.py", "symbol": "helper"}, rt)
    assert "def helper" in first
    second = st.run_source_read({"path": "app/charge.py", "symbol": "helper"}, rt)
    assert "取得済み" in second  # 2 回目は本文を再送しない


def test_source_read_run_budget_exhaustion(tmp_path: Path, monkeypatch):
    rt = _runtime(tmp_path)
    rt["remaining"] = 0  # 予算を使い切った状態
    out = st.run_source_read({"path": "app/charge.py", "symbol": "helper"}, rt)
    assert "上限に達しました" in out


def test_source_read_decrements_budget(tmp_path: Path):
    rt = _runtime(tmp_path)
    before = rt["remaining"]
    st.run_source_read({"path": "app/charge.py", "symbol": "Payment.charge"}, rt)
    assert rt["remaining"] < before


def test_db_schema_tool_detail(tmp_path: Path):
    rt = _runtime(tmp_path)
    out = st.run_db_schema_tool({"table": "payments"}, rt)
    assert "table: payments" in out
    assert "user_id" in out
    assert "FK→users.id" in out


def test_dispatch_unknown_tool(tmp_path: Path):
    rt = _runtime(tmp_path)
    assert "未知" in st.dispatch_source_tool("nope", {}, rt)


def test_injection_block_marker_and_schema_summary(tmp_path: Path):
    _codebase(tmp_path)
    index = build_source_index(tmp_path)
    schema = extract_db_schema(tmp_path)
    block = st.build_source_injection_block("cb", index, schema)
    assert "ソースコード（オンデマンド取得）" in block
    assert "source_search" in block
    assert "DB スキーマ（要約）" in block
    assert "payments" in block
    # 本文（コード）は注入されない
    assert "def charge" not in block


def test_injection_block_empty_when_no_source():
    assert st.build_source_injection_block("cb", None, None) == ""


def test_build_and_merge_source_context(tmp_path: Path):
    rt = _runtime(tmp_path)
    st.run_source_read({"path": "app/charge.py", "symbol": "helper"}, rt)
    ctx = st.build_source_context(rt)
    assert ctx is not None
    assert ctx.codebase == "cb"
    assert ctx.file_count == 1
    assert ctx.db_schema is not None
    assert len(ctx.tool_calls) == 1
    assert ctx.total_chars_fetched > 0

    # マージ（2 段階モード相当）
    rt2 = _runtime(tmp_path)
    st.run_source_search({"query": "payment"}, rt2)
    ctx2 = st.build_source_context(rt2)
    merged = st.merge_source_contexts([ctx, ctx2])
    assert len(merged.tool_calls) == 2
    assert merged.total_chars_fetched == ctx.total_chars_fetched + ctx2.total_chars_fetched
