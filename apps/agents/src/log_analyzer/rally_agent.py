"""構成4 — ラリー型 Multi-Agent (LangGraph stategraph)。

パイプライン:
    log text
      -> orchestrator (どの監視を呼ぶか決める)
      -> [fw / routing / app の必要なものだけ並列実行]
      -> rally_check (escalation があれば追加監視を呼ぶ)
      -> [追加監視（最大 1 ラウンド）]
      -> integrator (AnalysisResult に統合)

LangGraph の StateGraph を素手で書く（"Style A"）。`human_judgment_required` の
強制（議事録 L3）は integrator の SYSTEM_PROMPT で文言固定し、上書きしない方針。
"""
from __future__ import annotations

import time

from langgraph.graph import END, START, StateGraph

from log_analyzer.rally.integrator import integrator_node
from log_analyzer.rally.monitors import MONITOR_FNS
from log_analyzer.rally.orchestrator import orchestrator_node
from log_analyzer.rally.state import Config4State
from log_analyzer.schema import (
    AnalysisResult,
    ConfigId,
    GraphEdge,
    GraphNode,
    Metrics,
    RecommendedAction,
    RootCauseCandidate,
)
from log_analyzer.tracing import flush, get_client


def rally_check_node(state: Config4State) -> dict:
    """監視結果を見て、ラリー（追加監視）が必要か判断し state を更新する。

    Returns:
        rally_targets_pending: 次に呼ぶ監視のリスト（空ならラリー不要 → integrator へ）
        rally_round: ラリーが起きた場合のみ 2 にインクリメント
    """
    rally_round = state.get("rally_round", 1)
    if rally_round >= 2:
        return {"rally_targets_pending": []}

    escalations = state.get("escalations", [])
    if not escalations:
        return {"rally_targets_pending": []}

    # 既に round 1 で呼ばれた監視は再呼出しスキップ（同じ入力で 2 回呼んでも価値が低い）
    already_invoked = set(state.get("monitor_results", {}).keys())
    targets = [m for m in escalations if m not in already_invoked and m in MONITOR_FNS]
    targets = list(dict.fromkeys(targets))  # 順序保ったまま重複排除

    if not targets:
        return {"rally_targets_pending": []}

    return {"rally_round": 2, "rally_targets_pending": targets}


def _route_after_orchestrator(state: Config4State) -> list[str]:
    return state["orchestrator_decision"]["invoke"]


def _route_after_rally_check(state: Config4State) -> list[str]:
    targets = state.get("rally_targets_pending", [])
    if not targets:
        return ["integrator"]
    return targets


def build_graph():
    graph = StateGraph(Config4State)

    graph.add_node("orchestrator", orchestrator_node)
    graph.add_node("fw", MONITOR_FNS["fw"])
    graph.add_node("routing", MONITOR_FNS["routing"])
    graph.add_node("app", MONITOR_FNS["app"])
    graph.add_node("rally_check", rally_check_node)
    graph.add_node("integrator", integrator_node)

    graph.add_edge(START, "orchestrator")

    # orchestrator → 必要な監視へ並列ファンアウト
    graph.add_conditional_edges(
        "orchestrator",
        _route_after_orchestrator,
        {"fw": "fw", "routing": "routing", "app": "app"},
    )

    # 各監視 → rally_check（fan-in、LangGraph が暗黙で同期）
    for m in ("fw", "routing", "app"):
        graph.add_edge(m, "rally_check")

    # rally_check → 追加監視 or integrator
    graph.add_conditional_edges(
        "rally_check",
        _route_after_rally_check,
        {
            "integrator": "integrator",
            "fw": "fw",
            "routing": "routing",
            "app": "app",
        },
    )

    graph.add_edge("integrator", END)

    return graph.compile()


def run_rally(
    log_text: str,
    log_ref: str = "inline",
    prompt_overrides: dict[str, str] | None = None,
    model_overrides: dict[str, str] | None = None,
) -> AnalysisResult:
    langfuse = get_client()
    trace = langfuse.trace(
        name="config4-rally",
        input={"log_ref": log_ref, "log_size_bytes": len(log_text)},
        metadata={"config_id": ConfigId.CONFIG4.value, "schema_version": "v0.1"},
    )

    compiled = build_graph()
    wall_start = time.perf_counter()
    final_state: Config4State = compiled.invoke(
        {
            "log_text": log_text,
            "log_ref": log_ref,
            "prompt_overrides": prompt_overrides or {},
            "model_overrides": model_overrides or {},
        }
    )  # type: ignore[assignment]
    wall_ms = int((time.perf_counter() - wall_start) * 1000)

    # 全 LLM 呼び出しを Langfuse の Generation として記録
    for entry in final_state.get("token_log", []) or []:
        trace.generation(
            name=f"{entry['model']}-{entry['role']}",
            model=entry["model"],
            input=entry.get("input", "")[:2000],
            output=entry.get("raw_output", ""),
            usage_details={
                "input": entry["tokens_in"],
                "output": entry["tokens_out"],
            },
        )

    integrated = final_state.get("integrator_result", {}) or {}
    token_log = final_state.get("token_log", []) or []
    total_in = sum(e["tokens_in"] for e in token_log)
    total_out = sum(e["tokens_out"] for e in token_log)
    per_call_latencies = sorted(e["latency_ms"] for e in token_log)
    sum_latency = sum(per_call_latencies)
    p50 = per_call_latencies[len(per_call_latencies) // 2] if per_call_latencies else 0

    info_loss: list[str] = []
    info_loss.append(
        f"orchestrator invoked: {final_state.get('orchestrator_decision', {}).get('invoke', [])}"
    )
    info_loss.append(f"rally_round_completed: {final_state.get('rally_round', 1)}")
    info_loss.append(
        f"timing: wall_clock_ms={wall_ms}, sum_ms={sum_latency}, "
        f"parallelism_ratio={(sum_latency / wall_ms):.2f}x" if wall_ms else "timing: n/a"
    )
    for monitor_name, mr in (final_state.get("monitor_results") or {}).items():
        info_loss.append(
            f"per-monitor {monitor_name} confidence={mr.get('confidence', '?')}, "
            f"escalate_to={mr.get('escalate_to', [])}"
        )

    # token_log を組み立てて execution_graph に変換
    graph_nodes: list[GraphNode] = []
    graph_edges: list[GraphEdge] = []
    seen_ids: set[str] = set()
    for entry in token_log:
        role = entry["role"]  # "orchestrator" / "fw_monitor" / "routing_monitor" / "app_monitor" / "integrator"
        node_id = role
        if node_id in seen_ids:
            # 同じ監視が round1/2 両方で呼ばれた場合は後勝ち（最新の latency / tokens で上書き）
            for n in graph_nodes:
                if n.id == node_id:
                    n.latency_ms = entry["latency_ms"]
                    n.tokens_in = entry["tokens_in"]
                    n.tokens_out = entry["tokens_out"]
                    n.metadata["rounds_invoked"] = (
                        n.metadata.get("rounds_invoked", [1]) + [entry.get("round", 2)]
                    )
            continue
        seen_ids.add(node_id)
        if role == "orchestrator":
            node_role = "orchestrator"
        elif role == "integrator":
            node_role = "integrator"
        else:
            node_role = "monitor"
        node_meta: dict = {}
        if node_role == "monitor":
            node_meta["round"] = entry.get("round", 1)
        graph_nodes.append(
            GraphNode(
                id=node_id,
                label=entry["model"],
                role=node_role,
                model=entry["model"],
                latency_ms=entry["latency_ms"],
                tokens_in=entry["tokens_in"],
                tokens_out=entry["tokens_out"],
                metadata=node_meta,
            )
        )

    # エッジ: orchestrator → 各 monitor、各 monitor → integrator
    for n in graph_nodes:
        if n.role == "monitor":
            graph_edges.append(GraphEdge(source="orchestrator", target=n.id))
            graph_edges.append(GraphEdge(source=n.id, target="integrator"))

    result = AnalysisResult(
        config_id=ConfigId.CONFIG4,
        input_log_ref=log_ref,
        root_cause_candidates=[
            RootCauseCandidate(**c) for c in integrated.get("root_cause_candidates", [])
        ],
        recommended_actions=[
            RecommendedAction(**a) for a in integrated.get("recommended_actions", [])
        ],
        confidence=float(integrated.get("confidence", 0.0)),
        metrics=Metrics(
            tokens_in=total_in,
            tokens_out=total_out,
            latency_ms_total=wall_ms,
            latency_ms_p50=p50,
        ),
        info_loss_flags=info_loss,
        execution_graph_nodes=graph_nodes,
        execution_graph_edges=graph_edges,
    )

    trace.update(output=result.model_dump(mode="json"))
    flush()
    return result
