"""テスト共通の前処理。

テストが開発用 DB (``apps/agents/data/results.sqlite3``) を汚さないよう、
SQLite の保存先をテスト用の一時ディレクトリへ差し替える。

特に確認事項 B-4 で「解析エンドポイントの 400/422 も実行履歴に残す」対応を
入れたため、400/422 を検証する既存テストが実 DB に行を書き込むようになる。
個々のテストが独自に ``_DB_PATH`` を差し替える場合はそちらが優先される。
"""
from __future__ import annotations

import pytest

from log_analyzer import storage


@pytest.fixture(autouse=True)
def _isolate_db(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "_DB_PATH", tmp_path / "results.sqlite3")
    storage.init_db()
    yield
