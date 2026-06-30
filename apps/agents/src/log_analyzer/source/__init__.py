"""ソースコード解析サブパッケージ（Phase 1: 決定論インデックス ＋ DBスキーマ抽出）。

設計: docs/plan/source_code_analysis.md

- indexer:   コードベースを走査し、ファイル一覧とシンボル署名を抽出。search/read を提供。
- db_schema: SQL DDL（sqlparse）と ORM モデルから DB 構造を抽出・マージ。
"""
from log_analyzer.source.db_schema import extract_db_schema
from log_analyzer.source.indexer import (
    SourceIndex,
    build_source_index,
    load_index,
    save_index,
)

__all__ = [
    "SourceIndex",
    "build_source_index",
    "load_index",
    "save_index",
    "extract_db_schema",
]
