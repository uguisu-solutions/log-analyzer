"""BigQuery tool (rally) の単体テスト。

- run_bigquery_tool: host 許可リスト検証・整形・取得失敗時の graceful 動作
- monitors._run_monitor_llm: tool_use → bigquery_query 実行 → tool_result 再投入 →
  最終テキスト返却、トークン合算
- api._build_topology_log_text: BQ ノードは本文 inline されずマーカーが出る
"""
from __future__ import annotations

from types import SimpleNamespace

from log_analyzer import bigquery_client as bq_mod
from log_analyzer.rally import monitors as mon_mod
from log_analyzer.rally import tools as tools_mod
from log_analyzer.api import _build_topology_log_text, _normalize_bq_sources, _bq_allowlist


# ─── run_bigquery_tool ──────────────────────────────────────────────


def test_run_bigquery_tool_rejects_disallowed_host():
    out = tools_mod.run_bigquery_tool({"host": "evil"}, {"fw-01": {}})
    assert "許可されていません" in out
    assert "fw-01" in out  # 許可された host を案内


def test_run_bigquery_tool_formats_rows(monkeypatch):
    captured = {}

    def fake_query_logs(host, **kwargs):
        captured["host"] = host
        captured["kwargs"] = kwargs
        return [
            {"timestamp": "2026-06-10T09:00:00", "severity": "ERR", "message": "deny X"},
            {"timestamp": "2026-06-10T09:01:00", "severity": None, "message": "ok"},
        ]

    monkeypatch.setattr(bq_mod, "query_logs", fake_query_logs)
    allowed = {"fw-01": {"table": "device_logs", "start": "S", "end": "E", "limit": 100}}
    out = tools_mod.run_bigquery_tool({"host": "fw-01", "contains": "deny"}, allowed)
    assert "host=fw-01, 2 件" in out
    # 表形式: 列名はヘッダで 1 回、本文は値だけ
    assert "列: timestamp, severity, message" in out
    assert "ERR | deny X" in out
    assert captured["host"] == "fw-01"
    # allowed のデフォルト (table/start/end/limit) が補完される
    assert captured["kwargs"]["table"] == "device_logs"
    assert captured["kwargs"]["start"] == "S"
    assert captured["kwargs"]["contains"] == "deny"


def test_run_bigquery_tool_graceful_on_error(monkeypatch):
    def boom(host, **kwargs):
        raise RuntimeError("BQ down")

    monkeypatch.setattr(bq_mod, "query_logs", boom)
    out = tools_mod.run_bigquery_tool({"host": "fw-01"}, {"fw-01": {}})
    assert out.startswith("エラー")
    assert "BQ down" in out


def test_run_bigquery_tool_empty_result(monkeypatch):
    monkeypatch.setattr(bq_mod, "query_logs", lambda host, **k: [])
    out = tools_mod.run_bigquery_tool({"host": "fw-01"}, {"fw-01": {}})
    assert "見つかりませんでした" in out


def test_run_bigquery_tool_agent_overrides_columns(monkeypatch):
    captured = {}

    def fake_query_logs(host, **kwargs):
        captured.update(kwargs)
        return [{"timestamp": "t", "log_message": "m"}]

    monkeypatch.setattr(bq_mod, "query_logs", fake_query_logs)
    # ノード設定には列指定なし。エージェントが schema 確認後に列を明示するケース。
    allowed = {"ADServer": {"table": "logs.ad021_case2"}}
    tools_mod.run_bigquery_tool(
        {"host": "ADServer", "time_column": "timestamp",
         "text_column": "log_message", "columns": ["timestamp", "log_message"],
         "contains": "4768"},
        allowed,
    )
    assert captured["time_column"] == "timestamp"
    assert captured["text_column"] == "log_message"
    assert captured["select_columns"] == ["timestamp", "log_message"]


# ─── run_bigquery_schema_tool ───────────────────────────────────────


def test_run_bigquery_schema_tool_returns_columns_and_samples(monkeypatch):
    monkeypatch.setattr(
        bq_mod, "table_schema",
        lambda table=None: [{"name": "timestamp", "type": "TIMESTAMP"},
                            {"name": "log_message", "type": "STRING"}],
    )
    monkeypatch.setattr(
        bq_mod, "sample_rows",
        lambda table=None, **k: [{"timestamp": "2025-11-12T00:00:00", "log_message": "ev4768"}],
    )
    allowed = {"ADServer": {"table": "logs.ad021_case2"}}
    out = tools_mod.run_bigquery_schema_tool({"host": "ADServer"}, allowed)
    assert "timestamp: TIMESTAMP" in out
    assert "log_message: STRING" in out
    assert "logs.ad021_case2" in out
    assert "サンプル" in out
    assert "ev4768" in out


def test_run_bigquery_schema_tool_rejects_disallowed_host():
    out = tools_mod.run_bigquery_schema_tool({"host": "evil"}, {"fw-01": {}})
    assert "許可されていません" in out


def test_run_bigquery_schema_tool_returns_schema_even_if_sample_fails(monkeypatch):
    monkeypatch.setattr(bq_mod, "table_schema",
                        lambda table=None: [{"name": "timestamp", "type": "TIMESTAMP"}])

    def boom(table=None, **k):
        raise RuntimeError("scan denied")

    monkeypatch.setattr(bq_mod, "sample_rows", boom)
    out = tools_mod.run_bigquery_schema_tool({"host": "ADServer"}, {"ADServer": {"table": "t"}})
    assert "timestamp: TIMESTAMP" in out  # スキーマは返る
    assert "サンプル行の取得に失敗" in out


# ─── monitors._run_monitor_llm: tool-use ループ ─────────────────────


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


def _tool_use_resp(tool_id, tool_input, tin=100, tout=20):
    block = SimpleNamespace(type="tool_use", name="bigquery_query", id=tool_id, input=tool_input)
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


def test_run_monitor_llm_executes_tool_then_returns_text(monkeypatch):
    responses = [
        _tool_use_resp("t1", {"host": "fw-01", "contains": "deny"}),
        _text_resp('{"findings": [], "next": "integrator"}'),
    ]
    fake_client = _FakeAnthropic(responses)
    monkeypatch.setattr(mon_mod.anthropic, "Anthropic", lambda: fake_client)

    executed = {}

    def fake_tool(tool_input, allowed):
        executed["input"] = tool_input
        executed["allowed"] = allowed
        return "BigQuery 取得結果: host=fw-01, 1 件\nx"

    monkeypatch.setattr(mon_mod, "run_bigquery_tool", fake_tool)

    text, tin, tout, cc, cr, calls, fetched = mon_mod._run_monitor_llm(
        model="claude-opus-4-7",
        system_prompt="sys",
        user_blocks=[{"type": "text", "text": "## ログ\n..."}],
        bq_sources={"fw-01": {"table": "device_logs"}},
    )
    # 最終 JSON テキストが返る
    assert '"next": "integrator"' in text
    # tool が実行された
    assert executed["input"]["host"] == "fw-01"
    assert "fw-01" in executed["allowed"]
    # トークンは 2 回分合算 (100+80, 20+40)
    assert tin == 180
    assert tout == 60
    assert any("bigquery_query" in c for c in calls)
    # 取得した実ログが監査の証拠として捕捉される
    assert len(fetched) == 1
    assert fetched[0]["host"] == "fw-01"
    assert "host=fw-01" in fetched[0]["content"]
    # 1 回目は tools 付き、2 回目は tool_result を含む messages で呼ばれている
    assert "tools" in fake_client.messages.calls[0]


def test_run_monitor_llm_no_tools_when_no_bq(monkeypatch):
    fake_client = _FakeAnthropic([_text_resp('{"next": "integrator"}')])
    monkeypatch.setattr(mon_mod.anthropic, "Anthropic", lambda: fake_client)
    text, tin, tout, cc, cr, calls, fetched = mon_mod._run_monitor_llm(
        model="m", system_prompt="sys",
        user_blocks=[{"type": "text", "text": "x"}], bq_sources={},
    )
    assert '"next": "integrator"' in text
    assert calls == []  # BQ tool 未実行
    assert fetched == []  # 取得実ログも無し
    # tools は渡されない
    assert "tools" not in fake_client.messages.calls[0]


# ─── _build_topology_log_text: BQ マーカー注入 ──────────────────────


def test_build_topology_log_text_bq_marker_no_inline():
    topology = {"nodes": [{"id": "fw-01", "type": "FW"}], "links": []}
    text, _ = _build_topology_log_text(
        topology,
        node_logs={},  # アップロードログ無し
        node_bigquery={"fw-01": {"host": "fw-01", "start": "2026-06-10T09:00:00Z", "end": ""}},
    )
    assert "[ログ取得元: BigQuery]" in text
    assert 'bigquery_query' in text
    assert 'host="fw-01"' in text
    assert "既定期間" in text  # start が指定されているので期間表示


def test_normalize_and_allowlist_host_defaults_to_node_id():
    # 単一オブジェクト指定でも list に正規化される
    norm = _normalize_bq_sources({"fw-01": {"host": "", "table": "t"}})
    assert norm["fw-01"][0]["host"] == "fw-01"  # host 空 → node id
    allow = _bq_allowlist(norm)
    assert "fw-01" in allow
    assert allow["fw-01"][0]["table"] == "t"


def test_normalize_bq_sources_multiple_tables_per_node():
    """1 ノードに複数テーブル (list) を紐づけられる。"""
    norm = _normalize_bq_sources({
        "app": [
            {"host": "private-ap", "table": "logs.private"},
            {"host": "public-ap", "table": "logs.public"},
        ]
    })
    assert len(norm["app"]) == 2
    allow = _bq_allowlist(norm)
    assert set(allow) == {"private-ap", "public-ap"}
    assert allow["private-ap"][0]["table"] == "logs.private"


def test_bq_tool_resolves_by_host_and_table(monkeypatch):
    """同一 host に複数テーブルがあるとき table で解決する。"""
    allowed = {"h1": [{"table": "t_a"}, {"table": "t_b"}]}
    # table 未指定 → 複数あるので促すエラー
    out = tools_mod.run_bigquery_tool({"host": "h1"}, allowed)
    assert "複数のテーブル" in out
    # 許可外 table
    out = tools_mod.run_bigquery_tool({"host": "h1", "table": "t_x"}, allowed)
    assert "許可されていません" in out

    captured = {}

    def _fake_query(host, **kwargs):
        captured["table"] = kwargs.get("table")
        return [{"message": "ok"}]
    monkeypatch.setattr("log_analyzer.bigquery_client.query_logs", _fake_query)
    tools_mod.run_bigquery_tool({"host": "h1", "table": "t_b"}, allowed)
    assert captured["table"] == "t_b"
