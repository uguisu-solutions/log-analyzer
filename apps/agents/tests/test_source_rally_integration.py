"""ソースツールと rally 監視ループの結線テスト（LLM はフェイク）。"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from log_analyzer.rally import monitors as mon_mod
from log_analyzer.rally import source_tools as st
from log_analyzer.source.db_schema import extract_db_schema
from log_analyzer.source.indexer import build_source_index


class _FakeMessages:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self._responses.pop(0)


class _FakeAnthropic:
    def __init__(self, responses):
        self.messages = _FakeMessages(responses)


def _tool_use_resp(name, tool_id, tool_input, tin=100, tout=20):
    block = SimpleNamespace(type="tool_use", name=name, id=tool_id, input=tool_input)
    return SimpleNamespace(
        stop_reason="tool_use",
        content=[block],
        usage=SimpleNamespace(input_tokens=tin, output_tokens=tout),
    )


def _text_resp(text, tin=80, tout=40):
    block = SimpleNamespace(type="text", text=text)
    return SimpleNamespace(
        stop_reason="end_turn",
        content=[block],
        usage=SimpleNamespace(input_tokens=tin, output_tokens=tout),
    )


def _runtime(tmp_path: Path) -> dict:
    (tmp_path / "app").mkdir(exist_ok=True)
    (tmp_path / "app" / "charge.py").write_text(
        "def charge(amount):\n    return amount\n", encoding="utf-8"
    )
    (tmp_path / "schema.sql").write_text(
        "CREATE TABLE payments (id BIGINT PRIMARY KEY);", encoding="utf-8"
    )
    index = build_source_index(tmp_path)
    schema = extract_db_schema(tmp_path)
    return st.make_source_runtime(index, schema, codebase="cb")


def test_monitor_uses_source_tools(tmp_path, monkeypatch):
    rt = _runtime(tmp_path)
    responses = [
        _tool_use_resp("source_search", "t1", {"query": "charge"}),
        _tool_use_resp("source_read", "t2", {"path": "app/charge.py", "symbol": "charge"}),
        _text_resp('{"findings": [], "next": "integrator"}'),
    ]
    fake = _FakeAnthropic(responses)
    monkeypatch.setattr(mon_mod.anthropic, "Anthropic", lambda: fake)

    text, tin, tout, cc, cr, calls, fetched = mon_mod._run_monitor_llm(
        model="claude-opus-4-7",
        system_prompt="sys",
        user_blocks=[{"type": "text", "text": "## ログ\n..."}],
        bq_sources={},
        source_runtime=rt,
    )
    assert '"next": "integrator"' in text
    # source ツールが実行され記録される
    tools_used = {c["tool"] for c in rt["tool_calls"]}
    assert tools_used == {"source_search", "source_read"}
    assert any("source_search" in c for c in calls)
    assert any("source_read" in c for c in calls)
    # tokens は 3 回分合算 (100+100+80, 20+20+40)
    assert tin == 280
    assert tout == 80
    # 1 回目の呼び出しに source ツールスキーマが渡っている
    tool_names = {t["name"] for t in fake.messages.calls[0]["tools"]}
    assert {"source_search", "source_read", "db_schema"} <= tool_names
    # ガイダンスが system に入る
    assert "ソースコード参照について" in fake.messages.calls[0]["system"][0]["text"]


def test_monitor_no_tools_when_no_source_no_bq(tmp_path, monkeypatch):
    fake = _FakeAnthropic([_text_resp('{"next": "integrator"}')])
    monkeypatch.setattr(mon_mod.anthropic, "Anthropic", lambda: fake)
    text, *_ = mon_mod._run_monitor_llm(
        model="m", system_prompt="sys",
        user_blocks=[{"type": "text", "text": "x"}],
        bq_sources={}, source_runtime=None,
    )
    assert '"next": "integrator"' in text
    assert "tools" not in fake.messages.calls[0]


def test_bq_and_source_tools_coexist(tmp_path, monkeypatch):
    """BQ ソースとソースコードが両方あれば両方のツールが渡る。"""
    rt = _runtime(tmp_path)
    fake = _FakeAnthropic([_text_resp('{"next": "integrator"}')])
    monkeypatch.setattr(mon_mod.anthropic, "Anthropic", lambda: fake)
    mon_mod._run_monitor_llm(
        model="m", system_prompt="sys",
        user_blocks=[{"type": "text", "text": "x"}],
        bq_sources={"fw-01": {"table": "t"}}, source_runtime=rt,
    )
    names = {t["name"] for t in fake.messages.calls[0]["tools"]}
    assert {"bigquery_query", "bigquery_schema"} <= names
    assert {"source_search", "source_read", "db_schema"} <= names
