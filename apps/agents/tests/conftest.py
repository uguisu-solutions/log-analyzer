"""テスト共通の前処理。

1. テストが開発用 DB (``apps/agents/data/results.sqlite3``) を汚さないよう、
   SQLite の保存先をテスト用の一時ディレクトリへ差し替える。

   特に確認事項 B-4 で「解析エンドポイントの 400/422 も実行履歴に残す」対応を
   入れたため、400/422 を検証する既存テストが実 DB に行を書き込むようになる。
   個々のテストが独自に ``_DB_PATH`` を差し替える場合はそちらが優先される。

2. テストから外部ネットワークへ出ていかせない。Anthropic / OpenAI / BigQuery を
   実際に叩くと **課金が発生し、結果が非決定になる**。LLM 呼び出しは必ず
   ``monkeypatch`` / ``unittest.mock`` で差し替えること。
   localhost 宛（ローカル Langfuse 等）は許す。どうしても外部接続が必要な
   テストは ``@pytest.mark.allow_network`` を付ける。
"""
from __future__ import annotations

import socket

import pytest

from log_analyzer import storage

_LOCAL_HOSTS = {"127.0.0.1", "::1", "localhost", "0.0.0.0", ""}


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "allow_network: 外部ネットワークへの接続を明示的に許可する"
    )


@pytest.fixture(autouse=True)
def _isolate_db(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "_DB_PATH", tmp_path / "results.sqlite3")
    storage.init_db()
    yield


@pytest.fixture(autouse=True)
def _no_external_network(request, monkeypatch):
    """localhost 以外への TCP 接続を落とす（実 API 呼び出しの事故防止）。"""
    if request.node.get_closest_marker("allow_network"):
        yield
        return

    def _host_of(address) -> str:
        return str(address[0]) if isinstance(address, tuple) and address else str(address)

    real_connect = socket.socket.connect
    real_create_connection = socket.create_connection

    def guarded_connect(self, address, *args, **kwargs):
        host = _host_of(address)
        if host not in _LOCAL_HOSTS:
            raise RuntimeError(
                f"テストから外部ネットワークへ接続しようとした: {host}。"
                "LLM / BigQuery 呼び出しはモックすること"
                "（意図的な場合は @pytest.mark.allow_network）"
            )
        return real_connect(self, address, *args, **kwargs)

    def guarded_create_connection(address, *args, **kwargs):
        host = _host_of(address)
        if host not in _LOCAL_HOSTS:
            raise RuntimeError(f"テストから外部ネットワークへ接続しようとした: {host}")
        return real_create_connection(address, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket, "create_connection", guarded_create_connection)
    yield
