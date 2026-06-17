"""BigQuery ログ取得ルートのクライアント (取得は Google 公式 MCP 経由)。

config-log 解析の「BigQuery 取得」ノード向けに、事前に投入済みのログ行を
host + 期間で絞って取得する。設計方針:

- **取得 (SELECT) は Google 公式の MCP サーバー (MCP Toolbox for Databases) 経由**
  で実行する (``bigquery_mcp`` モジュール)。google-cloud-bigquery 直叩きは
  取得経路から撤去した。LLM には引き続き raw SQL を書かせず、本モジュールが
  host / 期間 / キーワードから SELECT 文を組み立てる。
- MCP の ``bigquery-execute-sql`` は raw SQL 文字列を受け取りクエリパラメータを
  渡せないため、値は ``_sql_string_literal`` で **エスケープしてリテラル化** する
  (host は呼び出し側で許可リスト検証済み、列名は ``_safe_ident`` で識別子検証)。
- ``maximum_bytes_billed`` は MCP では指定できないので、本実行前に ``dry_run`` で
  スキャン量を見積もり、上限超過なら中止する形で温存する。
- ``host`` の許可リスト検証は呼び出し側 (rally/tools.py の bigquery_query) が行う。

ログの投入 (バルクロード) は MCP の対象外なので、``ensure_table`` /
``get_client`` / ``insert_rows_json`` 経路は従来どおり google-cloud-bigquery を
使う (scripts/ingest_logs_to_bq.py から利用)。

設定は環境変数から読む (呼び出し時に評価。`api.py` / `cli.py` の
`load_dotenv()` 後に効く):

- ``GOOGLE_APPLICATION_CREDENTIALS``: サービスアカウント JSON キーのパス
  (MCP サーバー / 投入の双方が ADC として利用)
- ``BIGQUERY_PROJECT`` / ``BIGQUERY_DATASET`` / ``BIGQUERY_LOGS_TABLE``
- ``BIGQUERY_MAX_BYTES_BILLED`` / ``BIGQUERY_DEFAULT_LIMIT`` / ``BIGQUERY_MAX_LIMIT``
- MCP サーバー起動設定は ``bigquery_mcp`` 参照 (``BIGQUERY_MCP_*``)
"""
from __future__ import annotations

import json
import os
from typing import Any

_DEFAULT_TABLE = "device_logs"
_DEFAULT_DATASET = "network_logs"
# BigQuery のロケーション。データセットの保存リージョン兼ジョブ実行ロケーション。
# 国内データは東京リージョンに置く (US マルチリージョンを避ける)。
_DEFAULT_LOCATION = "asia-northeast1"
_FALLBACK_DEFAULT_LIMIT = 500
_FALLBACK_MAX_LIMIT = 2000
_FALLBACK_MAX_BYTES_BILLED = 1024 * 1024 * 1024  # 1 GiB

# 取得行 1 件のスキーマ (ingestion と一致させる)
LOG_COLUMNS = ("host", "timestamp", "severity", "source", "message", "line_no")


def _env_int(name: str, fallback: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return fallback
    try:
        return int(raw)
    except ValueError:
        return fallback


def default_limit() -> int:
    return _env_int("BIGQUERY_DEFAULT_LIMIT", _FALLBACK_DEFAULT_LIMIT)


def max_limit() -> int:
    return _env_int("BIGQUERY_MAX_LIMIT", _FALLBACK_MAX_LIMIT)


def max_bytes_billed() -> int:
    return _env_int("BIGQUERY_MAX_BYTES_BILLED", _FALLBACK_MAX_BYTES_BILLED)


def project_id() -> str | None:
    return os.environ.get("BIGQUERY_PROJECT") or None


def dataset_id() -> str:
    return os.environ.get("BIGQUERY_DATASET") or _DEFAULT_DATASET


def default_table() -> str:
    return os.environ.get("BIGQUERY_LOGS_TABLE") or _DEFAULT_TABLE


def location() -> str:
    return os.environ.get("BIGQUERY_LOCATION") or _DEFAULT_LOCATION


def _normalize_table(table: str | None) -> str:
    """テーブル名を ``project.dataset.table`` 形式に正規化する。

    呼び出し側からは末尾のテーブル名のみ (例 ``device_logs``) を受け取り、
    project/dataset は環境変数で補う。既に完全修飾 (ドットを含む) の場合は
    そのまま使う。SQL に直接埋めるので、識別子として安全な文字のみ許可する。
    """
    name = (table or default_table()).strip()
    if not name:
        name = default_table()
    parts = name.split(".")
    for p in parts:
        if not p or not all(c.isalnum() or c in ("_", "-") for c in p):
            raise ValueError(f"不正なテーブル名: {table!r}")
    if len(parts) == 3:
        proj, ds, tbl = parts
    elif len(parts) == 2:
        proj, ds, tbl = (project_id(), parts[0], parts[1])
    else:
        proj, ds, tbl = (project_id(), dataset_id(), parts[0])
    fq = f"{ds}.{tbl}"
    if proj:
        fq = f"{proj}.{fq}"
    return fq


def _clamp_limit(limit: int | None) -> int:
    if not limit or limit <= 0:
        return default_limit()
    return min(int(limit), max_limit())


def _safe_ident(name: str) -> str:
    """列名を識別子として検証する (SQL インジェクション対策)。

    実テーブルは日本語 (半角カナ等) の列名を持つため英数字限定にはできない。
    識別子は SQL ではバッククォートで囲む (``_quote_ident``) 前提で、ここでは
    クォートを破壊する **バッククォート** と **制御文字** のみを拒否する。
    """
    s = str(name).strip()
    if not s or "`" in s or any(ord(c) < 0x20 for c in s):
        raise ValueError(f"不正な列名: {name!r}")
    return s


def _quote_ident(name: str) -> str:
    """列名を検証のうえバッククォートで囲む (BigQuery の識別子クォート)。

    日本語・ハイフン等を含む列名でも安全に SQL へ埋め込めるようにする。
    """
    return f"`{_safe_ident(name)}`"


def _sql_string_literal(value: Any) -> str:
    """値を BigQuery の安全な文字列リテラル (シングルクォート) に変換する。

    MCP の ``bigquery-execute-sql`` は raw SQL のみでクエリパラメータを渡せない
    ため、host / 期間 / contains の値はここでエスケープしてリテラル化する。
    バックスラッシュ・引用符・改行をエスケープし、制御文字混入による
    インジェクションを防ぐ。
    """
    s = str(value)
    s = (
        s.replace("\\", "\\\\")
        .replace("'", "\\'")
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )
    return f"'{s}'"


def build_query(
    host: str | None = None,
    *,
    start: str | None = None,
    end: str | None = None,
    limit: int | None = None,
    contains: str | None = None,
    table: str | None = None,
    host_column: str | None = "host",
    time_column: str | None = "timestamp",
    text_column: str | None = "message",
    select_columns: list[str] | None = None,
) -> str:
    """SELECT 文を組み立てる純粋関数 (テスト容易性のため分離)。

    テーブルごとに列構成が異なる前提で、**設定された列だけ**を絞り込みに使う:

    - ``host_column``: host で絞る列。空なら絞らない (1 テーブル = 1 機器のケース)。
    - ``time_column``: 期間で絞る列。空なら期間指定不可。
    - ``text_column``: ``contains`` が検索する列。空なら contains 不可。
    - ``select_columns``: 取得列。空/None なら全列 (``*``)。

    既定値は自前 ingest した ``device_logs`` 用 (host/timestamp/message) なので、
    引数を省略すれば従来挙動と一致する。MCP の ``bigquery-execute-sql`` は raw SQL
    のみでクエリパラメータを渡せないため、値は ``_sql_string_literal`` で
    エスケープしてリテラル化する (列名は ``_safe_ident`` で識別子検証)。
    """
    fq_table = _normalize_table(table)
    n = _clamp_limit(limit)

    where: list[str] = []

    # host 列があるテーブルだけ host で絞る。無ければテーブル選択自体が機器を限定。
    hcol = (host_column or "").strip()
    if hcol:
        if not host or not str(host).strip():
            raise ValueError("host 列を持つテーブルでは host は必須です")
        where.append(f"{_quote_ident(hcol)} = {_sql_string_literal(host)}")

    tcol = (time_column or "").strip()
    if (start or end) and not tcol:
        raise ValueError("時刻列 (time_column) が無いため期間指定はできません")
    if start:
        where.append(f"{_quote_ident(tcol)} >= TIMESTAMP({_sql_string_literal(start)})")
    if end:
        where.append(f"{_quote_ident(tcol)} <= TIMESTAMP({_sql_string_literal(end)})")

    if contains:
        xcol = (text_column or "").strip()
        if not xcol:
            raise ValueError("本文列 (text_column) が無いため contains 指定はできません")
        # 部分一致は CONTAINS_SUBSTR (大小無視)。値はエスケープしてリテラル化。
        where.append(
            f"CONTAINS_SUBSTR({_quote_ident(xcol)}, {_sql_string_literal(contains)})"
        )

    # 取得列。未指定なら全列。指定時は識別子として検証しバッククォートで囲む。
    if select_columns:
        cols = [_quote_ident(c) for c in select_columns if str(c).strip()]
        select_clause = ", ".join(cols) if cols else "*"
    else:
        select_clause = "*"

    where_clause = f"\nWHERE {' AND '.join(where)}" if where else ""
    order_clause = f"\nORDER BY {_quote_ident(tcol)}" if tcol else ""

    return (
        f"SELECT {select_clause}\n"
        f"FROM `{fq_table}`"
        f"{where_clause}"
        f"{order_clause}\n"
        f"LIMIT {n}"
    )


# ─── 実行レイヤ (取得は MCP 経由 / 投入は google-cloud-bigquery) ──────

_client = None  # プロセス内シングルトン (投入用)


def get_client():
    """`bigquery.Client` を遅延初期化して返す (投入専用、プロセス内で再利用)。

    取得 (SELECT) は MCP 経由なので使わない。ログ投入 (``ensure_table`` /
    ``insert_rows_json``) のみがこのクライアントを使う。認証はサービス
    アカウント JSON キー (``GOOGLE_APPLICATION_CREDENTIALS``)。
    """
    global _client
    if _client is None:
        from google.cloud import bigquery  # 遅延 import

        # location をクライアント既定にすると load ジョブもこのリージョンで実行される
        _client = bigquery.Client(project=project_id(), location=location())
    return _client


def _extract_total_bytes(info: Any) -> int | None:
    """dry_run 結果からスキャン予定バイト数を取り出す (見つからなければ None)。

    toolbox の dry_run 応答は BigQuery の job リソース JSON で、バイト数は
    ``statistics`` 配下にネストし、さらに JSON 文字列として二重エンコードされて
    返ることがある。再帰的に走査し、JSON 文字列に出会ったらパースして潜る。
    見つかった候補のうち最大値を返す (totalBytesProcessed 等)。
    """
    candidates = {"totalBytesProcessed", "total_bytes_processed", "bytesProcessed",
                  "bytes_processed", "totalBytesBilled", "total_bytes_billed"}
    found: list[int] = []

    def _walk(obj: Any) -> None:
        if isinstance(obj, str):
            s = obj.strip()
            if s[:1] in ("{", "["):
                try:
                    _walk(json.loads(s))
                except (json.JSONDecodeError, ValueError):
                    pass
            return
        if isinstance(obj, dict):
            for k, v in obj.items():
                if k in candidates:
                    try:
                        found.append(int(v))
                    except (TypeError, ValueError):
                        pass
                _walk(v)
        elif isinstance(obj, list):
            for v in obj:
                _walk(v)

    _walk(info)
    return max(found) if found else None


def _enforce_byte_limit(sql: str) -> None:
    """``maximum_bytes_billed`` 相当: dry_run でスキャン量を見積もり上限超を中止。

    MCP では ``maximum_bytes_billed`` を直接指定できないため、本実行前に dry_run で
    見積もる。dry_run 自体に失敗した場合やバイト数を読めなかった場合は、過剰に
    止めないよう本実行に委ねる (toolbox 側の allowedDatasets/writeMode を併用前提)。
    """
    from log_analyzer import bigquery_mcp  # 遅延 import (mcp 依存をテストから切離す)

    limit = max_bytes_billed()
    try:
        info = bigquery_mcp.execute_sql(sql, dry_run=True)
    except Exception:  # noqa: BLE001 — dry_run 失敗時は本実行に委ねる
        return
    total = _extract_total_bytes(info)
    if total is not None and total > limit:
        raise RuntimeError(
            f"スキャン予定 {total} bytes が上限 {limit} bytes (BIGQUERY_MAX_BYTES_BILLED) "
            f"を超えるため取得を中止しました。期間/キーワードで絞り込んでください。"
        )


def query_logs(
    host: str | None = None,
    *,
    start: str | None = None,
    end: str | None = None,
    limit: int | None = None,
    contains: str | None = None,
    table: str | None = None,
    host_column: str | None = "host",
    time_column: str | None = "timestamp",
    text_column: str | None = "message",
    select_columns: list[str] | None = None,
) -> list[dict[str, Any]]:
    """host + 期間でログ行を取得して dict のリストで返す (MCP 経由)。

    host の許可リスト検証は呼び出し側の責務。SELECT 文は ``build_query`` で組み立て、
    本実行前に dry_run でスキャン量上限を確認してから MCP の ``bigquery-execute-sql``
    で実行する。列構成の違い (``host_column`` / ``time_column`` / ``text_column`` /
    ``select_columns``) はそのまま ``build_query`` へ渡す。
    """
    from log_analyzer import bigquery_mcp  # 遅延 import

    sql = build_query(
        host, start=start, end=end, limit=limit, contains=contains, table=table,
        host_column=host_column, time_column=time_column, text_column=text_column,
        select_columns=select_columns,
    )
    _enforce_byte_limit(sql)
    rows = bigquery_mcp.execute_sql(sql)
    return [_row_to_jsonable(row) for row in rows]


def _row_to_jsonable(row: Any) -> dict[str, Any]:
    """取得行を JSON 化可能な dict に変換する。

    MCP からは概ね JSON 化済みで返るが、日時系 (isoformat を持つ値) が混じる場合は
    ISO 文字列へ変換する。列名はテーブルごとに異なるので決め打ちしない。
    """
    d = dict(row)
    for k, v in d.items():
        if v is not None and hasattr(v, "isoformat"):
            d[k] = v.isoformat()
    return d


def table_schema(table: str | None = None) -> list[dict[str, str]]:
    """テーブルの列定義 (name/type) を返す (MCP 経由)。

    ``INFORMATION_SCHEMA.COLUMNS`` を SELECT して列名・型を得る。メタデータ
    クエリなのでスキャン量はごく僅か。エージェントが取得前に列構成を確認する用。
    ``bigquery-execute-sql`` 1 本に集約することで prebuilt 他ツールの引数仕様に
    依存しない。
    """
    from log_analyzer import bigquery_mcp  # 遅延 import

    fq = _normalize_table(table)
    parts = fq.split(".")
    tbl = parts[-1]
    schema_prefix = ".".join(parts[:-1])  # project.dataset または dataset
    info_table = f"`{schema_prefix}.INFORMATION_SCHEMA.COLUMNS`"
    sql = (
        f"SELECT column_name AS name, data_type AS type\n"
        f"FROM {info_table}\n"
        f"WHERE table_name = {_sql_string_literal(tbl)}\n"
        f"ORDER BY ordinal_position"
    )
    rows = bigquery_mcp.execute_sql(sql)
    return [{"name": str(r.get("name")), "type": str(r.get("type"))} for r in rows]


def sample_rows(
    table: str | None = None,
    *,
    limit: int = 3,
    select_columns: list[str] | None = None,
) -> list[dict[str, Any]]:
    """先頭数行をサンプル取得する (MCP 経由、エージェントがデータ形状を把握する用)。

    本実行前に dry_run でスキャン量上限を確認する。件数は安全のため 1〜10 に
    クランプする。
    """
    from log_analyzer import bigquery_mcp  # 遅延 import

    fq = _normalize_table(table)
    n = max(1, min(int(limit or 3), 10))
    if select_columns:
        cols = [_quote_ident(c) for c in select_columns if str(c).strip()]
        sel = ", ".join(cols) if cols else "*"
    else:
        sel = "*"
    sql = f"SELECT {sel}\nFROM `{fq}`\nLIMIT {n}"
    _enforce_byte_limit(sql)
    rows = bigquery_mcp.execute_sql(sql)
    return [_row_to_jsonable(row) for row in rows]


def ensure_table() -> str:
    """データセット/テーブルを idempotent に作成する (ingestion 用)。

    返り値は完全修飾テーブル名。`PARTITION BY DATE(timestamp)` +
    `CLUSTER BY host` でスキャン量を抑える設計。
    """
    from google.cloud import bigquery

    client = get_client()
    ds_id = dataset_id()
    proj = project_id() or client.project
    dataset_ref = bigquery.Dataset(f"{proj}.{ds_id}")
    dataset_ref.location = location()
    client.create_dataset(dataset_ref, exists_ok=True)

    fq_table = _normalize_table(default_table())
    schema = [
        bigquery.SchemaField("host", "STRING", mode="REQUIRED"),
        bigquery.SchemaField("timestamp", "TIMESTAMP", mode="REQUIRED"),
        bigquery.SchemaField("severity", "STRING"),
        bigquery.SchemaField("source", "STRING"),
        bigquery.SchemaField("message", "STRING"),
        bigquery.SchemaField("line_no", "INT64"),
        bigquery.SchemaField("ingested_at", "TIMESTAMP"),
    ]
    tbl = bigquery.Table(fq_table, schema=schema)
    tbl.time_partitioning = bigquery.TimePartitioning(
        type_=bigquery.TimePartitioningType.DAY, field="timestamp"
    )
    tbl.clustering_fields = ["host"]
    client.create_table(tbl, exists_ok=True)
    return fq_table
