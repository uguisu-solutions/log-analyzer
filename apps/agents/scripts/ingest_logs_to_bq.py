r"""ローカルのログファイルを BigQuery (device_logs) へ投入する CLI。

「予め BQ にログを上げておき、解析時に必要分だけ取得する」運用の前段。
ブラウザ経由の巨大アップロード (UI フリーズ要因) を避けるため、投入は
このオフライン CLI で行う。

使い方:
    cd apps\agents
    .\.venv\Scripts\Activate.ps1
    # .env に GOOGLE_APPLICATION_CREDENTIALS / BIGQUERY_PROJECT / BIGQUERY_DATASET を設定

    # 単一ファイル (host を明示)
    python scripts\ingest_logs_to_bq.py --host fw-01 ..\..\samples\topology\scenario2_api_acl_missing\fw-01.log

    # 複数ファイル (host はファイル名の stem から推定: fw-01.log -> fw-01)
    python scripts\ingest_logs_to_bq.py ..\..\samples\topology\scenario2_api_acl_missing\*.log

行ごとに 1 レコード (host / timestamp / severity / source / message / line_no /
ingested_at) として投入する。timestamp は行頭からのベストエフォートパース、
失敗時は投入時刻 (ingested_at と同じ) を用いる。

文字コードは ``--encoding`` で指定 (既定 ``auto``: utf-8 → cp932 等を strict で
自動判定)。``errors="replace"`` は使わないので、判定に失敗したら黙って文字化け
させず終了コード 3 で止まる。日本語 (Shift-JIS) ログは ``--encoding cp932`` でも可:

    python scripts\\ingest_logs_to_bq.py --host ADServer --encoding cp932 ad.log
"""
from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

from log_analyzer import bigquery_client

# 行頭の代表的なタイムスタンプ表記をベストエフォートで拾う
_TS_PATTERNS = [
    # ISO8601: 2026-06-10T09:00:00 / 2026-06-10 09:00:00(.123)(Z|+09:00)
    (re.compile(r"^(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?)"),
     None),
    # syslog: "Jun 10 09:00:00" (年が無いので現在年を補う)
    (re.compile(r"^([A-Z][a-z]{2}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2})"),
     "%b %d %H:%M:%S"),
]

_SEVERITY_RE = re.compile(r"\b(EMERG|ALERT|CRIT|CRITICAL|ERROR|ERR|WARN|WARNING|NOTICE|INFO|DEBUG)\b",
                          re.IGNORECASE)


def _parse_timestamp(line: str, now: datetime) -> tuple[str, bool]:
    """行頭からタイムスタンプを推定。返り値 (iso文字列, パース成功か)。失敗時は now。"""
    for rx, fmt in _TS_PATTERNS:
        m = rx.match(line)
        if not m:
            continue
        raw = m.group(1)
        try:
            if fmt is None:
                # ISO: fromisoformat (Z は明示変換)
                dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            else:
                dt = datetime.strptime(raw, fmt).replace(year=now.year)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.isoformat(), True
        except ValueError:
            continue
    return now.isoformat(), False


def _extract_severity(line: str) -> str | None:
    m = _SEVERITY_RE.search(line)
    return m.group(1).upper() if m else None


# 文字コード自動判定で試す候補 (日本語ログでよくある順)。utf-8 を先に試すのが肝心:
# Shift-JIS のバイト列は通常 utf-8 strict で失敗するので cp932 へフォールバックする。
_ENCODING_CANDIDATES = ("utf-8-sig", "utf-8", "cp932", "shift_jis", "euc_jp")


def _read_text(path: Path, encoding: str) -> str:
    """ログファイルを読む。`errors="replace"` は使わない (黙って文字化けさせない)。

    - ``encoding="auto"``: 候補を strict で順に試し、最初に成功したものを採用。
    - それ以外: 指定コーデックで strict 読み込み (失敗は UnicodeDecodeError)。

    復元不能な文字化け (U+FFFD 置換) を投入段階で防ぐのが目的。デコードに失敗したら
    黙って潰さず例外にし、``--encoding`` で明示させる。
    """
    data = path.read_bytes()
    if encoding and encoding != "auto":
        return data.decode(encoding)  # strict
    last_err: UnicodeDecodeError | None = None
    for enc in _ENCODING_CANDIDATES:
        try:
            return data.decode(enc)
        except UnicodeDecodeError as e:
            last_err = e
    raise ValueError(
        f"{path}: 文字コードを自動判定できません。--encoding で明示してください "
        f"(試行: {', '.join(_ENCODING_CANDIDATES)})。詳細: {last_err}"
    )


def _rows_from_file(path: Path, host: str, now: datetime, encoding: str = "auto") -> list[dict]:
    rows: list[dict] = []
    text = _read_text(path, encoding)
    now_iso = now.isoformat()
    for i, raw_line in enumerate(text.splitlines(), 1):
        line = raw_line.rstrip("\n")
        if not line.strip():
            continue
        ts, _ok = _parse_timestamp(line, now)
        rows.append({
            "host": host,
            "timestamp": ts,
            "severity": _extract_severity(line),
            "source": path.name,
            "message": line,
            "line_no": i,
            "ingested_at": now_iso,
        })
    return rows


def _host_for(path: Path, override: str | None) -> str:
    if override:
        return override
    return path.stem  # fw-01.log -> fw-01


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description="ローカルログを BigQuery に投入する")
    parser.add_argument("files", nargs="+", help="投入するログファイル (複数可)")
    parser.add_argument("--host", default=None,
                        help="全ファイルに適用する host。省略時はファイル名 stem を使用")
    parser.add_argument("--encoding", default="auto",
                        help="ログの文字コード。既定 auto (utf-8→cp932 等を strict で自動判定)。"
                             "日本語ログが Shift-JIS なら cp932 を明示可。errors=replace は使わない")
    parser.add_argument("--dry-run", action="store_true",
                        help="BQ へ投入せず、組み立てた行数のみ表示")
    args = parser.parse_args(argv)

    now = datetime.now(timezone.utc)
    paths = [Path(f) for f in args.files]
    missing = [p for p in paths if not p.is_file()]
    if missing:
        print(f"ファイルが見つかりません: {', '.join(str(p) for p in missing)}", file=sys.stderr)
        return 2

    all_rows: list[dict] = []
    for p in paths:
        host = _host_for(p, args.host)
        try:
            rows = _rows_from_file(p, host, now, args.encoding)
        except (UnicodeDecodeError, ValueError) as e:
            # 黙って文字化けさせず、ここで失敗を知らせる (--encoding で明示させる)
            print(f"文字コードエラー: {e}", file=sys.stderr)
            return 3
        print(f"  {p}  -> host={host}, {len(rows)} 行")
        all_rows.extend(rows)

    if not all_rows:
        print("投入対象の行がありません。", file=sys.stderr)
        return 1

    if args.dry_run:
        print(f"[dry-run] 合計 {len(all_rows)} 行 (投入はスキップ)")
        return 0

    table = bigquery_client.ensure_table()
    client = bigquery_client.get_client()
    errors = client.insert_rows_json(table, all_rows)
    if errors:
        print(f"投入エラー: {errors}", file=sys.stderr)
        return 1
    print(f"完了: {table} に {len(all_rows)} 行を投入しました。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
