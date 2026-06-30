"""ソースコード CRUD API（/api/source）のスモークテスト。

実ファイル I/O に依存するので、tmp_path で SOURCE_ROOT を差し替える。
複数アップロード / zip 展開 / zip-slip / 上限 / 同名拒否を中心に検証する。
"""
from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from log_analyzer import api as api_mod
from log_analyzer.source import codebase as source_codebase


@pytest.fixture
def client(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(source_codebase, "SOURCE_ROOT", tmp_path)
    return TestClient(api_mod.app)


def _zip_bytes(entries: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in entries.items():
            zf.writestr(name, content)
    return buf.getvalue()


def test_list_empty(client):
    r = client.get("/api/source")
    assert r.status_code == 200
    assert r.json() == {"codebases": []}


def test_upload_zip_then_list_tree_delete(client, tmp_path):
    zip_data = _zip_bytes(
        {
            "app/charge.py": "class Payment:\n    def charge(self):\n        return 1\n",
            "db/schema.sql": "CREATE TABLE users (id BIGINT PRIMARY KEY, email VARCHAR(255));",
            "node_modules/junk.js": "function nope(){}",  # 除外されるべき
        }
    )
    r = client.post(
        "/api/source",
        data={"name": "myapp"},
        files=[("files", ("src.zip", io.BytesIO(zip_data), "application/zip"))],
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["name"] == "myapp"
    # 索引対象は code ファイルのみ（.sql は DB スキーマに寄与、node_modules は除外）
    assert body["file_count"] == 1
    assert body["table_count"] == 1
    assert body["languages"].get("python") == 1

    # 物理配置の確認
    assert (tmp_path / "myapp" / "app" / "charge.py").exists()
    assert not (tmp_path / "myapp" / "node_modules").exists()

    # 一覧
    r = client.get("/api/source")
    names = [c["name"] for c in r.json()["codebases"]]
    assert names == ["myapp"]

    # ツリー（署名 + DBスキーマ、本文なし）
    r = client.get("/api/source/myapp/tree")
    assert r.status_code == 200
    tree = r.json()
    paths = {f["path"] for f in tree["files"]}
    assert "app/charge.py" in paths
    tnames = {t["name"].lower() for t in tree["db_schema"]["tables"]}
    assert "users" in tnames

    # 削除
    r = client.delete("/api/source/myapp")
    assert r.status_code == 200
    assert r.json() == {"deleted": "myapp"}
    assert not (tmp_path / "myapp").exists()


def test_upload_multiple_mixed(client, tmp_path):
    """単体ソース複数 + zip の混在アップロード。"""
    zip_data = _zip_bytes({"lib/util.ts": "export function u(){ return 1 }\n"})
    r = client.post(
        "/api/source",
        data={"name": "mixed"},
        files=[
            ("files", ("main.py", io.BytesIO(b"def main():\n    return 0\n"), "text/x-python")),
            ("files", ("models.py", io.BytesIO(
                b"class User(Base):\n    __tablename__='users'\n"), "text/x-python")),
            ("files", ("lib.zip", io.BytesIO(zip_data), "application/zip")),
        ],
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["file_count"] == 3
    assert body["languages"].get("python") == 2
    assert body["languages"].get("typescript") == 1
    assert (tmp_path / "mixed" / "main.py").exists()
    assert (tmp_path / "mixed" / "lib" / "util.ts").exists()


def test_upload_rejects_duplicate(client):
    files = [("files", ("a.py", io.BytesIO(b"x=1\n"), "text/plain"))]
    r1 = client.post("/api/source", data={"name": "dup"}, files=files)
    assert r1.status_code == 200
    r2 = client.post(
        "/api/source",
        data={"name": "dup"},
        files=[("files", ("a.py", io.BytesIO(b"x=1\n"), "text/plain"))],
    )
    assert r2.status_code == 409


def test_upload_rejects_bad_name(client):
    r = client.post(
        "/api/source",
        data={"name": "../escape"},
        files=[("files", ("a.py", io.BytesIO(b"x=1\n"), "text/plain"))],
    )
    assert r.status_code == 400


def test_zip_slip_entry_is_ignored(client, tmp_path):
    zip_data = _zip_bytes(
        {
            "ok.py": "x = 1\n",
            "../evil.py": "danger = 1\n",  # 展開先の外を指す → 無視される
        }
    )
    r = client.post(
        "/api/source",
        data={"name": "slip"},
        files=[("files", ("z.zip", io.BytesIO(zip_data), "application/zip"))],
    )
    assert r.status_code == 200, r.text
    assert (tmp_path / "slip" / "ok.py").exists()
    # 親ディレクトリに脱出していないこと
    assert not (tmp_path / "evil.py").exists()


def test_upload_rejects_oversized(client, monkeypatch):
    monkeypatch.setattr(source_codebase, "MAX_TOTAL_BYTES", 50)
    r = client.post(
        "/api/source",
        data={"name": "big"},
        files=[("files", ("a.py", io.BytesIO(b"x" * 200), "text/plain"))],
    )
    assert r.status_code == 413
    assert not (api_mod.source_codebase.SOURCE_ROOT / "big").exists()


def test_upload_with_no_indexable_files(client):
    """対象言語が無い（README だけ等）場合は 400。"""
    r = client.post(
        "/api/source",
        data={"name": "empty"},
        files=[("files", ("README.md", io.BytesIO(b"# hi\n"), "text/plain"))],
    )
    assert r.status_code == 400


def test_config_log_stream_rejects_unknown_codebase(client):
    """config-log-stream に存在しない source_codebase を指定すると 404（ストリーム開始前）。"""
    r = client.post(
        "/api/runs/config-log-stream",
        json={
            "config": "config4",
            "topology": {"nodes": [{"id": "app-01", "type": "Server"}], "links": []},
            "node_configs": {"app-01": [{"name": "c.conf", "content": "key=value"}]},
            "analysis_mode": "single",
            "single_source": "config",
            "source_codebase": "does_not_exist",
        },
    )
    assert r.status_code == 404
    assert "コードベースが見つかりません" in r.json()["detail"]


def test_tree_404_on_missing(client):
    r = client.get("/api/source/nope/tree")
    assert r.status_code == 404


def test_delete_404_on_missing(client):
    r = client.delete("/api/source/nope")
    assert r.status_code == 404
