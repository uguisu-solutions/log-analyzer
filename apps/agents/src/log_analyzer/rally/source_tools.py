"""監視エージェント用のソースコード オンデマンド参照ツール（Phase 2）。

BigQuery ツール（[rally/tools.py](tools.py)）を範とした native tool-use。各監視ノードが
tool-use ループの中で、自分の観点で必要なソースだけを取得する:

- ``source_search(query, lang?, limit?)``  関連ファイル・関数を検索（**署名のみ**、本文なし）
- ``source_read(path, symbol?)``           特定ファイル/関数の本文を取得（関数スライス、上限つき）
- ``db_schema(table?)``                     DB スキーマの詳細を取得（log_text には要約のみ注入）

input トークン肥大の抑制（設計: docs/plan/source_code_analysis.md §3）:
- ``source_search`` は本文を返さない／ヒット件数上限。
- ``source_read`` は 1 回の文字数上限＋関数単位スライス。
- **run 全体のソース取得総量にソフト上限**。超過後は取得を拒否し「絞れ」と返す。
- 同一 path/symbol の **重複取得をガード**（再送しない）。
これらは ``runtime`` (1 run で共有する可変 dict) に状態を持たせて横断的に効かせる。
"""
from __future__ import annotations

import os
from typing import Any

from log_analyzer.schema import DbSchema
from log_analyzer.source.db_schema import format_db_schema_detail, summarize_db_schema
from log_analyzer.source.indexer import SourceIndex


# ─── 予算（環境変数で調整可能）──────────────────────────────────────


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _run_budget_chars() -> int:
    # 1 run（1 監視チェーン / 1 Stage）でソース取得に使える総文字数のソフト上限
    return _env_int("RALLY_SOURCE_RUN_BUDGET_CHARS", 40000)


def _read_max_chars() -> int:
    # source_read 1 回で返す最大文字数
    return _env_int("RALLY_SOURCE_READ_MAX_CHARS", 6000)


def _search_max_hits() -> int:
    return _env_int("RALLY_SOURCE_SEARCH_MAX_HITS", 30)


# ─── ツールスキーマ（Anthropic Messages API の tools=）────────────

SOURCE_SEARCH_TOOL_SCHEMA: dict[str, Any] = {
    "name": "source_search",
    "description": (
        "解析対象ソースコードから、障害に関係しそうなファイル・関数を検索する。"
        "返るのは**署名（ファイルパス・関数/クラス名・行範囲）だけ**で本文は含まない。"
        "ログのエラー・例外名・識別子・関数名をクエリにして、まずここで当たりをつけ、"
        "本文が必要なものだけ source_read で取得すること（全件読みは避ける）。"
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "検索語。ログ中のエラー/例外名/識別子/関数名などを空白区切りで。",
            },
            "lang": {
                "type": "string",
                "enum": ["py", "ts", "js", "python", "typescript", "javascript"],
                "description": "言語で絞り込む場合に指定（任意）。",
            },
            "limit": {
                "type": "integer",
                "description": "最大ヒット件数（任意、既定 30、上限超過はクランプ）。",
            },
        },
        "required": ["query"],
    },
}

SOURCE_READ_TOOL_SCHEMA: dict[str, Any] = {
    "name": "source_read",
    "description": (
        "ソースコードの特定ファイル/関数の本文を取得する。symbol を指定すると"
        "その関数/クラス単位のスライスを返す（推奨。指定しないとファイル全体で"
        "トークンを浪費しやすい）。1 回の取得量・run 全体の取得量には上限があり、"
        "上限に達すると取得できなくなるので、必要な箇所に絞って呼ぶこと。"
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "コードベースルートからの相対パス（source_search の結果の path）。",
            },
            "symbol": {
                "type": "string",
                "description": "関数/クラス/メソッド名。指定すると関数単位スライスで返す（推奨）。",
            },
        },
        "required": ["path"],
    },
}

DB_SCHEMA_TOOL_SCHEMA: dict[str, Any] = {
    "name": "db_schema",
    "description": (
        "DB スキーマの詳細（列の型・NOT NULL・主キー・外部キー・index）を取得する。"
        "log_text には要約だけが載っているため、特定テーブルの詳細が必要なときに呼ぶ。"
        "table を省略すると全テーブルを返す。"
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "table": {
                "type": "string",
                "description": "詳細を見たいテーブル名（任意。省略時は全テーブル）。",
            },
        },
        "required": [],
    },
}

# 監視 system prompt 末尾に足すガイダンス（ソースが利用可能なとき）
SOURCE_TOOL_GUIDANCE = """

ソースコード参照について（重要）:
- このインシデントには解析対象のソースコードがあります（全文は与えられていません）。
- まず source_search で、ログのエラー/例外/識別子に関係しそうなファイル・関数を探してください（署名のみ返ります）。
- 本文が必要なものだけ source_read で取得してください。symbol を指定して関数単位で取ると無駄が減ります。
- DB スキーマの詳細が要るときは db_schema(table) を呼んでください（log_text には要約のみ）。
- 取得は最小限に。闇雲に全件読まず、疑わしい箇所に絞ること（取得量には上限があります）。
- 取得が済んだら、最終的に必ず指定スキーマの JSON を（ツールではなく）テキストで出力して終了してください。
"""

# ツール名 → True（監視ループのディスパッチ判定用）
SOURCE_TOOL_NAMES = frozenset({"source_search", "source_read", "db_schema"})


# ─── runtime（1 run で共有する状態）────────────────────────────────


def make_source_runtime(
    index: SourceIndex | None,
    db_schema: DbSchema | None,
    *,
    codebase: str = "",
) -> dict:
    """ソースツールの実行時状態を作る。run_rally_stream が state に持たせ、
    監視ノード・ツール実行関数が共有して横断的に予算/重複を管理する。"""
    return {
        "index": index,
        "db_schema": db_schema,
        "codebase": codebase,
        "remaining": _run_budget_chars(),
        "seen": set(),            # 取得済み (path, symbol) の重複ガード
        "tool_calls": [],         # SourceToolCall 相当の記録
        "current_node": "",       # rally_agent が監視ごとに更新（attribution 用）
        "current_round": 0,
    }


def has_source_tools(runtime: dict | None) -> bool:
    if not runtime:
        return False
    index = runtime.get("index")
    has_files = bool(index and index.files)
    has_schema = bool(runtime.get("db_schema") and runtime["db_schema"].tables)
    return has_files or has_schema


def source_tool_schemas(runtime: dict | None) -> list[dict]:
    """runtime の内容に応じて有効なツールスキーマだけを返す。"""
    if not runtime:
        return []
    tools: list[dict] = []
    index = runtime.get("index")
    if index and index.files:
        tools.append(SOURCE_SEARCH_TOOL_SCHEMA)
        tools.append(SOURCE_READ_TOOL_SCHEMA)
    if runtime.get("db_schema") and runtime["db_schema"].tables:
        tools.append(DB_SCHEMA_TOOL_SCHEMA)
    return tools


def _record(runtime: dict, tool: str, args: dict, result: str) -> None:
    runtime.setdefault("tool_calls", []).append(
        {
            "round": int(runtime.get("current_round", 0) or 0),
            "node": str(runtime.get("current_node", "") or ""),
            "tool": tool,
            "args": args,
            "result_chars": len(result),
        }
    )


# ─── 実行関数（tool_use ディスパッチから呼ばれる）─────────────────


def run_source_search(tool_input: dict, runtime: dict) -> str:
    index: SourceIndex | None = runtime.get("index")
    query = str((tool_input or {}).get("query") or "").strip()
    if index is None or not index.files:
        out = "ソースコードのインデックスがありません（コード本文は参照できません）。"
        _record(runtime, "source_search", {"query": query}, out)
        return out
    if not query:
        out = "エラー: query は必須です。"
        _record(runtime, "source_search", {"query": query}, out)
        return out
    lang = (tool_input or {}).get("lang")
    try:
        limit = int((tool_input or {}).get("limit") or _search_max_hits())
    except (TypeError, ValueError):
        limit = _search_max_hits()
    limit = max(1, min(limit, _search_max_hits()))
    hits = index.search(query, lang=lang, limit=limit)
    if not hits:
        out = f"query={query!r} に一致するファイル/シンボルは見つかりませんでした。"
        _record(runtime, "source_search", {"query": query, "lang": lang}, out)
        return out
    lines = [f"source_search: query={query!r} → {len(hits)} 件（署名のみ）"]
    for h in hits:
        syms = ", ".join(
            f"{m['name']}({m['kind']} L{m['start_line']}-{m['end_line']})"
            for m in h.get("matched_symbols", [])
        )
        lines.append(f"- {h['path']} [{h['language']}]" + (f": {syms}" if syms else ""))
    out = "\n".join(lines)
    _record(runtime, "source_search", {"query": query, "lang": lang}, out)
    return out


def run_source_read(tool_input: dict, runtime: dict) -> str:
    index: SourceIndex | None = runtime.get("index")
    path = str((tool_input or {}).get("path") or "").strip()
    symbol = (tool_input or {}).get("symbol")
    symbol = str(symbol).strip() if symbol else None
    args = {"path": path, "symbol": symbol}

    if index is None:
        out = "ソースコードのインデックスがありません。"
        _record(runtime, "source_read", args, out)
        return out
    if not path:
        out = "エラー: path は必須です。"
        _record(runtime, "source_read", args, out)
        return out

    # 予算チェック（run 全体）
    if runtime.get("remaining", 0) <= 0:
        out = (
            "ソース取得の上限に達しました。これ以上は取得できません。"
            "既に取得した情報で結論してください。"
        )
        _record(runtime, "source_read", args, out)
        return out

    # 重複ガード
    key = (path, symbol or "")
    if key in runtime.get("seen", set()):
        out = f"取得済みです（前述の {path}{(':' + symbol) if symbol else ''} を参照）。"
        _record(runtime, "source_read", args, out)
        return out

    cap = min(_read_max_chars(), int(runtime.get("remaining", _read_max_chars())))
    body = index.read(path, symbol=symbol, max_chars=cap)
    runtime["remaining"] = int(runtime.get("remaining", 0)) - len(body)
    runtime.setdefault("seen", set()).add(key)
    _record(runtime, "source_read", args, body)
    return body


def run_db_schema_tool(tool_input: dict, runtime: dict) -> str:
    schema: DbSchema | None = runtime.get("db_schema")
    table = (tool_input or {}).get("table")
    table = str(table).strip() if table else None
    if schema is None:
        out = "DB スキーマは検出されていません。"
    else:
        out = format_db_schema_detail(schema, table)
    _record(runtime, "db_schema", {"table": table}, out)
    return out


def dispatch_source_tool(name: str, tool_input: dict, runtime: dict) -> str:
    """ツール名に応じて実行関数へ振り分ける。未知名はエラー文字列。"""
    if name == "source_search":
        return run_source_search(tool_input, runtime)
    if name == "source_read":
        return run_source_read(tool_input, runtime)
    if name == "db_schema":
        return run_db_schema_tool(tool_input, runtime)
    return f"エラー: 未知のソースツール {name}"


# ─── log_text 注入ブロック / SourceContext ───────────────────────


def build_source_injection_block(
    codebase: str, index: SourceIndex | None, db_schema: DbSchema | None
) -> str:
    """log_text 先頭に差し込む「ソース利用可能」マーカー＋DBスキーマ要約。

    コード本文は注入しない（オンデマンド取得）。DB スキーマは要約のみ注入し、
    詳細は db_schema(table) ツールで引かせる（input トークン配慮）。
    """
    parts: list[str] = []
    if index is not None and index.files:
        langs = ", ".join(f"{k}:{v}" for k, v in sorted(index.language_breakdown().items()))
        parts.append("## ソースコード（オンデマンド取得）")
        parts.append(
            f"このインシデントには解析対象のソースコードがあります"
            f"（コードベース: {codebase}、{len(index.files)} ファイル / {langs}）。"
        )
        parts.append(
            "全文は与えられていません。source_search で関連ファイル・関数を探し、"
            "source_read で必要な箇所だけ取得してください。"
        )
        parts.append("")
    if db_schema is not None and db_schema.tables:
        parts.append(summarize_db_schema(db_schema).rstrip())
        parts.append("")
    return ("\n".join(parts) + "\n") if parts else ""


def build_source_context(runtime: dict | None):
    """runtime から SourceContext を組み立てる（解析履歴・UI 表示用）。"""
    if not runtime:
        return None
    from log_analyzer.schema import SourceContext, SourceToolCall

    index: SourceIndex | None = runtime.get("index")
    db_schema: DbSchema | None = runtime.get("db_schema")
    calls = [
        SourceToolCall(
            round=c.get("round", 0),
            node=c.get("node", ""),
            tool=c.get("tool", ""),
            args=c.get("args", {}) or {},
            result_chars=c.get("result_chars", 0),
        )
        for c in runtime.get("tool_calls", [])
    ]
    total_chars = sum(c.result_chars for c in calls)
    return SourceContext(
        codebase=str(runtime.get("codebase", "") or ""),
        db_schema=db_schema,
        tool_calls=calls,
        total_chars_fetched=total_chars,
        file_count=len(index.files) if index else 0,
        symbol_count=index.symbol_count() if index else 0,
        language_breakdown=index.language_breakdown() if index else {},
    )


def merge_source_contexts(contexts: list):
    """複数 Stage の SourceContext を 1 つに統合（2 段階モード用）。"""
    contexts = [c for c in contexts if c is not None]
    if not contexts:
        return None
    base = contexts[0]
    all_calls = []
    for c in contexts:
        all_calls.extend(c.tool_calls)
    from log_analyzer.schema import SourceContext

    return SourceContext(
        codebase=base.codebase,
        db_schema=base.db_schema,
        tool_calls=all_calls,
        total_chars_fetched=sum(c.total_chars_fetched for c in contexts),
        file_count=max(c.file_count for c in contexts),
        symbol_count=max(c.symbol_count for c in contexts),
        language_breakdown=base.language_breakdown,
    )
