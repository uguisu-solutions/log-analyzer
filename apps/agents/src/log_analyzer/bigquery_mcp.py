"""Google 公式 BigQuery MCP (MCP Toolbox for Databases) への stdio クライアント。

BigQuery 取得ルートの「実行レイヤ」。エージェントが native tool-use で呼ぶ
``bigquery_query`` / ``bigquery_schema`` の裏側で、SQL の実行先を
google-cloud-bigquery 直叩きから **MCP サーバー経由** に切り替えるための薄い
ブリッジ。設計方針:

- Google 公式の ``MCP Toolbox for Databases`` を ``--prebuilt bigquery --stdio``
  で **ローカルのサブプロセス** として起動し、stdio で通信する。
- MCP クライアントは async / anyio ベースなので、専用のバックグラウンド
  イベントループスレッドで 1 セッションだけ常駐させ、同期呼び出し
  (``execute_sql``) からは ``run_coroutine_threadsafe`` で橋渡しする。
  rally の監視ノードは executor スレッド上の同期コードなのでこの形が必要。
- 公開ツールは ``bigquery-execute-sql`` 1 本に集約する (取得・スキーマ確認・
  サンプル取得をすべて SELECT で表現)。これにより prebuilt の他ツールの
  引数スキーマに依存せず、堅牢に動く。

設定は環境変数から読む (``load_dotenv()`` 後に評価):

- ``BIGQUERY_MCP_COMMAND``: toolbox の実行コマンド (既定 ``toolbox``)
- ``BIGQUERY_MCP_ARGS``: 起動引数 (既定 ``--prebuilt bigquery --stdio``)
- ``BIGQUERY_MCP_EXECUTE_TOOL``: SQL 実行ツール名 (既定 ``execute_sql``)
- ``BIGQUERY_MCP_STARTUP_TIMEOUT`` / ``BIGQUERY_MCP_CALL_TIMEOUT``: 秒

認証 (``GOOGLE_APPLICATION_CREDENTIALS`` / ADC) と対象プロジェクト
(``BIGQUERY_PROJECT``) は toolbox 側が同じ環境変数を読むため、こちらは
``os.environ`` をそのままサブプロセスへ引き継ぐ。
"""
from __future__ import annotations

import atexit
import json
import os
import shlex
import threading
from typing import Any

_DEFAULT_COMMAND = "toolbox"
_DEFAULT_ARGS = "--prebuilt bigquery --stdio"
_DEFAULT_EXECUTE_TOOL = "execute_sql"
_DEFAULT_STARTUP_TIMEOUT = 30.0
_DEFAULT_CALL_TIMEOUT = 60.0


def _env_float(name: str, fallback: float) -> float:
    raw = os.environ.get(name)
    if not raw:
        return fallback
    try:
        return float(raw)
    except ValueError:
        return fallback


def command() -> str:
    return os.environ.get("BIGQUERY_MCP_COMMAND") or _DEFAULT_COMMAND


def args_list() -> list[str]:
    raw = os.environ.get("BIGQUERY_MCP_ARGS")
    if raw is None:
        raw = _DEFAULT_ARGS
    # フラグのみを想定。空白区切りを shlex で分割 (Windows のパスは含めない前提)
    return shlex.split(raw, posix=False) if os.name == "nt" else shlex.split(raw)


def execute_tool_name() -> str:
    return os.environ.get("BIGQUERY_MCP_EXECUTE_TOOL") or _DEFAULT_EXECUTE_TOOL


def startup_timeout() -> float:
    return _env_float("BIGQUERY_MCP_STARTUP_TIMEOUT", _DEFAULT_STARTUP_TIMEOUT)


def call_timeout() -> float:
    return _env_float("BIGQUERY_MCP_CALL_TIMEOUT", _DEFAULT_CALL_TIMEOUT)


class _MCPRuntime:
    """stdio MCP セッションを常駐させるバックグラウンドランタイム (プロセス内シングルトン)。"""

    def __init__(self) -> None:
        self._loop: Any = None
        self._thread: threading.Thread | None = None
        self._session: Any = None
        self._stack: Any = None
        self._ready = threading.Event()
        self._startup_error: BaseException | None = None
        self._start_lock = threading.Lock()
        self._call_lock = threading.Lock()

    # ─── 起動 ────────────────────────────────────────────────────────
    async def _startup(self) -> None:
        # 遅延 import: mcp 未インストールでも build_query 等の純粋関数は使えるように
        from contextlib import AsyncExitStack

        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        self._stack = AsyncExitStack()
        params = StdioServerParameters(
            command=command(),
            args=args_list(),
            env=dict(os.environ),  # 認証 / プロジェクトをそのまま引き継ぐ
        )
        read, write = await self._stack.enter_async_context(stdio_client(params))
        self._session = await self._stack.enter_async_context(ClientSession(read, write))
        await self._session.initialize()

    def _run_loop(self) -> None:
        import asyncio

        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._startup())
        except BaseException as e:  # noqa: BLE001 — 起動失敗は呼び出し側へ伝播
            self._startup_error = e
            self._ready.set()
            return
        self._ready.set()
        self._loop.run_forever()

    def start(self) -> None:
        with self._start_lock:
            if self._thread is None:
                self._thread = threading.Thread(
                    target=self._run_loop, name="bigquery-mcp", daemon=True
                )
                self._thread.start()
                atexit.register(self.shutdown)
        if not self._ready.wait(timeout=startup_timeout()):
            raise RuntimeError(
                f"BigQuery MCP サーバーの起動がタイムアウトしました "
                f"(command={command()!r}, args={args_list()})"
            )
        if self._startup_error is not None:
            raise RuntimeError(
                f"BigQuery MCP サーバーの起動に失敗しました: {self._startup_error}"
            ) from self._startup_error

    # ─── 呼び出し ────────────────────────────────────────────────────
    def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        import asyncio

        self.start()
        # 単一セッションへの呼び出しは直列化 (監視ノードは元々逐次実行)
        with self._call_lock:
            fut = asyncio.run_coroutine_threadsafe(
                self._session.call_tool(name, arguments), self._loop
            )
            result = fut.result(timeout=call_timeout())
        return _parse_result(result)

    # ─── 終了 ────────────────────────────────────────────────────────
    def shutdown(self) -> None:
        loop = self._loop
        if loop is None:
            return

        async def _close() -> None:
            if self._stack is not None:
                try:
                    await self._stack.aclose()
                except Exception:  # noqa: BLE001 — 終了時は握りつぶす
                    pass

        try:
            import asyncio

            fut = asyncio.run_coroutine_threadsafe(_close(), loop)
            fut.result(timeout=5)
        except Exception:  # noqa: BLE001
            pass
        finally:
            try:
                loop.call_soon_threadsafe(loop.stop)
            except Exception:  # noqa: BLE001
                pass


_runtime_singleton: _MCPRuntime | None = None
_runtime_lock = threading.Lock()


def _runtime() -> _MCPRuntime:
    global _runtime_singleton
    if _runtime_singleton is None:
        with _runtime_lock:
            if _runtime_singleton is None:
                _runtime_singleton = _MCPRuntime()
    return _runtime_singleton


def _text_of(result: Any) -> str:
    """CallToolResult の content から text を連結して返す。"""
    parts: list[str] = []
    for block in getattr(result, "content", None) or []:
        text = getattr(block, "text", None)
        if text:
            parts.append(text)
    return "".join(parts)


def _parse_result(result: Any) -> Any:
    """MCP の CallToolResult を JSON として解釈して返す。

    toolbox (prebuilt bigquery) は **1 行 = 1 つの text コンテンツブロック** で、
    各行を JSON オブジェクトとして返す。したがってブロックを連結せず **個別に
    パース** して収集する。

    - ``isError`` なら本文をメッセージに例外送出。
    - ``structuredContent`` があればそれを優先。
    - 各 text ブロックを JSON パースし、dict は 1 行として、list は展開して集める。
    - どのブロックも JSON でなければ連結した生テキストを返す (非クエリ系ツール用)。
    """
    if getattr(result, "isError", False):
        raise RuntimeError(_text_of(result) or "BigQuery MCP ツールがエラーを返しました")

    structured = getattr(result, "structuredContent", None)
    if structured is not None:
        return structured

    items: list[Any] = []
    raw_parts: list[str] = []
    for block in getattr(result, "content", None) or []:
        text = getattr(block, "text", None)
        if not text:
            continue
        raw_parts.append(text)
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, list):
            items.extend(parsed)
        else:
            items.append(parsed)

    if items:
        return items
    return "".join(raw_parts)


def _rows(data: Any) -> list[dict[str, Any]]:
    """execute_sql の結果を行 (dict) のリストへ正規化する。

    toolbox はクエリ結果を JSON 配列で返す。dict でラップされて返るケースに
    備え、代表的なキー (rows / result / data) もたどる。
    """
    if isinstance(data, list):
        return [r for r in data if isinstance(r, dict)]
    if isinstance(data, dict):
        for key in ("rows", "result", "results", "data"):
            inner = data.get(key)
            if isinstance(inner, list):
                return [r for r in inner if isinstance(r, dict)]
    return []


def execute_sql(sql: str, *, dry_run: bool = False) -> Any:
    """``bigquery-execute-sql`` を呼ぶ。

    - ``dry_run=False``: 行 (dict) のリストを返す。
    - ``dry_run=True``: toolbox が返す実行情報 (dict 等) をそのまま返す
      (スキャン量見積りに使う)。
    """
    arguments: dict[str, Any] = {"sql": sql}
    if dry_run:
        arguments["dry_run"] = True
    data = _runtime().call_tool(execute_tool_name(), arguments)
    if dry_run:
        return data
    return _rows(data)


def reset_for_tests() -> None:
    """テスト用: シングルトンを破棄する (実サーバー接続なしのテストで使用)。"""
    global _runtime_singleton
    _runtime_singleton = None
