"""ファイルストレージ抽象層 (filestore) のテスト。

ローカル FS バックエンドの往復（保存 / 一覧 / 読み取り / サイズ / 削除）と、
env 指定のパース（未設定=ローカル / gs://=GCS / 不正=エラー）を検証する。
GCS バックエンドは実バケット接続が要るため、ここでは URI 解釈と ref だけ確認する。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from log_analyzer import filestore


def test_local_roundtrip(tmp_path: Path):
    store = filestore.build_log_store(None, local_root=tmp_path)
    assert isinstance(store, filestore.LocalLogStore)
    assert store.list() == []

    store.save_bytes("a.log", b"line1\nline2\n")
    assert store.exists("a.log")
    assert store.read_text("a.log") == "line1\nline2\n"
    assert store.size("a.log") == 12
    assert [m.name for m in store.list()] == ["a.log"]
    assert store.ref("a.log").endswith("a.log")

    store.delete("a.log")
    assert not store.exists("a.log")


def test_local_list_sorted_and_filtered(tmp_path: Path):
    store = filestore.build_log_store("local", local_root=tmp_path)
    store.save_bytes("b.log", b"x")
    store.save_bytes("a.log", b"y")
    (tmp_path / "ignore.txt").write_text("nope")  # .log 以外は無視
    assert [m.name for m in store.list()] == ["a.log", "b.log"]


def test_local_missing_raises(tmp_path: Path):
    store = filestore.build_log_store(None, local_root=tmp_path)
    with pytest.raises(FileNotFoundError):
        store.read_text("missing.log")
    with pytest.raises(FileNotFoundError):
        store.size("missing.log")
    with pytest.raises(FileNotFoundError):
        store.delete("missing.log")


def test_local_save_overwrites(tmp_path: Path):
    store = filestore.build_log_store(None, local_root=tmp_path)
    store.save_bytes("a.log", b"first")
    store.save_bytes("a.log", b"second-longer")
    assert store.read_text("a.log") == "second-longer"
    assert not list(tmp_path.glob("*.tmp"))  # 一時ファイルが残らない


def test_gs_uri_parsing(tmp_path: Path):
    store = filestore.build_log_store("gs://my-bucket/logs/prod", local_root=tmp_path)
    assert isinstance(store, filestore.GcsLogStore)
    assert store.ref("x.log") == "gs://my-bucket/logs/prod/x.log"


def test_gs_uri_no_prefix(tmp_path: Path):
    store = filestore.build_log_store("gs://bucket-only", local_root=tmp_path)
    assert store.ref("x.log") == "gs://bucket-only/x.log"


def test_invalid_spec_raises(tmp_path: Path):
    with pytest.raises(ValueError):
        filestore.build_log_store("http://nope", local_root=tmp_path)
    with pytest.raises(ValueError):
        filestore.build_log_store("gs://", local_root=tmp_path)


def test_singleton_reset(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("LOG_STORE", raising=False)
    filestore.reset_for_tests()
    filestore.configure_default_local_root(tmp_path)
    s1 = filestore.get_log_store()
    s2 = filestore.get_log_store()
    assert s1 is s2  # キャッシュされる
    filestore.reset_for_tests()
    assert filestore.get_log_store() is not s1  # reset で作り直し
