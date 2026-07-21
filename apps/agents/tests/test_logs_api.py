"""ログ管理 API のスモークテスト。

実際のファイル I/O に依存するので、tmp_path で `_LOGS_DIR` を差し替える。
パストラバーサル / サイズ上限 / 同名拒否などの境界条件を中心に検証する。
"""
from __future__ import annotations

import io
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from log_analyzer import api as api_mod
from log_analyzer import filestore


@pytest.fixture
def client(tmp_path: Path, monkeypatch):
    """ログストアを一時ディレクトリのローカル FS に差し替えて TestClient を返す。"""
    original_root = api_mod._LOGS_DIR  # パッチ前の実ディレクトリを控える
    monkeypatch.setattr(api_mod, "_LOGS_DIR", tmp_path)
    monkeypatch.delenv("LOG_STORE", raising=False)  # 確実にローカル FS を使う
    filestore.reset_for_tests()
    filestore.configure_default_local_root(tmp_path)
    yield TestClient(api_mod.app)
    filestore.reset_for_tests()
    filestore.configure_default_local_root(original_root)


def test_list_logs_empty(client):
    r = client.get("/api/logs")
    assert r.status_code == 200
    assert r.json() == {"logs": []}


def test_upload_then_list_then_get_then_delete(client, tmp_path):
    # アップロード
    file_data = b"line1\nline2\nline3\n"
    r = client.post(
        "/api/logs",
        files={"file": ("test_sample.log", io.BytesIO(file_data), "text/plain")},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["name"] == "test_sample.log"
    assert body["lines"] == 3
    assert body["bytes"] == len(file_data)
    assert (tmp_path / "test_sample.log").exists()

    # 一覧
    r = client.get("/api/logs")
    assert r.status_code == 200
    logs = r.json()["logs"]
    assert len(logs) == 1
    assert logs[0]["name"] == "test_sample.log"
    assert logs[0]["lines"] == 3
    assert "modified_at" in logs[0]

    # プレビュー
    r = client.get("/api/logs/test_sample.log/content")
    assert r.status_code == 200
    body = r.json()
    assert body["name"] == "test_sample.log"
    assert body["total_lines"] == 3
    assert body["preview_lines"] == 3
    assert body["truncated"] is False
    assert body["content"] == "line1\nline2\nline3"

    # 削除
    r = client.delete("/api/logs/test_sample.log")
    assert r.status_code == 200
    assert r.json() == {"deleted": "test_sample.log"}
    assert not (tmp_path / "test_sample.log").exists()


def test_upload_rejects_duplicate_name(client):
    r1 = client.post(
        "/api/logs",
        files={"file": ("dup.log", io.BytesIO(b"first"), "text/plain")},
    )
    assert r1.status_code == 200
    r2 = client.post(
        "/api/logs",
        files={"file": ("dup.log", io.BytesIO(b"second"), "text/plain")},
    )
    assert r2.status_code == 409
    assert "既に存在" in r2.json()["detail"]


def test_upload_rejects_non_log_extension(client):
    r = client.post(
        "/api/logs",
        files={"file": ("readme.txt", io.BytesIO(b"hi"), "text/plain")},
    )
    assert r.status_code == 400
    assert ".log" in r.json()["detail"]


def test_upload_rejects_path_traversal(client):
    r = client.post(
        "/api/logs",
        files={"file": ("../escape.log", io.BytesIO(b"x"), "text/plain")},
    )
    assert r.status_code == 400


def test_upload_rejects_oversized_file(client, monkeypatch):
    # 上限を 100 byte まで下げて検証（巨大ファイルを実際に作らない）
    monkeypatch.setattr(api_mod, "_MAX_LOG_SIZE_BYTES", 100)
    r = client.post(
        "/api/logs",
        files={"file": ("big.log", io.BytesIO(b"x" * 200), "text/plain")},
    )
    assert r.status_code == 413
    # 失敗時は .tmp も残らない
    assert not list(api_mod._LOGS_DIR.glob("*.tmp"))
    assert not (api_mod._LOGS_DIR / "big.log").exists()


def test_preview_truncates_long_log(client, monkeypatch):
    monkeypatch.setattr(api_mod, "_PREVIEW_MAX_LINES", 5)
    content = "\n".join(f"line{i}" for i in range(20)).encode()
    client.post(
        "/api/logs",
        files={"file": ("long.log", io.BytesIO(content), "text/plain")},
    )
    r = client.get("/api/logs/long.log/content")
    assert r.status_code == 200
    body = r.json()
    assert body["total_lines"] == 20
    assert body["preview_lines"] == 5
    assert body["truncated"] is True
    assert body["content"].count("\n") == 4  # 5 行 = 区切り 4 つ


def test_get_content_404_on_missing(client):
    r = client.get("/api/logs/nope.log/content")
    assert r.status_code == 404


def test_delete_404_on_missing(client):
    r = client.delete("/api/logs/nope.log")
    assert r.status_code == 404


def test_delete_rejects_path_traversal(client):
    r = client.delete("/api/logs/..%2Fescape.log")
    # FastAPI は %2F を / にデコードして path として扱うため 404 になる場合と
    # 400 になる場合があるが、いずれにせよ削除は走らない
    assert r.status_code in (400, 404)
