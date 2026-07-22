"""コードベースの取り込み・一覧・削除（Phase 1）。

UI からの複数ファイルアップロード（zip / 単体ソース混在）を受け、
``samples/source/<name>/`` に集約する。zip は展開し、単体ソースはそのまま配置。

セキュリティ / 上限（設計: docs/plan/source_code_analysis.md §1）:
- **合計 50MB 上限**（展開後サイズで加算。zip 爆弾対策）。
- 除外ルール（``node_modules`` 等）を展開時に適用し、不要物は書き込まない。
- zip-slip（``../`` / 絶対パス）を弾き、展開先を ``<name>/`` 配下に閉じ込める。

API（api.py）からは本モジュールの同期関数を呼ぶ。アップロードのストリーム読み込み
（async）は api.py 側で行い、ステージ済みファイルを ``(filename, path)`` で渡す。
"""
from __future__ import annotations

import json
import re
import shutil
import zipfile
from pathlib import Path, PurePosixPath

from log_analyzer.source import store as source_store
from log_analyzer.source.db_schema import extract_db_schema
from log_analyzer.source.indexer import (
    MAX_FILE_BYTES,
    build_source_index,
    get_or_build_index,
    is_excluded_dir,
    is_excluded_file,
    language_for,
    save_index,
)

# このファイル: apps/agents/src/log_analyzer/source/codebase.py → repo ルートは 5 階層上
SOURCE_ROOT = Path(__file__).resolve().parents[5] / "samples" / "source"

# コードベースの保存先は store が env(SOURCE_STORE) で切替える（未設定＝この SOURCE_ROOT を
# ローカル作業ルートとして使い、GCS 同期は no-op）。
source_store.configure_local_root(SOURCE_ROOT)


def _store() -> source_store.SourceStore:
    """現在の SOURCE_ROOT を同期したうえでストアを返す。

    テストが ``SOURCE_ROOT`` を monkeypatch した場合でも、その差し替えを store に
    追従させる（本番では SOURCE_ROOT 不変なので再生成は起きない）。
    """
    source_store.configure_local_root(SOURCE_ROOT)
    return source_store.get_source_store()


# 取り込み合計サイズ上限（展開後ベース）
MAX_TOTAL_BYTES = 50 * 1024 * 1024

# コードベース名: 英数字 / _ - . のみ（パストラバーサル防止）。先頭ドットは不可。
_VALID_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

_META_FILENAME = ".meta.json"


class SourceError(Exception):
    """取り込み時のユーザー起因エラー（API は 4xx に変換）。"""

    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


def safe_codebase_dir(name: str) -> Path:
    """name を検証し、``samples/source/<name>`` の絶対パスを返す。"""
    if not _VALID_NAME_RE.match(name or ""):
        raise SourceError(
            f"コードベース名は英数字 / _ / - / . のみ（先頭はドット不可）: {name!r}"
        )
    target = (SOURCE_ROOT / name).resolve()
    root = SOURCE_ROOT.resolve()
    if target != root and root not in target.parents:
        raise SourceError(f"不正なコードベース名: {name!r}")
    return target


# ─── 取り込み ─────────────────────────────────────────────────────────


def materialize(name: str, items: list[tuple[str, Path]]) -> dict:
    """ステージ済みアップロード（zip / 単体）を展開・集約し、index/meta を生成する。

    ``items`` は ``(元のファイル名, ステージ済みパス)`` のリスト。
    返り値はコードベースの統計 dict。失敗時は ``SourceError``。
    """
    dest = safe_codebase_dir(name)
    store = _store()
    if store.exists(name) or dest.exists():
        raise SourceError(f"同名のコードベースが既に存在します: {name}", status_code=409)

    dest.mkdir(parents=True, exist_ok=False)
    written = 0
    try:
        for filename, staged in items:
            if zipfile.is_zipfile(staged):
                written = _extract_zip(staged, dest, written)
            else:
                written = _place_single(filename, staged, dest, written)
        index = build_source_index(dest)
        schema = extract_db_schema(dest)
        if not index.files and not schema.tables:
            raise SourceError(
                "解析対象のソース（Python / TS / JS）も DB スキーマ（DDL / ORM）も"
                "見つかりませんでした（すべて除外対象 / 非対象拡張子の可能性）"
            )
        save_index(index)
        stats = _build_stats(name, index, schema)
        (dest / _META_FILENAME).write_text(
            json.dumps(stats, ensure_ascii=False), encoding="utf-8"
        )
        # backing store（GCS）へ書き戻す。ローカル backend は no-op。
        store.persist(name)
        return stats
    except Exception:
        # 失敗時はコードベースディレクトリごとクリーンアップ
        shutil.rmtree(dest, ignore_errors=True)
        raise


def _check_budget(written: int) -> None:
    if written > MAX_TOTAL_BYTES:
        raise SourceError(
            f"合計サイズが上限を超えました（展開後 {written} > {MAX_TOTAL_BYTES} bytes）",
            status_code=413,
        )


def _place_single(filename: str, staged: Path, dest: Path, written: int) -> int:
    safe_name = Path(filename or staged.name).name  # ディレクトリ成分を捨てる
    if is_excluded_file(Path(safe_name)):
        return written
    size = staged.stat().st_size
    if size > MAX_FILE_BYTES:
        return written
    written += size
    _check_budget(written)
    shutil.copyfile(staged, dest / safe_name)
    return written


def _extract_zip(zpath: Path, dest: Path, written: int) -> int:
    dest_resolved = dest.resolve()
    with zipfile.ZipFile(zpath) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            rel = PurePosixPath(info.filename)
            # zip-slip: 絶対パス / .. を弾く
            if rel.is_absolute() or any(part == ".." for part in rel.parts):
                continue
            parts = rel.parts
            # 除外ディレクトリを含む / 除外ファイルはスキップ
            if any(is_excluded_dir(p) for p in parts[:-1]):
                continue
            if is_excluded_file(Path(parts[-1])):
                continue
            if info.file_size > MAX_FILE_BYTES:
                continue
            target = (dest / rel).resolve()
            if target != dest_resolved and dest_resolved not in target.parents:
                continue  # 多重防御: 展開先の外を指すエントリは無視
            written += info.file_size
            _check_budget(written)
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, open(target, "wb") as out:
                shutil.copyfileobj(src, out)
    return written


# ─── 一覧・統計・削除 ────────────────────────────────────────────────


def _build_stats(name: str, index, schema) -> dict:
    return {
        "name": name,
        "file_count": len(index.files),
        "bytes": index.total_bytes(),
        "symbol_count": index.symbol_count(),
        "languages": index.language_breakdown(),
        "table_count": len(schema.tables),
    }


def ensure_local(name: str) -> Path:
    """``name`` のファイル一式をローカルに用意し、その作業ディレクトリを返す。

    GCS backend では未取得なら download する。ローカル backend は no-op。tree-sitter
    走査・index 生成・rally の source tools はすべてこのローカルパス上で動く。
    """
    dest = safe_codebase_dir(name)
    _store().ensure_local(name)
    return dest


def list_codebases() -> list[dict]:
    """コードベース一覧（meta から、無ければ算出）。保存先は store が決める。"""
    store = _store()
    out: list[dict] = []
    for name in store.list_names():
        out.append(stats_for(name))
    return out


def stats_for(name: str) -> dict:
    """1 コードベースの統計。meta があればそれを、無ければ算出してキャッシュし返す。

    直接配置されたコードベース（meta なし）では、毎回の一覧取得で tree-sitter
    解析が走らないよう、インデックスは ``.index.json`` を使い回し、結果を
    ``.meta.json`` に書き出しておく。GCS backend では ``.meta.json`` を直接読んで
    一覧を軽量化し、無い場合のみローカル展開して算出・書き戻す。
    """
    store = _store()
    meta_data = store.read_meta(name)
    if meta_data is not None:
        return meta_data

    dest = ensure_local(name)
    index = get_or_build_index(dest)
    stats = _build_stats(name, index, extract_db_schema(dest))
    try:
        (dest / _META_FILENAME).write_text(
            json.dumps(stats, ensure_ascii=False), encoding="utf-8"
        )
        store.persist(name)
    except OSError:
        pass
    return stats


def exists(name: str) -> bool:
    return _store().exists(name)


def delete_codebase(name: str) -> bool:
    store = _store()
    dest = safe_codebase_dir(name)
    if not store.exists(name):
        return False
    store.delete(name)  # backing store（GCS）側を削除
    shutil.rmtree(dest, ignore_errors=True)  # ローカル作業ディレクトリを削除
    return True


def tree(name: str) -> dict:
    """ファイルツリー（署名のみ）＋ DB スキーマを返す（本文は含めない）。"""
    if not exists(name):
        raise SourceError(f"コードベースが見つかりません: {name}", status_code=404)
    dest = ensure_local(name)
    index = build_source_index(dest)
    schema = extract_db_schema(dest)
    return {
        "name": name,
        "files": [f.model_dump() for f in index.files],
        "db_schema": schema.model_dump(),
        "stats": _build_stats(name, index, schema),
    }


def has_language_files(name: str) -> bool:
    """対象言語のファイルが 1 つでもあるか（UI の有効化判定補助）。"""
    dest = ensure_local(name)
    for p in dest.rglob("*"):
        if p.is_file() and language_for(p) is not None:
            return True
    return False
