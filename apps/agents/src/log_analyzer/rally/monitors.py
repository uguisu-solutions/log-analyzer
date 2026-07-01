"""構成4 監視エージェント (委譲チェーン型)。

5 監視 (FW / Routing / App / DNS / Sec) はモデルと全体構造を共有し、
System Prompt と観点だけが異なる。各監視は ``read_topology`` / ``get_config``
を 1 回ずつ叩いてからログを分析し、findings を返すと同時に
**次に処理を委譲するノード** を 1 つ指名する。

監視の出力 JSON:
    {
      "findings": [...],
      "tool_calls_made": [...],
      "confidence": 0.0,
      "next": "fw|routing|app|dns|sec|integrator",
      "focus_hint_for_next": "次ノードに渡す観点指示 (next が integrator なら空)",
      "rationale": "なぜこの next を選んだか"
    }

遷移制約 (rally_agent 側で検証):
    - 自己遷移禁止 (例: fw → fw)
    - 直前ノードへの遷移禁止 (即時 ping-pong 防止)
    - 違反時は強制的に integrator にフォールバック
"""
from __future__ import annotations

import json
import os
import re
import time
from typing import Callable

import anthropic

from log_analyzer.rally import source_tools
from log_analyzer.rally._helpers import extract_json, safe_extract_json
from log_analyzer.rally.state import Config4State
from log_analyzer.tracing import usage_components
from log_analyzer.rally.tools import (
    BIGQUERY_SCHEMA_TOOL_SCHEMA,
    BIGQUERY_TOOL_SCHEMA,
    extract_target_service,
    get_config,
    read_topology,
    run_bigquery_schema_tool,
    run_bigquery_tool,
)

VALID_NEXT_NODES: set[str] = {"fw", "routing", "app", "dns", "sec", "integrator"}

# config4 監視のデフォルトモデル。
# config-log 解析の評価方針 (2026-06) で Claude 系ノードは Opus に統一。
# RALLY_MONITOR_MODEL で個別に上書き可能。
_DEFAULT_MONITOR_MODEL = "claude-opus-4-7"

# BigQuery 取得 (native tool-use) の最大反復回数。これを超えたらツール無しで
# 最終 JSON を出させて打ち切る (暴走・コスト暴発の防止)。
# スキーマ確認 (bigquery_schema) → 取得 (bigquery_query) の 2 段になるため、
# 1 ノードあたり最低 2 往復＋絞り込みの再取得を見込んで余裕を持たせる。
_MAX_TOOL_ITERATIONS = 6

# 監視 LLM の max_tokens。BigQuery 取得後は前置きの分析文＋findings/evidence が
# 日本語で長くなり、小さすぎると最終 JSON が途中で切れて (truncation) parse 失敗 →
# integrator フォールバックになる。実測で前置き+3 findings が ~3k tok に達したため
# 余裕を持って 8000。Opus の非ストリーミング上限 (~16k) 内で安全。
_MONITOR_MAX_TOKENS = 8000

# bq_sources がある時に system prompt 末尾へ足すツール利用ガイダンス。
_BQ_TOOL_GUIDANCE = """

BigQuery 取得について（重要）:
- ログ取得元が BigQuery のノード（上記ログ中に「[ログ取得元: BigQuery]」と記載）は、本文がここに含まれていません。
- テーブルの列構成は機器ごとに異なります。まず bigquery_schema ツールで列名・型・サンプル行を確認し、どの列が時刻か・どの列が本文か・絞り込みに使える列は何かを把握してください。
- そのうえで bigquery_query ツールを呼び、time_column / text_column / columns に実在する列名を指定し、host と期間/キーワードを絞ってログを取得してから分析してください。
- 取得は最小限に。疑わしい時間帯・キーワードに絞ること（全件取得は避ける）。
- 取得が済んだら、最終的に必ず上記スキーマの JSON を（ツールではなく）テキストで出力して終了してください。
- **最終出力は前置き・説明文・コードフェンスを付けず、JSON オブジェクトのみ**を出力すること（分析の地の文を JSON の前後に書かない）。
"""


# ─── System Prompts ──────────────────────────────────────────────────

_HYPOTHESIS_BREADTH_GUIDANCE = """\

原因の広さについて（重要。定型パターンに無理に当てはめない）:
- 障害の真因は、ログに派手な兆候を残さない「地味な原因」であることがある:
  例) ケーブル/コネクタ/PoE 等の物理層の劣化、設定値の単純ミス（タイムアウト値・
  上限値・誤った既定値）、登録漏れ（MAB/許可リスト/証明書）、権限・スコープの
  取りこぼし、移行・リファクタの取りこぼし（旧参照残存）。
- ログに強い痕跡がある派手な仮説に飛びつく前に、「典型症状が欠如していても
  説明できる地味な原因」を最低 1 つ検討し、除外しきれないなら findings に残すこと。
- 自分の監視レイヤの定型パターンに無理に当てはめない。証拠がレイヤ外
  （物理層 / 設定ミス / データ不整合 / アプリのコード不具合）を指すなら、
  その旨を summary に明記し、断定を避ける。
- 与えられた入力（問診票の症状・解析方式・Config・ソース）で真因を直接
  裏付けられない場合は、無理に断定せず「何を確認すれば切り分けられるか」を示す。
"""

_NEXT_NODE_GUIDANCE = _HYPOTHESIS_BREADTH_GUIDANCE + """\

委譲ルール（重要）:
- あなたは自分の観点で分析を終えたら、必ず "next" に次のノードを指名すること
- 候補: "fw" / "routing" / "app" / "dns" / "sec" / "integrator"
- "integrator" を選ぶと最終統合に進む（ラリー終了）
- 自分自身への遷移は禁止（即 ping-pong 防止のため）
- 直前に処理したノードへの遷移も禁止（rally_agent が自動的に integrator にフォールバックする）
- 既存の所見と矛盾しない、別観点で深掘りが必要な監視を選ぶ
- 主要原因が裏付けられた・追加調査の価値が低い → "integrator" を選ぶ
- 「解析中に追加投入されたログ」が user 入力にあれば、それも分析と次ノード判断に踏まえる
- "focus_hint_for_next" は次ノードに渡す観点（next=integrator なら空文字）

出力 (JSON のみ、コードフェンス不要):
{
  "findings": [
    {"category": "FW|Net|App|DNS|Sec|Unknown", "summary": "...", "evidence": ["..."]}
  ],
  "tool_calls_made": ["read_topology(<ip>)", "get_config(<service>)"],
  "confidence": 0.0,
  "next": "routing",
  "focus_hint_for_next": "FW で DENY を検出した宛先 IP の経路 / 再送状況を調べてほしい",
  "rationale": "FW 側で DENY は確認できたので、影響範囲を Routing で裏取りする"
}

ルール:
- findings は最大 3 件、確度の高い順
- summary の自然文は日本語、フィールド名・enum 値は英語
"""

FW_PROMPT = (
    """\
あなたはファイアウォール監視エージェントです。
与えられたログとトポロジ情報から、FW レイヤの異常（policy / DENY / ACL）を検出し、
構造化 JSON で報告してください。
オーケストレータまたは前段の監視から「観点指示」が与えられた場合は、その観点に沿って分析してください。
"""
    + _NEXT_NODE_GUIDANCE
)

ROUTING_PROMPT = (
    """\
あなたはルーティング・接続性の監視エージェントです。
与えられたログとトポロジ情報から、L3-L4 の異常（タイムアウト / 再送 / 経路 / 帯域）を
検出し、構造化 JSON で報告してください。
オーケストレータまたは前段の監視から「観点指示」が与えられた場合は、その観点に沿って分析してください。
"""
    + _NEXT_NODE_GUIDANCE
)

APP_PROMPT = (
    """\
あなたはアプリケーション層の監視エージェントです。
与えられたログとトポロジ情報、サービス設定から L7 の異常（5xx / プロセス / OOM / バックエンド応答）を
検出し、構造化 JSON で報告してください。
オーケストレータまたは前段の監視から「観点指示」が与えられた場合は、その観点に沿って分析してください。
"""
    + _NEXT_NODE_GUIDANCE
)

DNS_PROMPT = (
    """\
あなたは DNS の監視エージェントです。
与えられたログとトポロジ情報、サービス設定から DNS 解決の異常（SERVFAIL / NXDOMAIN /
ゾーン転送失敗 / 上流タイムアウト / 解決遅延）を検出し、構造化 JSON で報告してください。
オーケストレータまたは前段の監視から「観点指示」が与えられた場合は、その観点に沿って分析してください。
"""
    + _NEXT_NODE_GUIDANCE
)

SEC_PROMPT = (
    """\
あなたはセキュリティ監視エージェントです。
与えられたログとトポロジ情報、サービス設定から侵入・特権昇格・C2 通信・既知 IOC 接触などの
セキュリティ異常を検出し、構造化 JSON で報告してください。
オーケストレータまたは前段の監視から「観点指示」が与えられた場合は、その観点に沿って分析してください。

特記事項:
- 推奨アクションが「アカウント無効化／プロセス kill／NW 隔離」等の取り返しがつかない操作の場合、
  上位の integrator で human_judgment_required=true が立つよう「人間判断必須相当」と summary に明示
"""
    + _NEXT_NODE_GUIDANCE
)


def _extract_target_ip(log: str) -> str:
    """ログから「分析対象の宛先 IP」を雑に拾う。"""
    match = re.search(r"dst=(\d+\.\d+\.\d+\.\d+)", log)
    return match.group(1) if match else "unknown"


def _build_user_blocks(
    log: str,
    topology: dict,
    service_config: dict,
    focus_hint: str | None,
    previous_node: str | None,
    monitor_results: dict,
    appended_logs: list[dict] | None = None,
) -> list[dict]:
    """user メッセージを安定部分 + 動的部分の 2 ブロックに分けて返す。

    安定部分（元 log + tool 結果）に ``cache_control`` を立てる。元 log と tool
    結果は実行中変化しないので、同じ監視を委譲チェーン内で複数回呼ぶ場合 /
    同じログに対する別構成での再実行でキャッシュヒットが発生する。

    実行中にユーザーが投入した追加ログは **動的ブロック側** に入れることで
    キャッシュ無効化を避けつつ、以降の監視へ確実に伝える。
    """
    stable = (
        f"## ログ\n{log}\n\n"
        f"## ツール read_topology の結果\n"
        f"{json.dumps(topology, ensure_ascii=False, indent=2)}\n\n"
        f"## ツール get_config の結果\n"
        f"{json.dumps(service_config, ensure_ascii=False, indent=2)}\n"
    )
    dynamic_parts: list[str] = []
    if appended_logs:
        appended_text = "\n\n".join(
            f"### 追加ログ #{i + 1} (round {a.get('round_added', '?')} で投入、source={a.get('source', '?')})\n"
            f"{a.get('content', '')}"
            for i, a in enumerate(appended_logs)
        )
        dynamic_parts.append(
            "## 解析中に追加投入されたログ\n"
            "以下は実行中にユーザーが追加で渡したログです。元ログと合わせて分析し、"
            "次ノード判断にも反映してください。\n\n"
            + appended_text
        )
    if focus_hint:
        dynamic_parts.append(f"## 今ラウンドの観点指示\n{focus_hint}")
    if previous_node:
        dynamic_parts.append(
            f"## 委譲元\n直前に処理したのは {previous_node} です。"
            f"そのノードへの遷移は禁止されています（即 ping-pong 防止）。"
        )
    if monitor_results:
        dynamic_parts.append(
            "## 過去の監視結果（参考）\n"
            + json.dumps(monitor_results, ensure_ascii=False, indent=2)
        )
    dynamic = "\n\n".join(dynamic_parts) if dynamic_parts else "（動的な観点指示なし）"

    return [
        {"type": "text", "text": stable, "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": dynamic},
    ]


def _blocks_to_text(blocks: list[dict]) -> str:
    """token_log への保存用にブロック群を 1 つの文字列に連結。"""
    return "\n\n".join(b.get("text", "") for b in blocks)


def _extract_text(content) -> str:
    """Messages API レスポンスの content から text ブロックを連結して返す。

    実 SDK の text ブロックは ``type == "text"``。``type`` を持たないブロック
    (テストの SimpleNamespace 等) も text 属性があれば拾う。tool_use ブロックは除外。
    """
    parts = []
    for block in content:
        btype = getattr(block, "type", None)
        if btype == "text" or (btype is None and hasattr(block, "text")):
            parts.append(getattr(block, "text", "") or "")
    return "".join(parts)


def _run_monitor_llm(
    *,
    model: str,
    system_prompt: str,
    user_blocks: list[dict],
    bq_sources: dict[str, dict],
    source_runtime: dict | None = None,
) -> tuple[str, int, int, int, int, list[str], list[dict]]:
    """監視 LLM を呼ぶ。bq_sources / source_runtime があれば tool-use ループを回す。

    返り値: (最終テキスト, tokens_in 総量, tokens_out 合計, cache_creation 合計,
    cache_read 合計, 実行した BQ ツール呼び出しの記録, BigQuery から取得した実ログ
    ``[{host, content}]``)。``tokens_in`` は prompt caching の cache 書込/読出を
    含む**入力処理トークン総量**。cache 内訳はコスト計算用に別途返す。

    bq_sources が空の場合は従来通りツール無しの単発呼び出しと等価。
    """
    client = anthropic.Anthropic()
    use_bq = bool(bq_sources)
    use_source = source_tools.has_source_tools(source_runtime)
    use_tools = use_bq or use_source
    system_text = (
        system_prompt
        + (_BQ_TOOL_GUIDANCE if use_bq else "")
        + (source_tools.SOURCE_TOOL_GUIDANCE if use_source else "")
    )
    # system と user の安定ブロックに ephemeral キャッシュを設定（従来踏襲）。
    system = [
        {"type": "text", "text": system_text, "cache_control": {"type": "ephemeral"}}
    ]
    messages: list[dict] = [{"role": "user", "content": user_blocks}]
    total_in = 0
    total_out = 0
    total_cc = 0  # cache_creation 合計
    total_cr = 0  # cache_read 合計
    executed: list[str] = []
    fetched: list[dict] = []  # bigquery_query で取得した実ログ (監査の証拠用)

    def _accumulate(usage) -> None:
        nonlocal total_in, total_out, total_cc, total_cr
        c = usage_components(usage)
        total_in += c["input"] + c["cache_creation"] + c["cache_read"]
        total_out += c["output"]
        total_cc += c["cache_creation"]
        total_cr += c["cache_read"]

    def _retry_if_not_json(text: str) -> str:
        """最終応答から JSON が抽出できなければ「JSON のみ出力」と促して 1 回だけ
        再生成する。大量の BQ コンテキストでモデルが散文を返し、parse 失敗 →
        integrator フォールバックになるのを救済する (ツールは使わせない)。"""
        try:
            extract_json(text)
            return text  # 既に JSON が取れる
        except (ValueError, json.JSONDecodeError):
            pass
        messages.append({"role": "assistant", "content": text or "(空応答)"})
        messages.append({
            "role": "user",
            "content": (
                "前の応答から JSON オブジェクトを抽出できませんでした。"
                "これまでの分析を踏まえ、指定スキーマに厳密に従って、説明文・前置き・"
                "コードフェンスを一切付けず、JSON オブジェクトのみを出力してください。"
            ),
        })
        try:
            retry = client.messages.create(
                model=model, max_tokens=_MONITOR_MAX_TOKENS, system=system, messages=messages
            )
        except Exception:
            return text  # 再試行呼び出し自体が失敗したら元のテキストを返す
        _accumulate(retry.usage)
        retry_text = _extract_text(retry.content)
        return retry_text or text

    for _ in range(_MAX_TOOL_ITERATIONS):
        kwargs: dict = {
            "model": model,
            "max_tokens": _MONITOR_MAX_TOKENS,
            "system": system,
            "messages": messages,
        }
        if use_tools:
            tool_list: list[dict] = []
            if use_bq:
                tool_list += [BIGQUERY_SCHEMA_TOOL_SCHEMA, BIGQUERY_TOOL_SCHEMA]
            if use_source:
                tool_list += source_tools.source_tool_schemas(source_runtime)
            kwargs["tools"] = tool_list
        response = client.messages.create(**kwargs)
        _accumulate(response.usage)

        if use_tools and response.stop_reason == "tool_use":
            # assistant の tool_use ターンをそのまま履歴に積む
            messages.append({"role": "assistant", "content": response.content})
            tool_results: list[dict] = []
            for block in response.content:
                if getattr(block, "type", None) != "tool_use":
                    continue
                if block.name == "bigquery_query":
                    result_str = run_bigquery_tool(dict(block.input or {}), bq_sources)
                    executed.append(
                        f"bigquery_query(host={ (block.input or {}).get('host')!r})"
                    )
                    # 取得した実ログを監査の証拠として保持 (エラー文字列は除く)
                    if not result_str.startswith("エラー"):
                        fetched.append({
                            "host": str((block.input or {}).get("host") or ""),
                            "content": result_str,
                        })
                elif block.name == "bigquery_schema":
                    result_str = run_bigquery_schema_tool(dict(block.input or {}), bq_sources)
                    executed.append(
                        f"bigquery_schema(host={ (block.input or {}).get('host')!r})"
                    )
                elif block.name in source_tools.SOURCE_TOOL_NAMES:
                    args = dict(block.input or {})
                    result_str = source_tools.dispatch_source_tool(
                        block.name, args, source_runtime or {}
                    )
                    label = args.get("path") or args.get("query") or args.get("table") or ""
                    executed.append(f"{block.name}({label!r})")
                else:
                    result_str = f"エラー: 未知のツール {block.name}"
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result_str,
                    }
                )
            messages.append({"role": "user", "content": tool_results})
            continue

        # tool_use 以外 (end_turn 等) → 最終テキスト。JSON が取れなければ 1 回リトライ
        final_text = _retry_if_not_json(_extract_text(response.content))
        return final_text, total_in, total_out, total_cc, total_cr, executed, fetched

    # 反復上限に到達: ツール無しで呼び直し、最終 JSON を強制する
    final = client.messages.create(
        model=model, max_tokens=_MONITOR_MAX_TOKENS, system=system, messages=messages
    )
    _accumulate(final.usage)
    final_text = _retry_if_not_json(_extract_text(final.content))
    return final_text, total_in, total_out, total_cc, total_cr, executed, fetched


def _normalize_monitor_output(
    raw: dict,
    role: str,
    previous_node: str | None,
) -> tuple[dict, str | None]:
    """LLM 出力を正規化し、遷移制約を検証。違反時は integrator にフォールバック。

    返り値: (正規化された出力, 違反理由 or None)
    """
    next_node = raw.get("next")
    violation: str | None = None
    if next_node not in VALID_NEXT_NODES:
        violation = f"unknown next='{next_node}', fallback to integrator"
        next_node = "integrator"
    elif next_node == role:
        violation = f"self-delegation forbidden ({role} → {role}), fallback to integrator"
        next_node = "integrator"
    elif previous_node and next_node == previous_node:
        violation = (
            f"ping-pong forbidden ({role} → {previous_node}, just came from there), "
            "fallback to integrator"
        )
        next_node = "integrator"

    focus_hint_for_next = str(raw.get("focus_hint_for_next", "") or "")
    if next_node == "integrator":
        focus_hint_for_next = ""

    return (
        {
            "findings": raw.get("findings", []) or [],
            "tool_calls_made": raw.get("tool_calls_made", []) or [],
            "confidence": float(raw.get("confidence", 0.0) or 0.0),
            "next": next_node,
            "focus_hint_for_next": focus_hint_for_next,
            "rationale": str(raw.get("rationale", "") or ""),
        },
        violation,
    )


def _make_monitor(
    role: str, default_prompt: str
) -> Callable[[Config4State], dict]:
    slot_id = f"{role}_monitor"

    def _monitor(state: Config4State) -> dict:
        p_overrides = state.get("prompt_overrides", {}) or {}
        m_overrides = state.get("model_overrides", {}) or {}
        model = m_overrides.get(slot_id) or os.environ.get(
            "RALLY_MONITOR_MODEL", _DEFAULT_MONITOR_MODEL
        )
        system_prompt = p_overrides.get(slot_id, default_prompt)
        log = state["log_text"]
        target_ip = _extract_target_ip(log)
        topology = read_topology(target_ip)
        target_service = extract_target_service(log)
        service_config = get_config(target_service)
        focus_hint = state.get("pending_focus_hint") or None
        previous_node = state.get("previous_node")
        # 自分自身は previous には含めない（オーケストレータからの初回呼出のため）
        previous_for_prompt = previous_node if previous_node and previous_node != role else None

        user_blocks = _build_user_blocks(
            log,
            topology,
            service_config,
            focus_hint,
            previous_for_prompt,
            state.get("monitor_results", {}) or {},
            state.get("appended_logs") or [],
        )
        user_input = _blocks_to_text(user_blocks)

        # ログ取得元が BigQuery のノード / 解析対象ソースがあれば native tool-use を回す。
        bq_sources = state.get("bq_sources") or {}
        source_runtime = state.get("source_runtime")
        started = time.perf_counter()
        raw, tokens_in, tokens_out, cache_creation, cache_read, bq_tool_calls, bq_fetched = (
            _run_monitor_llm(
                model=model,
                system_prompt=system_prompt,
                user_blocks=user_blocks,
                bq_sources=bq_sources,
                source_runtime=source_runtime,
            )
        )
        latency_ms = int((time.perf_counter() - started) * 1000)
        parsed_raw, parse_error = safe_extract_json(
            raw,
            fallback={
                "findings": [],
                "tool_calls_made": [],
                "confidence": 0.0,
                "next": "integrator",
                "focus_hint_for_next": "",
                "rationale": "monitor JSON parse 失敗のため integrator にフォールバック",
            },
        )
        normalized, violation = _normalize_monitor_output(
            parsed_raw, role, previous_for_prompt
        )
        if parse_error:
            normalized["_parse_error"] = parse_error
        if violation:
            normalized["_routing_violation"] = violation
        # 実際に実行した BigQuery 取得を tool_calls_made に追記 (トレース用)
        if bq_tool_calls:
            normalized["tool_calls_made"] = list(
                normalized.get("tool_calls_made", []) or []
            ) + bq_tool_calls

        return {
            **normalized,
            "role": role,
            "model": model,
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "cache_creation": cache_creation,
            "cache_read": cache_read,
            "latency_ms": latency_ms,
            "raw_output": raw,
            "user_input": user_input,
            "tool_target_ip": target_ip,
            "tool_target_service": target_service,
            "focus_hint_received": focus_hint,
            "_bq_fetched": bq_fetched,
        }

    _monitor.__name__ = f"{role}_monitor"
    return _monitor


fw_monitor = _make_monitor("fw", FW_PROMPT)
routing_monitor = _make_monitor("routing", ROUTING_PROMPT)
app_monitor = _make_monitor("app", APP_PROMPT)
dns_monitor = _make_monitor("dns", DNS_PROMPT)
sec_monitor = _make_monitor("sec", SEC_PROMPT)


MONITOR_FNS: dict[str, Callable[[Config4State], dict]] = {
    "fw": fw_monitor,
    "routing": routing_monitor,
    "app": app_monitor,
    "dns": dns_monitor,
    "sec": sec_monitor,
}

# prompt_slots からの参照用（slot_id → デフォルト System Prompt）
DEFAULT_MONITOR_PROMPTS: dict[str, str] = {
    "fw_monitor": FW_PROMPT,
    "routing_monitor": ROUTING_PROMPT,
    "app_monitor": APP_PROMPT,
    "dns_monitor": DNS_PROMPT,
    "sec_monitor": SEC_PROMPT,
}
