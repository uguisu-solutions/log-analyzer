"""実行履歴 (storage + /api/runs/history) のテスト。"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from log_analyzer import api as api_mod
from log_analyzer import storage


@pytest.fixture
def tmp_db(tmp_path: Path, monkeypatch):
    """SQLite を一時 DB に切り替えて TestClient と storage を返す。"""
    db_path = tmp_path / "test_results.sqlite3"
    monkeypatch.setattr(storage, "_DB_PATH", db_path)
    storage.init_db()
    return storage


@pytest.fixture
def client(tmp_db):
    return TestClient(api_mod.app)


def _seed(storage_mod, n: int = 3, **overrides) -> list[int]:
    """既定パラメータで n 件挿入。返り値は ID リスト。"""
    base = dict(
        log_name="sample_firewall.log",
        config_id="config1",
        base_config="config1",
        confidence=0.85,
        tokens_in=120,
        tokens_out=80,
        latency_ms=1500,
        trace_id="trace-x",
        top_category="FW",
        top_summary="policy DENY",
    )
    base.update(overrides)
    return [storage_mod.insert_run_history(**base) for _ in range(n)]


# ─── storage ────────────────────────────────────────────────────────


def test_insert_and_get(tmp_db):
    rid = _seed(tmp_db, n=1)[0]
    row = tmp_db.get_run_history(rid)
    assert row is not None
    assert row["log_name"] == "sample_firewall.log"
    assert row["top_category"] == "FW"
    assert row["confidence"] == 0.85


def test_list_returns_newest_first(tmp_db):
    """started_at DESC で返ること。"""
    tmp_db.insert_run_history(
        log_name="a.log", config_id="config1", base_config="config1",
        confidence=None, tokens_in=None, tokens_out=None, latency_ms=None,
        trace_id=None, top_category=None, top_summary=None,
        started_at="2026-05-01T00:00:00+00:00",
    )
    tmp_db.insert_run_history(
        log_name="b.log", config_id="config1", base_config="config1",
        confidence=None, tokens_in=None, tokens_out=None, latency_ms=None,
        trace_id=None, top_category=None, top_summary=None,
        started_at="2026-05-13T00:00:00+00:00",
    )
    rows, total = tmp_db.list_run_history()
    assert total == 2
    assert rows[0]["log_name"] == "b.log"
    assert rows[1]["log_name"] == "a.log"


def test_filter_by_log_name(tmp_db):
    _seed(tmp_db, n=2, log_name="x.log")
    _seed(tmp_db, n=3, log_name="y.log")
    rows, total = tmp_db.list_run_history(log_name="y.log")
    assert total == 3
    assert all(r["log_name"] == "y.log" for r in rows)


def test_filter_by_config_id(tmp_db):
    _seed(tmp_db, n=2, config_id="config1", base_config="config1")
    _seed(tmp_db, n=1, config_id="user:5", base_config="config4")
    rows, total = tmp_db.list_run_history(config_id="user:5")
    assert total == 1
    assert rows[0]["config_id"] == "user:5"


def test_search_q_matches_summary_or_log_name(tmp_db):
    _seed(tmp_db, n=1, top_summary="ファイアウォール DENY", log_name="fw.log")
    _seed(tmp_db, n=1, top_summary="DNS タイムアウト", log_name="dns.log")
    rows, total = tmp_db.list_run_history(q="DENY")
    assert total == 1
    rows, total = tmp_db.list_run_history(q="dns")
    assert total == 1


def test_delete(tmp_db):
    rid = _seed(tmp_db, n=1)[0]
    assert tmp_db.delete_run_history(rid) is True
    assert tmp_db.get_run_history(rid) is None
    # 2 回目は False
    assert tmp_db.delete_run_history(rid) is False


# ─── API endpoints ──────────────────────────────────────────────────


def test_api_list_empty(client):
    r = client.get("/api/runs/history")
    assert r.status_code == 200
    body = r.json()
    assert body == {"entries": [], "total": 0, "limit": 200, "offset": 0}


def test_api_list_filters(tmp_db, client):
    _seed(tmp_db, n=2, log_name="fw.log", config_id="config1", top_summary="A")
    _seed(tmp_db, n=1, log_name="dns.log", config_id="config4", base_config="config4", top_summary="B")
    r = client.get("/api/runs/history?config_id=config1")
    assert r.status_code == 200
    assert r.json()["total"] == 2
    r = client.get("/api/runs/history?log_name=dns.log")
    assert r.json()["total"] == 1
    r = client.get("/api/runs/history?q=B")
    assert r.json()["total"] == 1


def test_api_get_404_on_missing(client):
    r = client.get("/api/runs/history/9999")
    assert r.status_code == 404


def test_api_get_returns_entry(tmp_db, client):
    rid = _seed(tmp_db, n=1)[0]
    r = client.get(f"/api/runs/history/{rid}")
    assert r.status_code == 200
    assert r.json()["id"] == rid


def test_api_delete(tmp_db, client):
    rid = _seed(tmp_db, n=1)[0]
    r = client.delete(f"/api/runs/history/{rid}")
    assert r.status_code == 200
    # 二度目は 404
    r = client.delete(f"/api/runs/history/{rid}")
    assert r.status_code == 404


def test_api_list_limit_validation(client):
    r = client.get("/api/runs/history?limit=0")
    assert r.status_code == 400
    r = client.get("/api/runs/history?limit=10000")
    assert r.status_code == 400
    r = client.get("/api/runs/history?offset=-1")
    assert r.status_code == 400
