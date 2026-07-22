"""コードベースストア (source.store) のテスト。

ローカル backend（``codebase.py`` が直接ローカルを扱うため薄い層）と、env 指定の
パース、シングルトンのルート追従（テストが SOURCE_ROOT を差し替えても付いてくる）を検証する。
GCS backend は実バケット接続が要るため URI 解釈と blob prefix 生成のみ確認する。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from log_analyzer.source import store as source_store


def test_local_list_and_exists(tmp_path: Path):
    st = source_store.build_source_store(None, local_root=tmp_path)
    assert isinstance(st, source_store.LocalSourceStore)
    assert st.list_names() == []

    (tmp_path / "cbA").mkdir()
    (tmp_path / "cbA" / ".meta.json").write_text(
        json.dumps({"name": "cbA", "file_count": 3}), encoding="utf-8"
    )
    (tmp_path / "cbB").mkdir()
    (tmp_path / "not_a_dir.txt").write_text("x")

    assert st.list_names() == ["cbA", "cbB"]
    assert st.exists("cbA")
    assert not st.exists("missing")


def test_local_read_meta(tmp_path: Path):
    st = source_store.build_source_store("local", local_root=tmp_path)
    (tmp_path / "cb").mkdir()
    assert st.read_meta("cb") is None  # meta 無し
    (tmp_path / "cb" / ".meta.json").write_text(
        json.dumps({"name": "cb", "file_count": 1}), encoding="utf-8"
    )
    assert st.read_meta("cb") == {"name": "cb", "file_count": 1}


def test_local_ensure_persist_delete_are_local_noops(tmp_path: Path):
    """ローカル backend では ensure/persist は no-op、delete は backing のみ（ローカルは残す）。"""
    st = source_store.build_source_store(None, local_root=tmp_path)
    (tmp_path / "cb").mkdir()
    st.ensure_local("cb")  # 何もしない
    st.persist("cb")       # 何もしない
    st.delete("cb")        # backing のみ（ローカル削除は呼び出し側 rmtree の責務）
    assert (tmp_path / "cb").exists()  # ローカルは残る


def test_gs_uri_parsing_and_prefix(tmp_path: Path):
    st = source_store.build_source_store("gs://bkt/src/prod", local_root=tmp_path)
    assert isinstance(st, source_store.GcsSourceStore)
    assert st._name_prefix("demo") == "src/prod/demo/"

    st2 = source_store.build_source_store("gs://bkt", local_root=tmp_path)
    assert st2._name_prefix("demo") == "demo/"


def test_invalid_spec_raises(tmp_path: Path):
    with pytest.raises(ValueError):
        source_store.build_source_store("s3://nope", local_root=tmp_path)
    with pytest.raises(ValueError):
        source_store.build_source_store("gs://", local_root=tmp_path)


def test_singleton_follows_root_change(tmp_path: Path, monkeypatch):
    """configure_local_root がルート変更時にシングルトンを作り直す。"""
    monkeypatch.delenv("SOURCE_STORE", raising=False)
    root_a = tmp_path / "a"
    root_b = tmp_path / "b"
    root_a.mkdir()
    root_b.mkdir()

    source_store.reset_for_tests()
    source_store.configure_local_root(root_a)
    s1 = source_store.get_source_store()
    assert source_store.get_source_store() is s1  # 同一ルートならキャッシュ

    source_store.configure_local_root(root_b)  # ルート変更 → 作り直し
    s2 = source_store.get_source_store()
    assert s2 is not s1

    source_store.reset_for_tests()
