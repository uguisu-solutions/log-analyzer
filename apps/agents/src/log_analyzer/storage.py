"""SQLite ベースのユーザー定義構成保存。

Phase 2 W6 で導入。当初計画の DynamoDB の代替（AWS 不使用方針 2026-05-07）。

スキーマ:
    saved_configs(id INTEGER, name TEXT UNIQUE, base_config TEXT,
                  overrides_json TEXT, model_overrides_json TEXT,
                  pipeline_json TEXT, created_at TEXT, updated_at TEXT)

- ``overrides_json``: ``{slot_id: 上書きプロンプト}``（config1〜4 用）
- ``model_overrides_json``: ``{slot_id: 上書きモデル名}``（config1〜4 用）
- ``pipeline_json``: ノード定義 JSON 全体（config5 用、UI で組み立てたパイプライン）
slot に上書きが無ければデフォルトが使われる（[prompt_slots.py](prompt_slots.py) 参照）。

ファイルパス: ``apps/agents/data/results.sqlite3``
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

# api.py から見ても scripts/ から見ても同じ場所に向くように絶対解決
_DB_PATH = Path(__file__).resolve().parents[2] / "data" / "results.sqlite3"


def _ensure_dir() -> None:
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)


def init_db() -> None:
    """テーブルが無ければ作成（idempotent）。条件付きマイグレーション付き。

    Phase 2 W6 で ``model_overrides_json`` を追加、`feature/visual-pipeline-builder`
    で ``pipeline_json`` を追加、`feature/run-history` で ``run_history`` テーブル追加。
    既存テーブルにカラムが無ければ ALTER TABLE で増やす（保存データは保持）。
    ``model_overrides_json`` が無いさらに古いスキーマだけは DROP して再作成する。
    """
    _ensure_dir()
    with _connect() as conn:
        rows = conn.execute("PRAGMA table_info(saved_configs)").fetchall()
        cols = {r[1] for r in rows}
        if cols and "model_overrides_json" not in cols:
            # かなり古いスキーマ → DROP（PoC 期間中の互換性は保証しない）
            conn.execute("DROP TABLE IF EXISTS saved_configs")
            cols = set()
        if not cols:
            conn.execute(
                """
                CREATE TABLE saved_configs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    base_config TEXT NOT NULL,
                    overrides_json TEXT NOT NULL,
                    model_overrides_json TEXT NOT NULL DEFAULT '{}',
                    pipeline_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
        elif "pipeline_json" not in cols:
            # config5 用カラム追加（ALTER TABLE で既存データ保持）
            conn.execute(
                "ALTER TABLE saved_configs ADD COLUMN pipeline_json TEXT NOT NULL DEFAULT '{}'"
            )
        # 旧 A-2 の saved_prompts テーブルは現スキーマでは不要
        conn.execute("DROP TABLE IF EXISTS saved_prompts")

        # run_history テーブル（実行 1 件 1 行のメタデータのみ。詳細は Langfuse へ）
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS run_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at TEXT NOT NULL,
                log_name TEXT NOT NULL,
                config_id TEXT NOT NULL,
                base_config TEXT NOT NULL,
                confidence REAL,
                tokens_in INTEGER,
                tokens_out INTEGER,
                latency_ms INTEGER,
                trace_id TEXT,
                top_category TEXT,
                top_summary TEXT
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_run_history_started_at "
            "ON run_history(started_at DESC)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_run_history_config "
            "ON run_history(config_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_run_history_log "
            "ON run_history(log_name)"
        )
        conn.commit()


@contextmanager
def _connect() -> Iterator[sqlite3.Connection]:
    _ensure_dir()
    conn = sqlite3.connect(_DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_to_config(row: sqlite3.Row) -> dict:
    d = dict(row)
    raw_p = d.pop("overrides_json")
    raw_m = d.pop("model_overrides_json", None)
    raw_pipe = d.pop("pipeline_json", None)
    d["overrides"] = json.loads(raw_p) if raw_p else {}
    d["model_overrides"] = json.loads(raw_m) if raw_m else {}
    d["pipeline"] = json.loads(raw_pipe) if raw_pipe and raw_pipe != "{}" else None
    return d


_COLS = (
    "id, name, base_config, overrides_json, model_overrides_json, pipeline_json, "
    "created_at, updated_at"
)


def list_saved_configs() -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT {_COLS} FROM saved_configs ORDER BY updated_at DESC"
        ).fetchall()
    return [_row_to_config(r) for r in rows]


def get_saved_config(config_id: int) -> dict | None:
    with _connect() as conn:
        row = conn.execute(
            f"SELECT {_COLS} FROM saved_configs WHERE id = ?",
            (config_id,),
        ).fetchone()
    return _row_to_config(row) if row else None


def create_saved_config(
    name: str,
    base_config: str,
    overrides: dict[str, str],
    model_overrides: dict[str, str] | None = None,
    pipeline: dict | None = None,
) -> dict:
    now = _now_iso()
    p = json.dumps(overrides, ensure_ascii=False)
    m = json.dumps(model_overrides or {}, ensure_ascii=False)
    pipe = json.dumps(pipeline or {}, ensure_ascii=False) if pipeline else "{}"
    with _connect() as conn:
        cursor = conn.execute(
            "INSERT INTO saved_configs (name, base_config, overrides_json, model_overrides_json, "
            "pipeline_json, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (name, base_config, p, m, pipe, now, now),
        )
        conn.commit()
        config_id = cursor.lastrowid
    saved = get_saved_config(config_id) if config_id else None
    if saved is None:
        raise RuntimeError("failed to retrieve saved config after insert")
    return saved


def update_saved_config(
    config_id: int,
    overrides: dict[str, str],
    model_overrides: dict[str, str] | None = None,
    pipeline: dict | None = None,
) -> dict | None:
    now = _now_iso()
    p = json.dumps(overrides, ensure_ascii=False)
    m = json.dumps(model_overrides or {}, ensure_ascii=False)
    pipe = json.dumps(pipeline or {}, ensure_ascii=False) if pipeline else "{}"
    with _connect() as conn:
        cursor = conn.execute(
            "UPDATE saved_configs SET overrides_json = ?, model_overrides_json = ?, "
            "pipeline_json = ?, updated_at = ? WHERE id = ?",
            (p, m, pipe, now, config_id),
        )
        conn.commit()
        if cursor.rowcount == 0:
            return None
    return get_saved_config(config_id)


def delete_saved_config(config_id: int) -> bool:
    with _connect() as conn:
        cursor = conn.execute("DELETE FROM saved_configs WHERE id = ?", (config_id,))
        conn.commit()
        return cursor.rowcount > 0


# ─── run_history ─────────────────────────────────────────────────────


_RUN_COLS = (
    "id, started_at, log_name, config_id, base_config, confidence, "
    "tokens_in, tokens_out, latency_ms, trace_id, top_category, top_summary"
)


def insert_run_history(
    *,
    log_name: str,
    config_id: str,
    base_config: str,
    confidence: float | None,
    tokens_in: int | None,
    tokens_out: int | None,
    latency_ms: int | None,
    trace_id: str | None,
    top_category: str | None,
    top_summary: str | None,
    started_at: str | None = None,
) -> int:
    """実行 1 件分のメタデータを記録し、行 ID を返す。"""
    when = started_at or _now_iso()
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO run_history (started_at, log_name, config_id, base_config, "
            "confidence, tokens_in, tokens_out, latency_ms, trace_id, top_category, top_summary) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                when, log_name, config_id, base_config, confidence,
                tokens_in, tokens_out, latency_ms, trace_id, top_category, top_summary,
            ),
        )
        conn.commit()
        return cur.lastrowid or 0


def list_run_history(
    *,
    log_name: str | None = None,
    config_id: str | None = None,
    base_config: str | None = None,
    q: str | None = None,
    limit: int = 200,
    offset: int = 0,
) -> tuple[list[dict], int]:
    """フィルタ付きで一覧を返す。``(rows, total_count)`` のタプル。

    - ``log_name`` / ``config_id`` / ``base_config``: 完全一致
    - ``q``: top_summary に部分一致
    """
    where: list[str] = []
    args: list = []
    if log_name:
        where.append("log_name = ?")
        args.append(log_name)
    if config_id:
        where.append("config_id = ?")
        args.append(config_id)
    if base_config:
        where.append("base_config = ?")
        args.append(base_config)
    if q:
        where.append("(top_summary LIKE ? OR log_name LIKE ?)")
        like = f"%{q}%"
        args.extend([like, like])
    where_sql = (" WHERE " + " AND ".join(where)) if where else ""

    with _connect() as conn:
        total = conn.execute(
            f"SELECT COUNT(*) FROM run_history{where_sql}", args
        ).fetchone()[0]
        rows = conn.execute(
            f"SELECT {_RUN_COLS} FROM run_history{where_sql} "
            f"ORDER BY started_at DESC LIMIT ? OFFSET ?",
            (*args, limit, offset),
        ).fetchall()
    return [dict(r) for r in rows], int(total)


def get_run_history(run_id: int) -> dict | None:
    with _connect() as conn:
        row = conn.execute(
            f"SELECT {_RUN_COLS} FROM run_history WHERE id = ?", (run_id,)
        ).fetchone()
    return dict(row) if row else None


def delete_run_history(run_id: int) -> bool:
    with _connect() as conn:
        cursor = conn.execute("DELETE FROM run_history WHERE id = ?", (run_id,))
        conn.commit()
        return cursor.rowcount > 0
