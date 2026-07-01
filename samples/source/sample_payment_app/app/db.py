"""DB セッション（題材用の最小実装）。"""
from __future__ import annotations


class _Session:
    def execute(self, sql: str, params: tuple) -> None: ...
    def commit(self) -> None: ...


_session = _Session()


def get_session() -> _Session:
    return _session
