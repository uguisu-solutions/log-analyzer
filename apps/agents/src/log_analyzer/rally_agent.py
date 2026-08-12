"""構成4 — 委譲チェーン型 (シングルアクティブ・ノード) ラリー。

新フロー (2026-05-14 仕様変更):
    1. orchestrator が初回 1 回だけ実行され、最初に起動する監視を 1 つ選ぶ
    2. 監視は分析を行い、次に処理を委譲するノード (別監視 or integrator) を JSON で指名
    3. 常に 1 つのノードのみがアクティブ。複数同時実行はしない
    4. 自己遷移 (A→A) と直前ノードへの遷移 (即時 ping-pong) は禁止
    5. rally_max_rounds を超えると ``await_confirmation`` イベントを emit し、
       UI 側の確認モーダルでユーザーが継続 / 停止を選ぶまで待機する

各ステップを SSE ストリームとして emit するため、コア実装は
``run_rally_stream`` という async generator。非ストリーミング呼出 (CLI /
``/api/runs``) は ``run_rally`` / ``run_rally_async`` に内包される薄いラッパ。
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Awaitable, Callable

from log_analyzer import evidence_grounding
from log_analyzer.rally import source_tools
from log_analyzer.rally.integrator import integrator_node
from log_analyzer.rally.monitors import MONITOR_FNS
from log_analyzer.rally.orchestrator import orchestrator_select_first
from log_analyzer.schema import (
    AnalysisResult,
    ConfigId,
    DelegationEventDTO,
    GraphEdge,
    GraphNode,
    Metrics,
    MonitorFinding,
    MonitorReport,
    RecommendedAction,
    RootCauseCandidate,
    RoundMetrics,
    SuspectedNodeFinding,
)
from log_analyzer.tracing import flush, get_client, usage_for

# uvicorn 起動時にコンソールへ確実に出るよう uvicorn のロガーに載せる。
_logger = logging.getLogger("uvicorn.error")

# ─── 監視ノードの調査根拠 (確認事項 A-3) の保存上限 ─────────────────
# findings / evidence は解析結果 JSON (analysis_history.result_json) に保存される。
# evidence はモデルが引用した **ログ本文そのもの** なので、無制限に残すと
# 履歴 DB の肥大と機微データの保持量増加につながる。通常の解析はこの範囲に
# 収まる想定で、超えた分は切り詰めて ``truncation_note`` に記録する。
_MAX_FINDINGS_PER_MONITOR = 10
_MAX_EVIDENCE_PER_FINDING = 5
_MAX_EVIDENCE_CHARS = 500
_MAX_TOOL_CALLS = 20

# decision_waiter コールバックの戻り値型:
#   {"action": "continue", "extend_by": int}  rally_max_rounds を +extend_by 延長して再開
#   {"action": "stop"}                        即時 integrator に移行
DecisionWaiter = Callable[[], Awaitable[dict[str, Any]]]


def _drain_appends(state: dict, append_queue) -> list[dict]:
    """append_queue にキューされた追加ログを state に取り込み、追加されたエントリを返す。

    各エントリには現時点の round を ``round_added`` として記録する。
    UI 側がストリーム上でいつ・どのソースから追加されたかを表示できるようにするため。
    """
    if append_queue is None:
        return []
    added: list[dict] = []
    while True:
        try:
            entry = append_queue.get_nowait()
        except asyncio.QueueEmpty:
            break
        record = {
            "round_added": int(state.get("rally_round", 0)),
            "source": str(entry.get("source", "inline")),
            "content": str(entry.get("content", "")),
        }
        state.setdefault("appended_logs", []).append(record)
        added.append(record)
    return added


@dataclass
class StreamEvent:
    """SSE で UI に流す 1 イベント。"""

    kind: str
    data: dict[str, Any]


# ─── 内部ヘルパ ──────────────────────────────────────────────────────


async def _run_sync(fn: Callable[..., Any], *args, **kwargs) -> Any:
    """同期関数をデフォルト executor で実行（イベントループを塞がない）。"""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: fn(*args, **kwargs))


def _build_execution_graph(
    token_log: list[dict], delegation_history: list[dict]
) -> tuple[list[GraphNode], list[GraphEdge]]:
    """token_log と delegation_history から React Flow 用のグラフを組み立てる。

    - ノード ID: orchestrator / <role>_monitor / integrator
    - エッジ: delegation_history のチェーンをそのまま辿る（順序保持）
    """
    by_role: dict[str, dict] = {}
    for entry in token_log:
        role = entry["role"]
        agg = by_role.setdefault(
            role,
            {
                "model": entry["model"],
                "tokens_in": 0,
                "tokens_out": 0,
                "latency_ms": 0,
                "invocations": 0,
            },
        )
        agg["tokens_in"] += entry["tokens_in"]
        agg["tokens_out"] += entry["tokens_out"]
        agg["latency_ms"] += entry["latency_ms"]
        agg["invocations"] += 1

    def _node_id(name: str) -> str:
        if name == "orchestrator" or name == "integrator":
            return name
        return f"{name}_monitor"

    nodes: list[GraphNode] = []
    for role, agg in by_role.items():
        if role == "orchestrator":
            role_kind = "orchestrator"
        elif role == "integrator":
            role_kind = "integrator"
        else:
            role_kind = "monitor"
        nodes.append(
            GraphNode(
                id=_node_id(role),
                label=agg["model"],
                role=role_kind,
                model=agg["model"],
                latency_ms=agg["latency_ms"],
                tokens_in=agg["tokens_in"],
                tokens_out=agg["tokens_out"],
                metadata={"invocations": agg["invocations"]},
            )
        )

    edges: list[GraphEdge] = []
    for d in delegation_history:
        src, tgt = d.get("from_node"), d.get("to_node")
        if not src or not tgt:
            continue
        edges.append(
            GraphEdge(
                source=_node_id(src),
                target=_node_id(tgt),
                label=f"r{d.get('round', '?')}",
            )
        )
    return nodes, edges


def _build_round_metrics(token_log: list[dict]) -> list[RoundMetrics]:
    """token_log を round 順に並べた per-round metrics を返す (Phase D)。

    orchestrator: round=0、各監視: round>=1、integrator: 最終 round+1。
    """
    out: list[RoundMetrics] = []
    # orchestrator (round=0)
    for entry in token_log:
        if entry.get("role") == "orchestrator":
            out.append(RoundMetrics(
                round=0,
                role="orchestrator",
                model=str(entry.get("model") or ""),
                tokens_in=int(entry.get("tokens_in") or 0),
                tokens_out=int(entry.get("tokens_out") or 0),
                latency_ms=int(entry.get("latency_ms") or 0),
            ))
    # 監視 (round >= 1)
    monitors = [e for e in token_log if e.get("role") not in {"orchestrator", "integrator"}]
    monitors_sorted = sorted(monitors, key=lambda e: int(e.get("round") or 0))
    for entry in monitors_sorted:
        out.append(RoundMetrics(
            round=int(entry.get("round") or 0),
            role=str(entry.get("role") or ""),
            model=str(entry.get("model") or ""),
            tokens_in=int(entry.get("tokens_in") or 0),
            tokens_out=int(entry.get("tokens_out") or 0),
            latency_ms=int(entry.get("latency_ms") or 0),
        ))
    # integrator
    max_round = max((r.round for r in out), default=0)
    for entry in token_log:
        if entry.get("role") == "integrator":
            out.append(RoundMetrics(
                round=max_round + 1,
                role="integrator",
                model=str(entry.get("model") or ""),
                tokens_in=int(entry.get("tokens_in") or 0),
                tokens_out=int(entry.get("tokens_out") or 0),
                latency_ms=int(entry.get("latency_ms") or 0),
            ))
    return out


def _truncate(text: str, limit: int) -> tuple[str, bool]:
    """``limit`` 文字で切り詰める。切り詰めたら末尾に … を付け、フラグを返す。"""
    if len(text) <= limit:
        return text, False
    return text[:limit] + "…", True


def _build_monitor_report(result: dict, *, round_no: int) -> MonitorReport:
    """監視ノードの実行結果 (dict) を保存用 MonitorReport に変換する (確認事項 A-3)。

    LLM 出力そのままの findings は形が揺れる (evidence が文字列 1 件だけ、
    エントリが dict でない等) ため正規化し、上限で切り詰める。何を落としたかは
    ``truncation_note`` に残して UI に「N 件省略」と出せるようにする。
    """
    raw_findings = result.get("findings") or []
    if not isinstance(raw_findings, list):
        raw_findings = []
    notes: list[str] = []

    if len(raw_findings) > _MAX_FINDINGS_PER_MONITOR:
        notes.append(f"findings {len(raw_findings)} 件中 {_MAX_FINDINGS_PER_MONITOR} 件のみ保存")
        raw_findings = raw_findings[:_MAX_FINDINGS_PER_MONITOR]

    findings: list[MonitorFinding] = []
    dropped_evidence = 0
    truncated_evidence = 0
    for entry in raw_findings:
        if not isinstance(entry, dict):
            # 稀に findings が文字列配列で返ることがある → summary 扱いで拾う
            findings.append(MonitorFinding(summary=str(entry)))
            continue
        raw_ev = entry.get("evidence")
        if isinstance(raw_ev, str):
            raw_ev = [raw_ev]
        elif not isinstance(raw_ev, list):
            raw_ev = []
        if len(raw_ev) > _MAX_EVIDENCE_PER_FINDING:
            dropped_evidence += len(raw_ev) - _MAX_EVIDENCE_PER_FINDING
            raw_ev = raw_ev[:_MAX_EVIDENCE_PER_FINDING]
        evidence: list[str] = []
        for ev in raw_ev:
            text, cut = _truncate(str(ev), _MAX_EVIDENCE_CHARS)
            truncated_evidence += 1 if cut else 0
            evidence.append(text)
        findings.append(
            MonitorFinding(
                category=str(entry.get("category") or ""),
                summary=str(entry.get("summary") or ""),
                evidence=evidence,
            )
        )
    if dropped_evidence:
        notes.append(f"evidence {dropped_evidence} 件を省略")
    if truncated_evidence:
        notes.append(f"evidence {truncated_evidence} 件を {_MAX_EVIDENCE_CHARS} 字で切り詰め")

    raw_calls = result.get("tool_calls_made") or []
    if not isinstance(raw_calls, list):
        raw_calls = []
    tool_calls = [str(c) for c in raw_calls]
    if len(tool_calls) > _MAX_TOOL_CALLS:
        notes.append(f"tool_calls {len(tool_calls)} 件中 {_MAX_TOOL_CALLS} 件のみ保存")
        tool_calls = tool_calls[:_MAX_TOOL_CALLS]

    return MonitorReport(
        round=round_no,
        role=str(result.get("role") or ""),
        model=str(result.get("model") or ""),
        confidence=float(result.get("confidence") or 0.0),
        findings=findings,
        tool_calls=tool_calls,
        rationale=str(result.get("rationale") or ""),
        focus_hint_received=str(result.get("focus_hint_received") or ""),
        focus_hint_for_next=str(result.get("focus_hint_for_next") or ""),
        truncation_note=" / ".join(notes),
        parse_error=result.get("_parse_error"),
    )


def _build_analysis_result(
    *,
    log_ref: str,
    trace_id: str,
    integrator_result: dict,
    token_log: list[dict],
    delegation_history: list[dict],
    rally_round: int,
    rally_max_rounds: int,
    wall_ms: int,
    topology_node_ids: list[str] | None = None,
    log_text: str = "",
    bq_evidence: list[dict] | None = None,
    monitor_reports: list[MonitorReport] | None = None,
) -> AnalysisResult:
    """final イベント用の AnalysisResult を組み立てる。"""
    total_in = sum(e["tokens_in"] for e in token_log)
    total_out = sum(e["tokens_out"] for e in token_log)
    per_call_latencies = sorted(e["latency_ms"] for e in token_log)
    p50 = per_call_latencies[len(per_call_latencies) // 2] if per_call_latencies else 0

    info_loss: list[str] = []
    info_loss.append(f"delegation_rounds_completed: {rally_round} (max={rally_max_rounds})")
    visited = [d.get("to_node") for d in delegation_history if d.get("to_node")]
    info_loss.append("delegation_chain: " + " → ".join(["orchestrator", *visited]))

    violations = [d for d in delegation_history if d.get("kind") == "routing_violation_fallback"]
    if violations:
        info_loss.append(
            f"routing_violations: {len(violations)} (自動的に integrator にフォールバック)"
        )
    forced = [
        d for d in delegation_history if d.get("kind") in {"max_rounds_finalize", "user_finalize"}
    ]
    if forced:
        info_loss.append(f"final_action: {forced[-1]['kind']}")
    if integrator_result.get("_parse_error"):
        info_loss.append(f"integrator_parse_error: {integrator_result['_parse_error']}")
    for d in delegation_history:
        if d.get("parse_error"):
            info_loss.append(
                f"parse_error round={d.get('round')} kind={d.get('kind')}: {d['parse_error']}"
            )

    graph_nodes, graph_edges = _build_execution_graph(token_log, delegation_history)
    history_dtos = [DelegationEventDTO(**_pick_event_dto_fields(d)) for d in delegation_history]

    # トポロジー解析モードの場合、integrator が出した障害候補ノードのうち
    # 「提供された node_ids 」に含まれるものだけを採用する（LLM が幻の ID を出した場合の防御）。
    # 新フォーマット (優先): suspected_nodes = [{"node_id", "summary", "severity"}, ...]
    # LLM のブレに備え:
    #   - キー: node_id / id / nodeId / nodeID のいずれも受ける
    #   - severity: 大文字小文字・前後空白を正規化
    # 旧フォーマット (フォールバック): suspected_node_ids = ["id1", "id2", ...]
    suspected_node_ids: list[str] = []
    suspected_node_findings: list[SuspectedNodeFinding] = []
    if topology_node_ids:
        allowed = set(topology_node_ids)
        seen: set[str] = set()
        allowed_severity = {"primary", "secondary", "info"}

        def _pick_id(entry: dict) -> str:
            for key in ("node_id", "id", "nodeId", "nodeID"):
                v = entry.get(key)
                if v:
                    return str(v).strip()
            return ""

        def _norm_severity(v) -> str:
            s = str(v or "").strip().lower()
            return s if s in allowed_severity else ""

        # 優先: 構造化 suspected_nodes
        raw_findings = integrator_result.get("suspected_nodes") or []
        if isinstance(raw_findings, list):
            for entry in raw_findings:
                if not isinstance(entry, dict):
                    continue
                nid = _pick_id(entry)
                if not nid or nid not in allowed or nid in seen:
                    continue
                suspected_node_ids.append(nid)
                suspected_node_findings.append(
                    SuspectedNodeFinding(
                        node_id=nid,
                        summary=str(entry.get("summary") or entry.get("description") or "").strip(),
                        severity=_norm_severity(entry.get("severity")),
                    )
                )
                seen.add(nid)
        # 旧フィールド suspected_node_ids も並列に許容: LLM が両方出した場合
        # にも欠落分を拾う（structured 側にあれば seen で重複排除される）
        raw_ids = integrator_result.get("suspected_node_ids") or []
        if isinstance(raw_ids, list):
            for nid in raw_ids:
                s = str(nid).strip()
                if s in allowed and s not in seen:
                    suspected_node_ids.append(s)
                    suspected_node_findings.append(
                        SuspectedNodeFinding(node_id=s, summary="", severity="")
                    )
                    seen.add(s)

    # 証拠グラウンディング検証 (グループ2-b・決定的/LLM不使用): 候補に引用された
    # 具体的識別子 (id=N / IP / MAC 等) が、AI が実際に見た入力 (圧縮後 log_text ＋
    # 取得ログ) に実在するか照合。実在しなければでっち上げの疑いとして警告し、
    # 確信度に上限をかける安全ネット。
    confidence = float(integrator_result.get("confidence", 0.0))
    corpus = log_text
    if bq_evidence:
        corpus += "\n" + "\n".join(str(e.get("content") or "") for e in bq_evidence)
    confidence, grounding = evidence_grounding.apply_grounding(
        integrator_result.get("root_cause_candidates", []) or [], confidence, corpus
    )
    # 常に結果を出す（全接地でも「2-b が動いた」ことを確認できるように）。
    if grounding.has_ungrounded:
        shown = ", ".join(grounding.ungrounded[:10])
        more = " ほか" if len(grounding.ungrounded) > 10 else ""
        msg = (
            f"ungrounded_evidence: 提供ログに無い具体値を検出 ({grounding.grounded}/"
            f"{grounding.total_atoms} 接地) — {shown}{more}（確信度に上限を適用）"
        )
        info_loss.append(msg)
        _logger.info("[evidence_grounding] %s", msg)
    elif grounding.total_atoms > 0:
        msg = f"evidence_grounding: 引用された具体値 {grounding.total_atoms} 件すべて接地（でっち上げなし）"
        info_loss.append(msg)
        _logger.info("[evidence_grounding] %s", msg)
    else:
        _logger.info("[evidence_grounding] 照合対象の具体値なし")

    return AnalysisResult(
        trace_id=trace_id,
        config_id=ConfigId.CONFIG4,
        input_log_ref=log_ref,
        root_cause_candidates=[
            RootCauseCandidate(**c) for c in integrator_result.get("root_cause_candidates", [])
        ],
        recommended_actions=[
            RecommendedAction(**a) for a in integrator_result.get("recommended_actions", [])
        ],
        confidence=confidence,
        metrics=Metrics(
            tokens_in=total_in,
            tokens_out=total_out,
            latency_ms_total=wall_ms,
            latency_ms_p50=p50,
        ),
        info_loss_flags=info_loss,
        execution_graph_nodes=graph_nodes,
        execution_graph_edges=graph_edges,
        delegation_rounds=rally_round,
        delegation_max_rounds=rally_max_rounds,
        delegation_history=history_dtos,
        suspected_node_ids=suspected_node_ids,
        suspected_node_findings=suspected_node_findings,
        round_metrics=_build_round_metrics(token_log),
        monitor_reports=list(monitor_reports or []),
    )


_EVENT_DTO_KEYS = {"round", "kind", "from_node", "to_node", "focus_hint", "rationale", "confidence"}


def _pick_event_dto_fields(d: dict) -> dict:
    return {k: v for k, v in d.items() if k in _EVENT_DTO_KEYS}


# ─── コア: ストリーミング実装 ────────────────────────────────────────


async def run_rally_stream(
    log_text: str,
    log_ref: str = "inline",
    *,
    prompt_overrides: dict[str, str] | None = None,
    model_overrides: dict[str, str] | None = None,
    rally_max_rounds: int = 3,
    decision_waiter: DecisionWaiter | None = None,
    append_queue: "asyncio.Queue[dict] | None" = None,
    topology_context: dict | None = None,
    audit_after_integrator: bool = False,
    audit_system_prompt: str | None = None,
    bq_sources: dict[str, dict] | None = None,
    source_index: Any = None,
    source_db_schema: Any = None,
    source_codebase: str = "",
) -> AsyncIterator[StreamEvent]:
    """委譲チェーンを 1 ステップずつ実行しながら ``StreamEvent`` を yield する。

    Args:
        rally_max_rounds: 委譲チェーンを許す最大ステップ数。到達したら
            ``decision_waiter`` を呼んで継続可否をユーザーに問う。
            ``decision_waiter`` が None なら強制 finalize。
            ``continue`` 選択で ``rally_max_rounds`` が ``extend_by`` だけ延長される。
        append_queue: ユーザーが実行中に追加投入したログを受け取るキュー。
            各監視 / integrator の開始前にドレインし、以降の監視・integrator の
            動的入力ブロックに含める。元の ``log_text`` は変更しない（caching 維持）。
    """
    state: dict[str, Any] = {
        "log_text": log_text,
        "log_ref": log_ref,
        "prompt_overrides": prompt_overrides or {},
        "model_overrides": model_overrides or {},
        "monitor_results": {},
        # 各監視の調査根拠 (findings / evidence / tool_calls) を結果に残すための蓄積
        # (確認事項 A-3)。monitor_results は「次の監視へ渡す参考材料」で用途が異なる。
        "monitor_reports": [],
        "delegation_history": [],
        "token_log": [],
        "rally_round": 0,
        "rally_max_rounds": rally_max_rounds,
        "current_node": "orchestrator",
        "previous_node": None,
        "pending_focus_hint": "",
        "appended_logs": [],
        # トポロジー解析タブから渡される。{nodes: [...], links: [...]} 形式。
        # integrator は suspected_node_ids 生成のためにこの ID 一覧を参照する。
        "topology_context": topology_context,
        # ログ取得元が BigQuery のノードのメタデータ {host: {table,start,end,limit}}。
        # 監視ノードの bigquery_query tool-use で host 許可リスト兼デフォルト補完に使う。
        "bq_sources": bq_sources or {},
        # 監視が BigQuery から実際に取得した行 [{host, content}]。監査の証拠として渡す
        # (rally 本体には再投入しない = コスト増を避ける)。
        "bq_evidence": [],
        # 解析対象ソースのオンデマンド参照ツールの実行時状態 (1 run 共有・予算/重複管理)。
        # index も db_schema も無ければ None (= source tool は無効・後方互換)。
        "source_runtime": (
            source_tools.make_source_runtime(
                source_index, source_db_schema, codebase=source_codebase
            )
            if (source_index is not None or source_db_schema is not None)
            else None
        ),
    }

    langfuse = get_client()
    trace = langfuse.trace(
        name="config4-rally",
        input={"log_ref": log_ref, "log_size_bytes": len(log_text)},
        metadata={"config_id": ConfigId.CONFIG4.value, "schema_version": "v0.1"},
    )
    trace_id = str(trace.id)
    wall_start = time.perf_counter()

    yield StreamEvent("run_started", {"trace_id": trace_id, "rally_max_rounds": rally_max_rounds})

    # ─── 1. Orchestrator (初回 1 回のみ) ─────────────────────────
    yield StreamEvent("orchestrator_start", {})
    # 確認事項 B-3: Langfuse の Generation に渡す開始/終了の絶対時刻。
    # ノード側は perf_counter による経過時間しか持たないため、呼び出し側で取る。
    orch_started_at = datetime.now(timezone.utc)
    try:
        orch = await _run_sync(orchestrator_select_first, state)
    except Exception as e:
        yield StreamEvent("error", {"stage": "orchestrator", "message": str(e)})
        return

    state["token_log"].append(
        {
            "role": "orchestrator",
            "model": orch["model"],
            "tokens_in": orch["tokens_in"],
            "tokens_out": orch["tokens_out"],
            "cache_creation": orch.get("cache_creation", 0),
            "cache_read": orch.get("cache_read", 0),
            "latency_ms": orch["latency_ms"],
            "input": orch["user_input"][:2000],
            "raw_output": orch["raw_output"],
            "started_at": orch_started_at,
            "ended_at": datetime.now(timezone.utc),
            "parse_error": orch.get("parse_error"),
        }
    )
    first_event = {
        "round": 0,
        "kind": "orchestrator_initial",
        "from_node": "orchestrator",
        "to_node": orch["first_node"],
        "focus_hint": orch["focus_hint"],
        "rationale": orch["rationale"],
        "confidence": None,
    }
    if orch.get("parse_error"):
        first_event["parse_error"] = orch["parse_error"]
    state["delegation_history"].append(first_event)
    yield StreamEvent("orchestrator_decision", first_event)

    state["current_node"] = orch["first_node"]
    state["pending_focus_hint"] = orch["focus_hint"]
    state["previous_node"] = "orchestrator"

    # ─── 2. 委譲チェーンループ ───────────────────────────────────
    while True:
        # 上限超え: 確認モーダル or 強制 finalize
        if state["rally_round"] >= state["rally_max_rounds"]:
            if decision_waiter is None:
                forced = {
                    "round": state["rally_round"] + 1,
                    "kind": "max_rounds_finalize",
                    "from_node": state["current_node"],
                    "to_node": "integrator",
                    "focus_hint": "",
                    "rationale": (
                        f"rally_max_rounds={state['rally_max_rounds']} 到達のため強制 finalize"
                    ),
                    "confidence": None,
                }
                state["delegation_history"].append(forced)
                yield StreamEvent("max_rounds_finalize", forced)
                state["current_node"] = "integrator"
                break

            # 確認モーダル
            await_payload = {
                "round": state["rally_round"],
                "rally_max_rounds": state["rally_max_rounds"],
                "delegation_history": list(state["delegation_history"]),
                "monitor_results": dict(state["monitor_results"]),
                "next_node_if_continued": state["current_node"],
            }
            yield StreamEvent("await_confirmation", await_payload)
            try:
                decision = await decision_waiter()
            except Exception as e:
                yield StreamEvent("error", {"stage": "await_confirmation", "message": str(e)})
                return
            yield StreamEvent("user_decision", decision)

            if decision.get("action") == "stop":
                stop_event = {
                    "round": state["rally_round"] + 1,
                    "kind": "user_finalize",
                    "from_node": state["current_node"],
                    "to_node": "integrator",
                    "focus_hint": "",
                    "rationale": "ユーザーが確認モーダルで停止を選択",
                    "confidence": None,
                }
                state["delegation_history"].append(stop_event)
                state["current_node"] = "integrator"
                break
            else:
                extend_by = int(decision.get("extend_by", 3) or 3)
                state["rally_max_rounds"] += max(1, extend_by)
                state["delegation_history"].append(
                    {
                        "round": state["rally_round"],
                        "kind": "user_extend",
                        "from_node": None,
                        "to_node": None,
                        "focus_hint": "",
                        "rationale": (
                            f"ユーザーが +{extend_by} ラウンド延長を選択 "
                            f"(new max={state['rally_max_rounds']})"
                        ),
                        "confidence": None,
                    }
                )
                # ループ継続（current_node はそのまま）

        current = state["current_node"]
        if current == "integrator":
            break

        # ─── 監視ノード実行 ──────────────────────────────────
        # ユーザーが投入した追加コンテンツ (ログ / 設定 / コメント) をここで取り込む。
        # 元の log_text は変更しないので prompt caching の安定ブロックは維持される。
        drained = _drain_appends(state, append_queue)
        for record in drained:
            yield StreamEvent("log_appended", record)

        # 介入再起動: ユーザーから追加コンテンツが届いていた場合、現在予定していた
        # 監視を走らせず、orchestrator を再呼び出しして初期ノードを再選択する。
        # 議事録 2026-05-26「処理中にプロンプトで介入があった場合は、一度
        # オーケストレーションノードに戻り、初期ノード選択から再開」に対応。
        if drained:
            yield StreamEvent(
                "intervention_restart",
                {
                    "reason": "ユーザーから追加コンテンツが届きました。orchestrator に戻り初期ノードを再選択します。",
                    "added_count": len(drained),
                    "previous_planned_node": current,
                },
            )
            try:
                orch = await _run_sync(orchestrator_select_first, state)
            except Exception as e:
                yield StreamEvent("error", {"stage": "orchestrator_restart", "message": str(e)})
                return
            state["token_log"].append(
                {
                    "role": "orchestrator",
                    "model": orch["model"],
                    "tokens_in": orch["tokens_in"],
                    "tokens_out": orch["tokens_out"],
                    "cache_creation": orch.get("cache_creation", 0),
                    "cache_read": orch.get("cache_read", 0),
                    "latency_ms": orch["latency_ms"],
                    "input": orch["user_input"][:2000],
                    "raw_output": orch["raw_output"],
                }
            )
            restart_event = {
                "round": state["rally_round"],
                "kind": "orchestrator_restart",
                "from_node": "orchestrator",
                "to_node": orch["first_node"],
                "focus_hint": orch["focus_hint"],
                "rationale": (orch["rationale"] or "") + " (ユーザー介入により再選択)",
                "confidence": None,
            }
            if orch.get("parse_error"):
                restart_event["parse_error"] = orch["parse_error"]
            state["delegation_history"].append(restart_event)
            yield StreamEvent("orchestrator_decision", restart_event)
            state["current_node"] = orch["first_node"]
            state["pending_focus_hint"] = orch["focus_hint"]
            state["previous_node"] = "orchestrator"
            continue  # 次のループ反復で、新しい current_node に対して監視を走らせる

        next_round = state["rally_round"] + 1
        state["rally_round"] = next_round

        # ソースツールの記録に「どの監視が・どのラウンドで」を付けるための attribution
        if state.get("source_runtime"):
            state["source_runtime"]["current_node"] = current
            state["source_runtime"]["current_round"] = next_round

        yield StreamEvent(
            "monitor_start",
            {
                "round": next_round,
                "node": current,
                "focus_hint": state["pending_focus_hint"],
                "previous_node": state["previous_node"],
            },
        )

        monitor_fn = MONITOR_FNS.get(current)
        if monitor_fn is None:
            err_event = {
                "round": next_round,
                "kind": "routing_violation_fallback",
                "from_node": state["previous_node"],
                "to_node": "integrator",
                "focus_hint": "",
                "rationale": f"unknown monitor '{current}', fallback to integrator",
                "confidence": None,
            }
            state["delegation_history"].append(err_event)
            yield StreamEvent("monitor_decision", err_event)
            state["current_node"] = "integrator"
            break

        monitor_started_at = datetime.now(timezone.utc)
        try:
            result = await _run_sync(monitor_fn, state)
        except Exception as e:
            yield StreamEvent(
                "error", {"stage": f"monitor:{current}", "message": str(e), "round": next_round}
            )
            return

        # token_log / monitor_results / delegation_history を更新
        state["token_log"].append(
            {
                "role": current,
                "model": result["model"],
                "tokens_in": result["tokens_in"],
                "tokens_out": result["tokens_out"],
                "cache_creation": result.get("cache_creation", 0),
                "cache_read": result.get("cache_read", 0),
                "latency_ms": result["latency_ms"],
                "input": result["user_input"][:2000],
                "raw_output": result["raw_output"],
                "round": next_round,
                "started_at": monitor_started_at,
                "ended_at": datetime.now(timezone.utc),
                "parse_error": result.get("_parse_error"),
            }
        )
        # findings + confidence のみ monitor_results に残す（次監視への参考材料）
        state["monitor_results"][current] = {
            "findings": result["findings"],
            "confidence": result["confidence"],
            "tool_calls_made": result["tool_calls_made"],
        }
        if result.get("_parse_error"):
            state["monitor_results"][current]["_parse_error"] = result["_parse_error"]
        # 調査根拠を結果に残す (確認事項 A-3)。上限で切り詰めて保存する。
        state["monitor_reports"].append(_build_monitor_report(result, round_no=next_round))
        # BigQuery 取得実ログを監査の証拠として蓄積 (rally 本体へは再投入しない)
        if result.get("_bq_fetched"):
            state.setdefault("bq_evidence", []).extend(result["_bq_fetched"])

        next_node = result["next"]
        violation = result.get("_routing_violation")
        if violation:
            kind = "routing_violation_fallback"
        elif next_node == "integrator":
            kind = "monitor_finalize"
        else:
            kind = "monitor_delegation"

        decision_event = {
            "round": next_round,
            "kind": kind,
            "from_node": current,
            "to_node": next_node,
            "focus_hint": result["focus_hint_for_next"],
            "rationale": result["rationale"],
            "confidence": result["confidence"],
        }
        if violation:
            decision_event["violation"] = violation
        if result.get("_parse_error"):
            decision_event["parse_error"] = result["_parse_error"]
        state["delegation_history"].append(decision_event)
        # UI 用には findings も出して見せたいので別キーで添える
        yield StreamEvent(
            "monitor_decision",
            {
                **decision_event,
                "findings": result["findings"],
                "tool_target_ip": result.get("tool_target_ip"),
                "tool_target_service": result.get("tool_target_service"),
                "tokens_in": result["tokens_in"],
                "tokens_out": result["tokens_out"],
                "latency_ms": result["latency_ms"],
                "model": result["model"],
            },
        )

        state["previous_node"] = current
        state["current_node"] = next_node
        state["pending_focus_hint"] = result["focus_hint_for_next"]

        if next_node == "integrator":
            break

    # ─── 3. Integrator ───────────────────────────────────────────
    # 統合直前にもう一度追加ログをドレインする（チェーン中の最終ノード後に
    # 投入されたログを integrator が見られるようにするため）
    for record in _drain_appends(state, append_queue):
        yield StreamEvent("log_appended", record)
    yield StreamEvent("integrator_start", {})
    integrator_started_at = datetime.now(timezone.utc)
    try:
        integ = await _run_sync(integrator_node, state)
    except Exception as e:
        yield StreamEvent("error", {"stage": "integrator", "message": str(e)})
        return

    integrator_result = integ["result"]
    state["token_log"].append({
        **integ["token_log_entry"],
        "started_at": integrator_started_at,
        "ended_at": datetime.now(timezone.utc),
        "parse_error": integrator_result.get("_parse_error"),
    })
    yield StreamEvent(
        "integrator_done",
        {
            "confidence": integrator_result.get("confidence", 0.0),
            "candidates": len(integrator_result.get("root_cause_candidates", [])),
            "actions": len(integrator_result.get("recommended_actions", [])),
        },
    )

    # ─── 4. Langfuse へ全 generation を反映 ────────────────────
    # 確認事項 B-3: start_time / end_time を渡さないと Langfuse の Latency が
    # 0.00s のままになる (計装漏れ)。JSON パースに失敗したノードは WARNING で
    # 印を付け、トレース上で「なぜフォールバックしたか」を追えるようにする。
    for entry in state["token_log"]:
        gen_kwargs: dict[str, Any] = {
            "name": f"{entry['model']}-{entry['role']}"
            + (f"-r{entry['round']}" if "round" in entry else ""),
            "model": entry["model"],
            "input": entry.get("input", "")[:2000],
            "output": entry.get("raw_output", ""),
            "usage": usage_for(
                entry["model"], entry["tokens_in"], entry["tokens_out"],
                cache_creation=entry.get("cache_creation", 0),
                cache_read=entry.get("cache_read", 0),
            ),
        }
        if entry.get("started_at"):
            gen_kwargs["start_time"] = entry["started_at"]
            gen_kwargs["end_time"] = entry.get("ended_at")
        if entry.get("parse_error"):
            gen_kwargs["level"] = "WARNING"
            gen_kwargs["status_message"] = f"parse_error: {str(entry['parse_error'])[:200]}"
        trace.generation(**gen_kwargs)

    wall_ms = int((time.perf_counter() - wall_start) * 1000)
    topology_node_ids: list[str] = []
    if topology_context:
        topology_node_ids = [
            str(n.get("id"))
            for n in topology_context.get("nodes", []) or []
            if n.get("id") is not None
        ]
    result = _build_analysis_result(
        log_ref=log_ref,
        trace_id=trace_id,
        integrator_result=integrator_result,
        token_log=state["token_log"],
        delegation_history=state["delegation_history"],
        rally_round=state["rally_round"],
        rally_max_rounds=state["rally_max_rounds"],
        wall_ms=wall_ms,
        topology_node_ids=topology_node_ids or None,
        log_text=state["log_text"],
        bq_evidence=state.get("bq_evidence"),
        monitor_reports=state.get("monitor_reports"),
    )

    # ソースツールが使われていれば、参照記録を SourceContext として結果に載せる
    if state.get("source_runtime"):
        result.source_context = source_tools.build_source_context(state["source_runtime"])

    # ─── 5. 監査エージェント (Phase C, オプション) ─────────────────
    if audit_after_integrator:
        from log_analyzer.audit_agent import run_audit  # 遅延 import (依存軽量化)
        yield StreamEvent("audit_start", {"model_hint": "gpt-5.5"})
        try:
            audit = await _run_sync(
                lambda: run_audit(
                    log_text, topology_context, result,
                    system_prompt=audit_system_prompt,
                    bq_evidence=state.get("bq_evidence"),
                    # 監査もこのトレースに Generation として残す (確認事項 B-1)
                    trace_id=trace_id,
                )
            )
        except Exception as e:
            yield StreamEvent("error", {"stage": "audit", "message": str(e)})
            audit = None
        if audit is not None:
            result.audit_report = audit
            yield StreamEvent(
                "audit_done",
                {
                    "verdict": audit.verdict,
                    "confidence": audit.confidence,
                    "concerns": len(audit.concerns),
                    "alternatives": len(audit.alternative_hypotheses),
                    "tokens_in": audit.tokens_in,
                    "tokens_out": audit.tokens_out,
                    "latency_ms": audit.latency_ms,
                    "model": audit.model,
                },
            )

    trace.update(output=result.model_dump(mode="json"))
    flush()
    # bq_evidence は監査専用の証拠。2 段ラッパ (_run_one_stage) が final を傍受して
    # 取り出すために final イベントに同梱する (UI はこのキーを無視してよい)。
    yield StreamEvent(
        "final",
        {"result": result.model_dump(mode="json"), "bq_evidence": state.get("bq_evidence") or []},
    )


# ─── 非ストリーミング呼出ラッパ ──────────────────────────────────────


async def run_rally_async(
    log_text: str,
    log_ref: str = "inline",
    *,
    prompt_overrides: dict[str, str] | None = None,
    model_overrides: dict[str, str] | None = None,
    rally_max_rounds: int | None = None,
    bq_sources: dict[str, dict] | None = None,
) -> AnalysisResult:
    """非対話・非ストリーミングの async 呼出。/api/runs から使う。

    確認モーダルは出さず、上限到達で強制 finalize（``decision_waiter=None``）。
    """
    final_payload: dict | None = None
    async for ev in run_rally_stream(
        log_text,
        log_ref,
        prompt_overrides=prompt_overrides,
        model_overrides=model_overrides,
        rally_max_rounds=rally_max_rounds or 3,
        decision_waiter=None,
        bq_sources=bq_sources,
    ):
        if ev.kind == "final":
            final_payload = ev.data["result"]
        elif ev.kind == "error":
            raise RuntimeError(f"rally stream error: {ev.data}")
    if final_payload is None:
        raise RuntimeError("rally stream ended without producing a final result")
    return AnalysisResult.model_validate(final_payload)


def run_rally(
    log_text: str,
    log_ref: str = "inline",
    *,
    prompt_overrides: dict[str, str] | None = None,
    model_overrides: dict[str, str] | None = None,
    rally_max_rounds: int | None = None,
) -> AnalysisResult:
    """CLI / compare_configs.py 互換の同期エントリポイント。"""
    return asyncio.run(
        run_rally_async(
            log_text,
            log_ref,
            prompt_overrides=prompt_overrides,
            model_overrides=model_overrides,
            rally_max_rounds=rally_max_rounds,
        )
    )
