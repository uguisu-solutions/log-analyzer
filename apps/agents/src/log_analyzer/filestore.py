"""ファイルストレージ抽象層。

DB (``storage.py``) が ``DATABASE_URL`` で SQLite/PostgreSQL を切替えるのと同じ流儀で、
アップロードファイルの保存先を環境変数で切替える。**未設定ならローカル FS＝ローカル開発は
現状維持**（不変条件）。

- ``LOG_STORE`` 未設定 → ローカル FS（既定 ``samples/logs/``）
- ``LOG_STORE=gs://<bucket>/<prefix>`` → GCS

方針: hosting-refactor-policy.md #1。ログは「単一・小」なので **GCS オブジェクト直
get/put**。コードベース (``samples/source``) は多数ファイルの再帰走査＋tree-sitter のため
別扱い（download→ローカル処理→キャッシュ書戻し。Increment 3 で ``SourceStore`` を追加予定）。

GCS バックエンドは ``google-cloud-storage`` を **遅延 import** する。ローカル開発や
ユニットテストではこの依存を必要としない。認証は ADC（Cloud Run のサービスアカウント）。
"""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


@dataclass(frozen=True)
class LogMeta:
    """ログ 1 件のメタ情報。"""

    name: str
    size: int
    modified_at: datetime | None


class LogStore(ABC):
    """``*.log`` の保存先を抽象化する。``name`` は呼び出し側で検証済みの安全な名前。"""

    @abstractmethod
    def list(self) -> list[LogMeta]:
        """``*.log`` を名前昇順で列挙する。"""

    @abstractmethod
    def exists(self, name: str) -> bool: ...

    @abstractmethod
    def read_text(self, name: str) -> str:
        """UTF-8（不正バイトは置換）で全文を返す。無ければ ``FileNotFoundError``。"""

    @abstractmethod
    def size(self, name: str) -> int:
        """バイト数。無ければ ``FileNotFoundError``。"""

    @abstractmethod
    def save_bytes(self, name: str, data: bytes) -> None:
        """``data`` を ``name`` として保存（上書き）。存在チェックは呼び出し側の責務。"""

    @abstractmethod
    def delete(self, name: str) -> None:
        """削除。無ければ ``FileNotFoundError``。"""

    @abstractmethod
    def ref(self, name: str) -> str:
        """トレース/表示用のラベル（ローカル絶対パス or ``gs://`` URI）。読み取りには使わない。"""


class LocalLogStore(LogStore):
    """ローカル FS バックエンド（従来挙動）。"""

    def __init__(self, root: Path) -> None:
        self._root = root

    def _path(self, name: str) -> Path:
        return self._root / name

    def list(self) -> list[LogMeta]:
        if not self._root.exists():
            return []
        out: list[LogMeta] = []
        for path in sorted(self._root.glob("*.log")):
            st = path.stat()
            out.append(
                LogMeta(
                    name=path.name,
                    size=st.st_size,
                    modified_at=datetime.fromtimestamp(st.st_mtime, tz=timezone.utc),
                )
            )
        return out

    def exists(self, name: str) -> bool:
        return self._path(name).is_file()

    def read_text(self, name: str) -> str:
        path = self._path(name)
        if not path.is_file():
            raise FileNotFoundError(name)
        return path.read_text(encoding="utf-8", errors="replace")

    def size(self, name: str) -> int:
        path = self._path(name)
        if not path.is_file():
            raise FileNotFoundError(name)
        return path.stat().st_size

    def save_bytes(self, name: str, data: bytes) -> None:
        self._root.mkdir(parents=True, exist_ok=True)
        target = self._path(name)
        tmp = target.with_suffix(target.suffix + ".tmp")
        try:
            tmp.write_bytes(data)
            tmp.replace(target)  # 同一FS内アトミック置換
        except Exception:
            if tmp.exists():
                tmp.unlink()
            raise

    def delete(self, name: str) -> None:
        path = self._path(name)
        if not path.is_file():
            raise FileNotFoundError(name)
        path.unlink()

    def ref(self, name: str) -> str:
        return str(self._path(name))


class GcsLogStore(LogStore):
    """GCS バックエンド。``gs://<bucket>/<prefix>`` を受け取り ``<prefix>/<name>`` を扱う。"""

    def __init__(self, bucket: str, prefix: str) -> None:
        self._bucket_name = bucket
        self._prefix = prefix.strip("/")
        self._bucket = None  # 遅延生成（google-cloud-storage の import を先送り）

    def _get_bucket(self):  # noqa: ANN202 — google Bucket 型を型注釈に出さない
        if self._bucket is None:
            from google.cloud import storage  # 遅延 import（ADC で認証）

            client = storage.Client()
            self._bucket = client.bucket(self._bucket_name)
        return self._bucket

    def _blob_name(self, name: str) -> str:
        return f"{self._prefix}/{name}" if self._prefix else name

    def _blob(self, name: str):  # noqa: ANN202
        return self._get_bucket().blob(self._blob_name(name))

    def list(self) -> list[LogMeta]:
        prefix = f"{self._prefix}/" if self._prefix else ""
        out: list[LogMeta] = []
        for blob in self._get_bucket().list_blobs(prefix=prefix):
            base = blob.name[len(prefix):]
            if not base.endswith(".log") or "/" in base:
                continue
            out.append(
                LogMeta(name=base, size=blob.size or 0, modified_at=blob.updated)
            )
        out.sort(key=lambda m: m.name)
        return out

    def exists(self, name: str) -> bool:
        return self._blob(name).exists()

    def read_text(self, name: str) -> str:
        blob = self._blob(name)
        if not blob.exists():
            raise FileNotFoundError(name)
        return blob.download_as_bytes().decode("utf-8", errors="replace")

    def size(self, name: str) -> int:
        blob = self._blob(name)
        blob.reload()  # size を得るためメタを取得（無ければ NotFound）
        if blob.size is None:
            raise FileNotFoundError(name)
        return blob.size

    def save_bytes(self, name: str, data: bytes) -> None:
        self._blob(name).upload_from_string(data, content_type="text/plain")

    def delete(self, name: str) -> None:
        blob = self._blob(name)
        if not blob.exists():
            raise FileNotFoundError(name)
        blob.delete()

    def ref(self, name: str) -> str:
        return f"gs://{self._bucket_name}/{self._blob_name(name)}"


def _parse_gs_uri(uri: str) -> tuple[str, str]:
    """``gs://bucket/prefix`` を (bucket, prefix) に分解する。"""
    rest = uri[len("gs://"):]
    bucket, _, prefix = rest.partition("/")
    if not bucket:
        raise ValueError(f"不正な gs URI: {uri!r}")
    return bucket, prefix


def build_log_store(spec: str | None, *, local_root: Path) -> LogStore:
    """env の指定からログストアを構築する。

    ``spec`` が空 / ``local`` → ローカル FS、``gs://...`` → GCS。
    """
    spec = (spec or "").strip()
    if not spec or spec == "local":
        return LocalLogStore(local_root)
    if spec.startswith("gs://"):
        bucket, prefix = _parse_gs_uri(spec)
        return GcsLogStore(bucket, prefix)
    raise ValueError(f"未対応の LOG_STORE 指定: {spec!r}（'' / 'local' / 'gs://...' のみ）")


# ─── プロセス内シングルトン ────────────────────────────────────────────
_log_store: LogStore | None = None
_default_local_root: Path | None = None


def configure_default_local_root(root: Path) -> None:
    """LOG_STORE 未設定時に使うローカルディレクトリを登録する（api 起動時に呼ぶ）。"""
    global _default_local_root
    _default_local_root = root


def get_log_store() -> LogStore:
    """env ``LOG_STORE`` に基づくログストアを返す（遅延生成・キャッシュ）。"""
    global _log_store
    if _log_store is None:
        root = _default_local_root or (Path.cwd() / "samples" / "logs")
        _log_store = build_log_store(os.environ.get("LOG_STORE"), local_root=root)
    return _log_store


def reset_for_tests() -> None:
    """テスト用: シングルトンを破棄する。"""
    global _log_store
    _log_store = None
