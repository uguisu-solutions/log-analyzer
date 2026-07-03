"""構成4 監視エージェント用のツール群（モック実装）。

Phase 2 では実機 / 構成管理 DB を引く想定だが、PoC 期間中は
``samples/topology/*.json`` の固定データから返す。tool 呼び出しは
監視エージェント関数内で予測可能なタイミングで実行し、結果を LLM の
コンテキストとして渡す（LLM 主導のツール呼び出しは Phase 2 後半で検討）。

提供ツール:
- ``read_topology(target_ip)``: ネットワーク／FW 構成（hosts / policy / neighbors）
- ``get_config(service_id)``: サービス別の構成（DNS / Auth / App 等の設定値・既知の問題）
- ``bigquery_query(host, ...)``: BigQuery に投入済みのログを host + 期間で取得
  （こちらは LLM 主導の native tool-use。監視ノードの tool-use ループから呼ばれる）
"""
from __future__ import annotations

import json
import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

# `samples/topology/*.json` を解決する。本ファイルから 5 階層上が repo ルート。
_TOPOLOGY_DIR = Path(__file__).resolve().parents[5] / "samples" / "topology"


@lru_cache(maxsize=1)
def _load_all_topologies() -> list[dict]:
    if not _TOPOLOGY_DIR.exists():
        return []
    return [
        json.loads(p.read_text(encoding="utf-8"))
        for p in sorted(_TOPOLOGY_DIR.glob("*.json"))
        # service_configs.json は別ツールで読むのでここでは除外
        if "service_config" not in p.name
    ]


@lru_cache(maxsize=1)
def _load_service_configs() -> dict[str, dict]:
    """``service_configs.json`` を 1 つにマージ。複数あれば後者が前者を上書き。"""
    out: dict[str, dict] = {}
    if not _TOPOLOGY_DIR.exists():
        return out
    for p in sorted(_TOPOLOGY_DIR.glob("*service_config*.json")):
        data = json.loads(p.read_text(encoding="utf-8"))
        services = data.get("services") or {}
        if isinstance(services, dict):
            out.update(services)
    return out


def read_topology(target_ip: str) -> dict:
    """target_ip 周辺のネットワークトポロジ情報を返すモックツール。

    一致する host エントリが見つからなければ ``matched_topology: null`` で
    返す（LLM はそれを見て「トポロジ未登録」と判断できる）。
    """
    for topology in _load_all_topologies():
        for host in topology.get("hosts", []):
            if host.get("ip") == target_ip:
                return {
                    "matched_topology": topology.get("topology_id"),
                    "host": host,
                    "neighbors": [
                        n for n in topology.get("neighbors", [])
                        if target_ip in (n.get("src"), n.get("dst"))
                    ],
                    "policy": topology.get("policy", {}),
                }
    return {
        "matched_topology": None,
        "note": f"no topology entry found for {target_ip}",
    }


def get_config(service_id: str) -> dict:
    """``service_id`` のサービス構成を返すモックツール。

    `hostname` / `ip` のどちらでも一致を試す。見つからなければ ``matched: null``。
    """
    configs = _load_service_configs()
    if service_id in configs:
        return {"matched": True, "service_id": service_id, "config": configs[service_id]}
    # IP で逆引き
    for sid, cfg in configs.items():
        if cfg.get("ip") == service_id:
            return {"matched": True, "service_id": sid, "config": cfg}
    return {
        "matched": False,
        "service_id": service_id,
        "note": f"no service config found for {service_id}",
    }


# ログから「対象サービス」を雑に拾うためのヒューリスティック
_SERVICE_HOSTNAME_RE = re.compile(r"\b(dns\d+|auth-server|app-server-\d+|fw\d+)\b")


def extract_target_service(log: str, fallback: str = "app-server-1") -> str:
    """ログから注目すべきサービス ID を 1 つ抜き出す（最も頻出のものを採用）。

    監視が ``get_config(service_id)`` を 1 回叩く際の引数決定に使う。
    """
    counts: dict[str, int] = {}
    for m in _SERVICE_HOSTNAME_RE.findall(log):
        counts[m] = counts.get(m, 0) + 1
    if not counts:
        return fallback
    return max(counts.items(), key=lambda kv: kv[1])[0]


# ─── BigQuery 取得ツール (LLM native tool-use) ──────────────────────

# Anthropic Messages API の tools= に渡すスキーマ。raw SQL は受け取らず、
# host / 期間 / 件数 / 部分一致のみをパラメータとして受ける (安全策)。
# スキーマ確認ツール。テーブルは機器ごとに列構成が異なるので、取得前にこれで
# 列名・型・サンプル行を確認してから bigquery_query を呼ぶ。
BIGQUERY_SCHEMA_TOOL_SCHEMA: dict[str, Any] = {
    "name": "bigquery_schema",
    "description": (
        "BigQuery テーブルの列構成 (列名・型) とサンプル行を確認する。テーブルは "
        "機器ごとに列名や構造が異なるため、bigquery_query で取得する前に必ずこれを "
        "呼び、どの列が時刻か・どの列が本文か・絞り込みに使える列は何かを把握する "
        "こと。列定義の取得は課金されない。"
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "host": {
                "type": "string",
                "description": "対象ノードの host (= node id)。許可されたノードのみ指定可。",
            },
            "table": {
                "type": "string",
                "description": (
                    "同一 host に複数テーブルが紐づく場合に、確認するテーブル名を指定する。"
                    "テーブルが 1 つだけなら省略可。指定可能なテーブルは log_text の "
                    "[ログ取得元: BigQuery] 記載を参照。"
                ),
            },
        },
        "required": ["host"],
    },
}

BIGQUERY_TOOL_SCHEMA: dict[str, Any] = {
    "name": "bigquery_query",
    "description": (
        "BigQuery に投入済みのデバイスログを取得する。ログ取得元が BigQuery の "
        "ノード (log_text 内で [ログ取得元: BigQuery] と記載) について、必要な "
        "期間・キーワード・件数だけを絞って取得すること。全件を闇雲に取らず、疑わしい "
        "時間帯・キーワードに絞ること。テーブルの列構成は機器ごとに異なるので、先に "
        "bigquery_schema で列を確認し、time_column / text_column / columns に実在する "
        "列名を指定すること (省略時はノード設定の既定列を使う)。"
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "host": {
                "type": "string",
                "description": "取得対象ノードの host (= node id)。許可されたノードのみ指定可。",
            },
            "table": {
                "type": "string",
                "description": (
                    "同一 host に複数テーブルが紐づく場合に、取得対象テーブル名を指定する。"
                    "テーブルが 1 つだけなら省略可。指定可能なテーブルは log_text の "
                    "[ログ取得元: BigQuery] 記載を参照。先に bigquery_schema で列を確認すること。"
                ),
            },
            "start_time": {
                "type": "string",
                "description": "取得開始時刻 (ISO8601, 例 2026-06-10T09:00:00Z)。省略可。",
            },
            "end_time": {
                "type": "string",
                "description": "取得終了時刻 (ISO8601)。省略可。",
            },
            "limit": {
                "type": "integer",
                "description": "最大取得件数。省略時は既定値。上限を超える指定はクランプされる。",
            },
            "contains": {
                "type": "string",
                "description": "本文列に対する部分一致フィルタ (大小無視)。省略可。",
            },
            "time_column": {
                "type": "string",
                "description": (
                    "start_time/end_time を適用する時刻列名。bigquery_schema で確認した "
                    "実在の列を指定。省略時はノード設定の既定 (timestamp)。"
                ),
            },
            "text_column": {
                "type": "string",
                "description": (
                    "contains を検索する本文列名。実在の列を指定。省略時はノード設定の "
                    "既定 (message)。"
                ),
            },
            "columns": {
                "type": "array",
                "items": {"type": "string"},
                "description": "取得する列名のリスト。省略時は全列。不要列を外すとトークン節約。",
            },
        },
        "required": ["host"],
    },
}


# ─── BQ 取得結果の予算化 (コンテキスト肥大化・parse 失敗の防止) ─────
# 監視のツールループは最大数回 BQ を取得し、結果を全文コンテキストに積む。
# 上限が無いと累積でコンテキストが肥大化し、最終 JSON 出力が壊れて parse 失敗
# (→ integrator フォールバック) を招く。ここで「コンテキストに載せる行数」と
# 「全体文字数」に上限を掛ける。いずれも環境変数で調整可能。
def _bq_env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _max_rows_in_context() -> int:
    # コンテキストに載せる最大行数 (取得自体は bigquery_client 側で別途クランプ)
    return _bq_env_int("RALLY_BQ_MAX_ROWS_IN_CONTEXT", 200)


def _max_result_chars() -> int:
    # 1 回の取得結果テキストの全体文字数上限 (最終セーフティネット)
    return _bq_env_int("RALLY_BQ_RESULT_MAX_CHARS", 12000)


def _truncate_chars(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    keep = max_chars // 2
    omitted = len(text) - max_chars
    return text[:keep] + f"\n...（全体で {omitted} 文字省略）...\n" + text[-keep:]


def _format_rows(host: str, rows: list[dict]) -> str:
    """取得行を表形式テキストに整形する (LLM 向け)。

    列構成はテーブルごとに異なるので列名を決め打ちせず、実際に返ってきた列を
    そのまま使う。トークン節約のため (1) 列名はヘッダで 1 回だけ出し、(2) 全行で
    値が空の列は出力から除外する。
    """
    if not rows:
        return f"BigQuery 取得結果: host={host}, 0 件"
    cols = list(rows[0].keys())

    def _empty(v: Any) -> bool:
        return v is None or str(v).strip() == ""

    nonempty = [c for c in cols if any(not _empty(r.get(c)) for r in rows)]
    lines = [
        f"BigQuery 取得結果: host={host}, {len(rows)} 件",
        f"列: {', '.join(nonempty)}",
    ]
    for r in rows:
        lines.append(" | ".join("" if _empty(r.get(c)) else str(r.get(c)) for c in nonempty))
    return "\n".join(lines)


def _sources_for_host(allowed_sources: dict, host: str) -> list[dict]:
    """host に許可された BQ ソース設定を list で返す。

    許可リストは host→list[dict] (複数テーブル) を基本とするが、後方互換で
    host→dict (単一テーブル・旧形式) も受け付ける。
    """
    v = allowed_sources.get(host)
    if isinstance(v, dict):
        return [v]
    if isinstance(v, list):
        return [s for s in v if isinstance(s, dict)]
    return []


def _resolve_bq_source(
    allowed_sources: dict, host: str, table: str | None
) -> tuple[dict | None, str | None]:
    """(host, table) から許可済みソースを 1 つ解決する。

    返り値: ``(src, None)`` 成功 / ``(None, エラー文)`` 失敗。
    - host が複数テーブルを持つのに table 未指定 → どのテーブルか促すエラー。
    - 指定 table が許可に無い → 許可テーブル一覧を返すエラー。
    """
    sources = _sources_for_host(allowed_sources, host)
    if not sources:
        allowed = ", ".join(sorted(allowed_sources)) or "(なし)"
        return None, f"エラー: host={host!r} は取得が許可されていません。許可された host: {allowed}"
    if table:
        for s in sources:
            if str(s.get("table") or "").strip() == table:
                return s, None
        avail = ", ".join(str(s.get("table") or "(既定)") for s in sources)
        return None, (
            f"エラー: host={host!r} に table={table!r} は許可されていません。"
            f"指定可能なテーブル: {avail}"
        )
    if len(sources) == 1:
        return sources[0], None
    avail = ", ".join(str(s.get("table") or "(既定)") for s in sources)
    return None, (
        f"エラー: host={host!r} には複数のテーブルが紐づいています。"
        f"table を指定してください: {avail}"
    )


def run_bigquery_tool(tool_input: dict, allowed_sources: dict) -> str:
    """``bigquery_query`` tool_use を実行し、tool_result 用の文字列を返す。

    host+table は ``allowed_sources`` (= run の BQ ノードメタデータ) に含まれるものだけ
    許可。table / 既定期間 / 既定件数は解決されたソースのエントリから補完する。
    失敗時もエラー文字列を返し (例外は投げない)、LLM が graceful に判断できるようにする。
    """
    from log_analyzer import bigquery_client

    host = str((tool_input or {}).get("host") or "").strip()
    if not host:
        return "エラー: host は必須です。"
    table_req = str((tool_input or {}).get("table") or "").strip() or None
    src, err = _resolve_bq_source(allowed_sources, host, table_req)
    if err:
        return err
    src = src or {}
    start = tool_input.get("start_time") or src.get("start")
    end = tool_input.get("end_time") or src.get("end")
    limit = tool_input.get("limit") or src.get("limit")
    contains = tool_input.get("contains")
    table = src.get("table")
    # 列構成はテーブルごとに異なる。エージェントが (bigquery_schema で確認した上で)
    # 列を明示した場合はそれを優先し、無ければノード設定 → device_logs 既定 の順で補完。
    host_column = src.get("host_column", "host")
    agent_time = tool_input.get("time_column")
    agent_text = tool_input.get("text_column")
    agent_cols = tool_input.get("columns")
    time_column = agent_time if agent_time not in (None, "") else src.get("time_column", "timestamp")
    text_column = agent_text if agent_text not in (None, "") else src.get("text_column", "message")
    select_columns = (agent_cols or None) or (src.get("columns") or None)
    try:
        rows = bigquery_client.query_logs(
            host, start=start, end=end, limit=limit, contains=contains, table=table,
            host_column=host_column, time_column=time_column, text_column=text_column,
            select_columns=select_columns,
        )
    except Exception as e:  # noqa: BLE001 — LLM に失敗を伝えて継続させる
        return f"エラー: BigQuery 取得に失敗しました: {e}"
    if not rows:
        return (
            f"host={host} の該当ログは見つかりませんでした "
            f"(start={start}, end={end}, contains={contains})。"
        )
    # コンテキスト肥大化を防ぐため、載せる行数と全体文字数に上限を掛ける。
    # 取得自体は最大件数まで行うが、モデルに渡すのは先頭の代表行＋省略注記とする。
    total = len(rows)
    cap = _max_rows_in_context()
    shown = rows[:cap]
    text = _format_rows(host, shown)
    if total > cap:
        text += (
            f"\n...（全 {total} 件中 先頭 {cap} 件のみ表示。残り {total - cap} 件は省略。"
            f"必要なら contains / 期間 でさらに絞り込んでください）..."
        )
    return _truncate_chars(text, _max_result_chars())


def _format_schema(host: str, table: str, schema: list[dict], rows: list[dict]) -> str:
    lines = [f"host={host} のテーブル ({table or '既定テーブル'}) のスキーマ:"]
    lines.append("列 (名前: 型):")
    for c in schema:
        lines.append(f"  - {c.get('name')}: {c.get('type')}")
    if rows:
        cols = list(rows[0].keys())
        lines.append(f"サンプル {len(rows)} 行 (列: {', '.join(cols)}):")
        for r in rows:
            lines.append(" | ".join("" if r.get(c) is None else str(r.get(c)) for c in cols))
    return "\n".join(lines)


def run_bigquery_schema_tool(tool_input: dict, allowed_sources: dict) -> str:
    """``bigquery_schema`` tool_use を実行し、列定義＋サンプル行の文字列を返す。

    host 許可リスト検証は run_bigquery_tool と同じ。列定義の取得 (get_table) は
    課金されないが、サンプル行は少量スキャンする (maximum_bytes_billed で上限あり)。
    失敗時もエラー文字列を返す (例外は投げない)。
    """
    from log_analyzer import bigquery_client

    host = str((tool_input or {}).get("host") or "").strip()
    if not host:
        return "エラー: host は必須です。"
    table_req = str((tool_input or {}).get("table") or "").strip() or None
    src, err = _resolve_bq_source(allowed_sources, host, table_req)
    if err:
        return err
    src = src or {}
    table = src.get("table")
    try:
        schema = bigquery_client.table_schema(table=table)
    except Exception as e:  # noqa: BLE001
        return f"エラー: スキーマ取得に失敗しました: {e}"
    rows: list[dict] = []
    try:
        rows = bigquery_client.sample_rows(table=table, limit=3)
    except Exception as e:  # noqa: BLE001 — サンプルは任意。失敗してもスキーマは返す
        rows = []
        schema_note = f"\n(サンプル行の取得に失敗: {e})"
        return _format_schema(host, table, schema, rows) + schema_note
    return _format_schema(host, table, schema, rows)
