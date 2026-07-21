"""コードベース（``samples/source/<name>/``）の保存先抽象層。

ログ (``filestore.py``) と同じく env で切替える。ただしコードベースは **多数の小ファイルを
再帰走査し tree-sitter を掛ける** ため、GCS を直接 FS 扱いはしない（遅い / rename・locking
差異）。方針 (hosting-refactor-policy.md #1.2): **入口(GCS)は抽象化、処理は必ずローカル実 FS**。

- ``SOURCE_STORE`` 未設定 → ローカル FS（``codebase.py`` は従来どおり ``SOURCE_ROOT`` で動作、
  本モジュールは完全な no-op）。
- ``SOURCE_STORE=gs://<bucket>/<prefix>`` → GCS。``ensure_local`` で解析前にローカルへ展開し、
  ``persist`` で取り込み結果（ソース＋``.index.json``＋``.meta.json``）を書き戻す。

``codebase.py`` は常にローカルディレクトリ（``local_root/<name>``）に対して処理し、その
ローカルと backing store の同期だけを本 store が担う。これにより **ローカル開発は
一切変わらない**（不変条件）。GCS 認証は ADC（Cloud Run のサービスアカウント）。
"""

from __future__ import annotations

import json
import os
from abc import ABC, abstractmethod
from pathlib import Path

_META_FILENAME = ".meta.json"


class SourceStore(ABC):
    """コードベースのローカル作業ディレクトリと backing store を同期する。"""

    @abstractmethod
    def ensure_local(self, name: str) -> None:
        """``name`` のファイル一式をローカル ``local_root/<name>`` に用意する。

        ローカル backend は no-op。GCS backend は未取得なら download する。
        """

    @abstractmethod
    def persist(self, name: str) -> None:
        """ローカル ``local_root/<name>`` の内容を backing store へ書き戻す。

        ローカル backend は no-op（既にそこが正）。GCS backend は upload。
        """

    @abstractmethod
    def list_names(self) -> list[str]:
        """登録済みコードベース名を返す。"""

    @abstractmethod
    def exists(self, name: str) -> bool: ...

    @abstractmethod
    def read_meta(self, name: str) -> dict | None:
        """``.meta.json`` を（あれば）読む。無ければ ``None``。一覧の軽量化に使う。"""

    @abstractmethod
    def delete(self, name: str) -> None:
        """backing store 側の ``name`` を削除する（ローカルディレクトリは呼び出し側が消す）。"""


class LocalSourceStore(SourceStore):
    """ローカル FS backend。``codebase.py`` が直接 ``local_root`` を扱うため実体は薄い。"""

    def __init__(self, local_root: Path) -> None:
        self._root = local_root

    def ensure_local(self, name: str) -> None:
        return  # 既にローカルが正

    def persist(self, name: str) -> None:
        return  # 既にローカルが正

    def list_names(self) -> list[str]:
        if not self._root.exists():
            return []
        return sorted(d.name for d in self._root.iterdir() if d.is_dir())

    def exists(self, name: str) -> bool:
        return (self._root / name).is_dir()

    def read_meta(self, name: str) -> dict | None:
        meta = self._root / name / _META_FILENAME
        if not meta.is_file():
            return None
        try:
            return json.loads(meta.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None

    def delete(self, name: str) -> None:
        return  # ローカルディレクトリの削除は呼び出し側（rmtree）が担う


class GcsSourceStore(SourceStore):
    """GCS backend。``gs://<bucket>/<prefix>`` の ``<prefix>/<name>/...`` を扱う。"""

    def __init__(self, bucket: str, prefix: str, local_root: Path) -> None:
        self._bucket_name = bucket
        self._prefix = prefix.strip("/")
        self._root = local_root
        self._bucket = None
        self._materialized: set[str] = set()  # プロセス内で download 済みの名前

    def _get_bucket(self):  # noqa: ANN202
        if self._bucket is None:
            from google.cloud import storage  # 遅延 import（ADC 認証）

            self._bucket = storage.Client().bucket(self._bucket_name)
        return self._bucket

    def _name_prefix(self, name: str) -> str:
        base = f"{self._prefix}/" if self._prefix else ""
        return f"{base}{name}/"

    def ensure_local(self, name: str) -> None:
        dest = self._root / name
        # 既に download 済み、またはローカルに実体があればスキップ（demo 前提の単純キャッシュ）。
        if name in self._materialized or (dest.exists() and any(dest.iterdir())):
            self._materialized.add(name)
            return
        prefix = self._name_prefix(name)
        for blob in self._get_bucket().list_blobs(prefix=prefix):
            rel = blob.name[len(prefix):]
            if not rel or rel.endswith("/"):
                continue
            target = dest / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            blob.download_to_filename(str(target))
        self._materialized.add(name)

    def persist(self, name: str) -> None:
        src = self._root / name
        if not src.is_dir():
            return
        prefix = self._name_prefix(name)
        bucket = self._get_bucket()
        for path in src.rglob("*"):
            if not path.is_file():
                continue
            rel = path.relative_to(src).as_posix()
            bucket.blob(f"{prefix}{rel}").upload_from_filename(str(path))
        self._materialized.add(name)

    def list_names(self) -> list[str]:
        base = f"{self._prefix}/" if self._prefix else ""
        names: set[str] = set()
        # delimiter で「サブディレクトリ」= コードベース名を列挙
        iterator = self._get_bucket().list_blobs(prefix=base, delimiter="/")
        # prefixes を得るには結果を消費する必要がある
        list(iterator)
        for sub in iterator.prefixes:
            name = sub[len(base):].rstrip("/")
            if name:
                names.add(name)
        return sorted(names)

    def exists(self, name: str) -> bool:
        if (self._root / name).is_dir():
            return True
        blobs = self._get_bucket().list_blobs(prefix=self._name_prefix(name), max_results=1)
        return any(True for _ in blobs)

    def read_meta(self, name: str) -> dict | None:
        blob = self._get_bucket().blob(f"{self._name_prefix(name)}{_META_FILENAME}")
        if not blob.exists():
            return None
        try:
            return json.loads(blob.download_as_bytes().decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None

    def delete(self, name: str) -> None:
        for blob in self._get_bucket().list_blobs(prefix=self._name_prefix(name)):
            try:
                blob.delete()
            except Exception:  # noqa: BLE001 — 個別失敗は無視して続行
                pass
        self._materialized.discard(name)


def _parse_gs_uri(uri: str) -> tuple[str, str]:
    rest = uri[len("gs://"):]
    bucket, _, prefix = rest.partition("/")
    if not bucket:
        raise ValueError(f"不正な gs URI: {uri!r}")
    return bucket, prefix


def build_source_store(spec: str | None, *, local_root: Path) -> SourceStore:
    """env 指定からコードベースストアを構築する（'' / 'local' / 'gs://...'）。"""
    spec = (spec or "").strip()
    if not spec or spec == "local":
        return LocalSourceStore(local_root)
    if spec.startswith("gs://"):
        bucket, prefix = _parse_gs_uri(spec)
        return GcsSourceStore(bucket, prefix, local_root)
    raise ValueError(f"未対応の SOURCE_STORE 指定: {spec!r}（'' / 'local' / 'gs://...' のみ）")


# ─── プロセス内シングルトン ────────────────────────────────────────────
_store: SourceStore | None = None
_local_root: Path | None = None


def configure_local_root(root: Path) -> None:
    """コードベースのローカル作業ルート（既定 samples/source）を登録する。

    ルートが変わったらシングルトンを作り直す（テストが SOURCE_ROOT を差し替えても
    追従できるようにするため）。本番では SOURCE_ROOT は不変なので再生成は起きない。
    """
    global _local_root, _store
    if root != _local_root:
        _local_root = root
        _store = None


def get_source_store() -> SourceStore:
    """env ``SOURCE_STORE`` に基づくストアを返す（遅延生成・キャッシュ）。"""
    global _store
    if _store is None:
        root = _local_root or (Path.cwd() / "samples" / "source")
        _store = build_source_store(os.environ.get("SOURCE_STORE"), local_root=root)
    return _store


def reset_for_tests() -> None:
    global _store
    _store = None
