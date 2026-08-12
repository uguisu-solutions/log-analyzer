"""失敗・中断の実行履歴記録 (確認事項 B-4) のテスト。

従来は「解析が正常に final まで到達したとき」しか履歴が残らず、
バリデーションエラー・方針却下・途中例外・中断は解析履歴にも実行履歴にも
残らなかった。run_history に status / error_stage / error_message を足して
残す対応の検証。
"""
from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

from log_analyzer import storage


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    """テスト用に SQLite を差し替えて初期化する。"""
    db = tmp_path / "results.sqlite3"
    monkeypatch.setattr(storage, "_DB_PATH", db)
    storage.init_db()
    return db


# ─── マイグレーション ───────────────────────────────────────────────


def test_init_db_adds_status_columns_to_existing_table(tmp_path, monkeypatch):
    """status を持たない旧スキーマの DB でも、init_db で列が追加され既存行が 'ok' になること。"""
    db = tmp_path / "old.sqlite3"
    # 対応前のスキーマ (status / error_stage / error_message 無し) を再現
    con = sqlite3.connect(db)
    con.execute(
        """
        CREATE TABLE run_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at TEXT NOT NULL,
            log_name TEXT NOT NULL,
            config_id TEXT NOT NULL,
            base_config TEXT NOT NULL,
            confidence REAL,
            tokens_in INTEGER,
            tokens_out INTEGER,
            latency_ms INTEGER,
            trace_id TEXT,
            top_category TEXT,
            top_summary TEXT
        )
        """
    )
    con.execute(
        "INSERT INTO run_history (started_at, log_name, config_id, base_config) "
        "VALUES ('2026-01-01T00:00:00+00:00', 'old.log', 'config4', 'config4')"
    )
    con.commit()
    con.close()

    monkeypatch.setattr(storage, "_DB_PATH", db)
    storage.init_db()

    rows, total = storage.list_run_history()
    assert total == 1
    assert rows[0]["status"] == "ok"  # 既存行は正常終了扱い
    assert rows[0]["error_stage"] is None

    # 冪等: もう一度呼んでも失敗しない
    storage.init_db()
    assert storage.list_run_history()[1] == 1


# ─── storage: status 付き記録 ───────────────────────────────────────


def test_insert_and_filter_by_status(temp_db):
    common = dict(
        log_name="a.log", config_id="config4", base_config="config4",
        confidence=None, tokens_in=0, tokens_out=0, latency_ms=None,
        trace_id=None, top_category=None, top_summary=None,
    )
    storage.insert_run_history(**common)  # 既定 status="ok"
    storage.insert_run_history(
        **common, status="error", error_stage="monitor:fw", error_message="boom"
    )
    storage.insert_run_history(
        **common, status="rejected", error_stage="policy", error_message="方針却下"
    )

    rows, total = storage.list_run_history()
    assert total == 3
    assert {r["status"] for r in rows} == {"ok", "error", "rejected"}

    errors, n = storage.list_run_history(status="error")
    assert n == 1
    assert errors[0]["error_stage"] == "monitor:fw"
    assert errors[0]["error_message"] == "boom"


# ─── API: バリデーションエラーも残す ────────────────────────────────


def test_validation_error_is_recorded(temp_db, monkeypatch):
    """解析エンドポイントの 422 (LLM 実行前の失敗) が実行履歴に残ること。"""
    from log_analyzer import api

    client = TestClient(api.app)
    # topology / analysis_mode 等の必須項目を欠いたリクエスト
    res = client.post("/api/runs/config-log-stream", json={})
    assert res.status_code == 422

    rows, total = storage.list_run_history(status="error")
    assert total == 1
    assert rows[0]["error_stage"] == "validation"
    assert rows[0]["log_name"] == "(validation error)"


def test_pre_run_400_is_recorded(temp_db):
    """解析前の 400 (config4 以外を指定など) も validation として残ること。"""
    from log_analyzer import api

    client = TestClient(api.app)
    res = client.post(
        "/api/runs/config-log-stream",
        json={
            "config": "config1",  # config4 以外 → 400
            "topology": {"nodes": [{"id": "n1"}], "links": []},
            "node_configs": {"n1": [{"name": "x.conf", "content": "x"}]},
        },
    )
    assert res.status_code == 400
    rows, total = storage.list_run_history(status="error")
    assert total == 1
    assert rows[0]["error_stage"] == "validation"


def test_stream_exception_is_recorded(temp_db, monkeypatch):
    """解析の途中例外が status='error' として残ること (従来は何も残らなかった)。"""
    from log_analyzer import api

    async def _boom(*args, **kwargs):
        raise RuntimeError("rally が壊れた")
        yield  # pragma: no cover — async generator にするため

    monkeypatch.setattr(api, "run_rally_stream", _boom)

    client = TestClient(api.app)
    with client.stream(
        "POST",
        "/api/runs/config-log-stream",
        json={
            "config": "config4",
            "analysis_mode": "single",
            "single_source": "log",
            "topology": {"nodes": [{"id": "fw-01"}], "links": []},
            "node_logs": {"fw-01": [{"name": "fw.log", "content": "deny ..."}]},
        },
    ) as res:
        body = "".join(res.iter_text())

    assert "error" in body and "rally が壊れた" in body
    rows, total = storage.list_run_history(status="error")
    assert total == 1
    assert rows[0]["error_stage"] == "config-log-stream"
    assert "rally が壊れた" in rows[0]["error_message"]


def test_successful_run_is_not_marked_failed(temp_db, monkeypatch):
    """正常終了は status='ok' で 1 行だけ (中断記録と二重に入らないこと)。"""
    from log_analyzer import api
    from log_analyzer.rally_agent import StreamEvent

    async def _fake_stream(*args, **kwargs):
        yield StreamEvent("run_started", {"trace_id": "t-1", "rally_max_rounds": 3})
        yield StreamEvent(
            "final",
            {
                "result": {
                    "trace_id": "t-1",
                    "config_id": "config4",
                    "input_log_ref": "inline",
                    "root_cause_candidates": [],
                    "recommended_actions": [],
                    "confidence": 0.6,
                    "metrics": {"tokens_in": 10, "tokens_out": 5, "latency_ms_total": 100},
                }
            },
        )

    monkeypatch.setattr(api, "run_rally_stream", _fake_stream)

    client = TestClient(api.app)
    with client.stream(
        "POST",
        "/api/runs/config-log-stream",
        json={
            "config": "config4",
            "analysis_mode": "single",
            "single_source": "log",
            "topology": {"nodes": [{"id": "fw-01"}], "links": []},
            "node_logs": {"fw-01": [{"name": "fw.log", "content": "deny ..."}]},
        },
    ) as res:
        "".join(res.iter_text())

    rows, total = storage.list_run_history()
    assert total == 1
    assert rows[0]["status"] == "ok"


def test_validation_error_on_other_endpoint_is_not_recorded(temp_db):
    """解析以外のエンドポイントのバリデーションエラーは記録しない (ノイズ回避)。"""
    from log_analyzer import api

    client = TestClient(api.app)
    res = client.get("/api/runs/history?limit=0")
    assert res.status_code == 400
    assert storage.list_run_history()[1] == 0


def test_status_failed_filter_groups_all_failures(temp_db):
    """status='failed' で ok 以外 (error / aborted / rejected) をまとめて取れること。"""
    common = dict(
        log_name="a.log", config_id="config4", base_config="config4",
        confidence=None, tokens_in=0, tokens_out=0, latency_ms=None,
        trace_id=None, top_category=None, top_summary=None,
    )
    storage.insert_run_history(**common)
    storage.insert_run_history(**common, status="error", error_stage="s", error_message="m")
    storage.insert_run_history(**common, status="aborted", error_stage="client", error_message="m")
    storage.insert_run_history(**common, status="rejected", error_stage="policy", error_message="m")

    rows, total = storage.list_run_history(status="failed")
    assert total == 3
    assert {r["status"] for r in rows} == {"error", "aborted", "rejected"}
