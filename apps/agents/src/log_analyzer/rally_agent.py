"""構成4 — オーケストレータ駆動 Multi-Agent (LangGraph stategraph)。

パイプライン:
    log text
      -> orchestrator (どの監視を呼ぶか / 統合に進むかを毎ターン判断)
         ↑                              ↓ conditional
         └── monitors (fw / routing / app の必要なものだけ並列実行)
                                        ↓ finalize
                                  integrator (AnalysisResult に統合)

旧版にあった ``rally_check_node`` は削除。判断はすべて orchestrator に集約され、
監視結果を見て「もう一度呼ぶ／統合に進む」を毎ラウンド orchestrator が決める。
ループ上限は ``state["rally_max_rounds"]``（既定 3、env RALLY_MAX_ROUNDS で上書き可）。
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
    OrchestratorDecisionDTO,
    RecommendedAction,
    RootCauseCandidate,
)
from log_analyzer.tracing import flush, get_client


def _route_after_orchestrator(state: Config4State) -> list[str]:
    """orchestrator の決定に基づき次のノード群を返す。

    - action="invoke" かつ invoke が非空 → 該当監視へ並列ファンアウト
    - それ以外（finalize / 不正） → integrator へ
    """
    decision = state.get("orchestrator_decision") or {}
    if decision.get("action") == "invoke":
        invoke = decision.get("invoke") or []
        if invoke:
            return list(invoke)
    return ["integrator"]


def build_graph():
    graph = StateGraph(Config4State)

    graph.add_node("orchestrator", orchestrator_node)
    graph.add_node("fw", MONITOR_FNS["fw"])
    graph.add_node("routing", MONITOR_FNS["routing"])
    graph.add_node("app", MONITOR_FNS["app"])
    graph.add_node("integrator", integrator_node)

    graph.add_edge(START, "orchestrator")

    # orchestrator → 監視（並列ファンアウト） or integrator
    graph.add_conditional_edges(
        "orchestrator",
        _route_after_orchestrator,
        {
            "fw": "fw",
            "routing": "routing",
            "app": "app",
            "integrator": "integrator",
        },
    )

    # 各監視 → orchestrator に戻る（fan-in、orchestrator が再評価）
    for m in ("fw", "routing", "app"):
        graph.add_edge(m, "orchestrator")

    graph.add_edge("integrator", END)

    # rally_max_rounds=3 のとき最悪 6 ラウンド分のスーパーステップ
    # (orchestrator + monitors) + integrator ≒ 8 super-steps なので
    # デフォルトの recursion_limit=25 で十分余裕がある。
    return graph.compile()


def _aggregate_graph_nodes(token_log: list[dict]) -> tuple[list[GraphNode], list[GraphEdge]]:
    """token_log から execution_graph 用のノード/エッジを構築する。

    同じ role が複数回呼ばれた場合は tokens / latency を合算し、
    ``metadata.invocations`` に呼出回数を記録する。
    """
    by_role: dict[str, dict] = {}
    for entry in token_log:
        role = entry["role"]
        agg = by_role.setdefault(
            role,
            {
                "model": entry["model"],
                "latency_ms": 0,
                "tokens_in": 0,
                "tokens_out": 0,
                "invocations": 0,
                "rounds": [],
            },
        )
        agg["latency_ms"] += entry["latency_ms"]
        agg["tokens_in"] += entry["tokens_in"]
        agg["tokens_out"] += entry["tokens_out"]
        agg["invocations"] += 1
        if "round" in entry:
            agg["rounds"].append(entry["round"])

    nodes: list[GraphNode] = []
    invoked_monitors: set[str] = set()
    for role, agg in by_role.items():
        if role == "orchestrator":
            node_role = "orchestrator"
        elif role == "integrator":
            node_role = "integrator"
        else:
            node_role = "monitor"
            # role は "fw_monitor" / "routing_monitor" / "app_monitor"
            invoked_monitors.add(role.replace("_monitor", ""))
        meta: dict = {"invocations": agg["invocations"]}
        if agg["rounds"]:
            meta["rounds"] = agg["rounds"]
        nodes.append(
            GraphNode(
                id=role,
                label=agg["model"],
                role=node_role,
                model=agg["model"],
                latency_ms=agg["latency_ms"],
                tokens_in=agg["tokens_in"],
                tokens_out=agg["tokens_out"],
                metadata=meta,
            )
        )

    # エッジ: orchestrator ↔ 各 invoked monitor、orchestrator → integrator（呼ばれていれば）
    edges: list[GraphEdge] = []
    has_orchestrator = any(n.id == "orchestrator" for n in nodes)
    has_integrator = any(n.id == "integrator" for n in nodes)
    for m in invoked_monitors:
        monitor_id = f"{m}_monitor"
        if has_orchestrator:
            edges.append(GraphEdge(source="orchestrator", target=monitor_id))
            edges.append(GraphEdge(source=monitor_id, target="orchestrator"))
    if has_orchestrator and has_integrator:
        edges.append(GraphEdge(source="orchestrator", target="integrator"))

    return nodes, edges


def run_rally(
    log_text: str,
    log_ref: str = "inline",
    prompt_overrides: dict[str, str] | None = None,
    model_overrides: dict[str, str] | None = None,
    rally_max_rounds: int | None = None,
    rally_force_min_rounds: int | None = None,
) -> AnalysisResult:
    langfuse = get_client()
    trace = langfuse.trace(
        name="config4-rally",
        input={"log_ref": log_ref, "log_size_bytes": len(log_text)},
        metadata={"config_id": ConfigId.CONFIG4.value, "schema_version": "v0.1"},
    )

    compiled = build_graph()
    wall_start = time.perf_counter()
    initial_state: dict = {
        "log_text": log_text,
        "log_ref": log_ref,
        "prompt_overrides": prompt_overrides or {},
        "model_overrides": model_overrides or {},
    }
    if rally_max_rounds is not None and rally_max_rounds > 0:
        initial_state["rally_max_rounds"] = rally_max_rounds
    if rally_force_min_rounds is not None and rally_force_min_rounds > 0:
        initial_state["rally_force_min_rounds"] = rally_force_min_rounds
    final_state: Config4State = compiled.invoke(initial_state)  # type: ignore[assignment]
    wall_ms = int((time.perf_counter() - wall_start) * 1000)

    # 全 LLM 呼び出しを Langfuse の Generation として記録
    for entry in final_state.get("token_log", []) or []:
        trace.generation(
            name=f"{entry['model']}-{entry['role']}-r{entry.get('round', '?')}",
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

    history = final_state.get("orchestrator_history") or []
    last_decision = final_state.get("orchestrator_decision") or {}
    final_round = final_state.get("rally_round", 0)
    max_rounds = final_state.get("rally_max_rounds", 0)

    info_loss: list[str] = []
    info_loss.append(f"rally_rounds_completed: {final_round} (max={max_rounds})")
    info_loss.append(
        f"orchestrator_decisions: {len(history)} "
        f"(forced_finalize={sum(1 for d in history if d.get('forced'))})"
    )
    if last_decision.get("forced"):
        info_loss.append("final_action: forced_finalize_due_to_max_rounds")
    else:
        info_loss.append(f"final_action: {last_decision.get('action', 'unknown')}")
    info_loss.append(
        f"timing: wall_clock_ms={wall_ms}, sum_ms={sum_latency}, "
        + (f"parallelism_ratio={(sum_latency / wall_ms):.2f}x" if wall_ms else "n/a")
    )
    for monitor_name, mr in (final_state.get("monitor_results") or {}).items():
        info_loss.append(
            f"per-monitor {monitor_name} confidence={mr.get('confidence', '?')}"
        )
        if mr.get("_parse_error"):
            info_loss.append(
                f"per-monitor {monitor_name} parse_error: {mr['_parse_error']}"
            )
    # integrator の parse 失敗（応答切断など）も info_loss に立てる
    if integrated.get("_parse_error"):
        info_loss.append(f"integrator_parse_error: {integrated['_parse_error']}")
    # orchestrator の parse 失敗履歴も列挙
    for d in history:
        if d.get("parse_error"):
            info_loss.append(
                f"orchestrator_parse_error round={d.get('round')}: {d['parse_error']}"
            )

    graph_nodes, graph_edges = _aggregate_graph_nodes(token_log)

    history_dtos = [
        OrchestratorDecisionDTO(
            round=int(d.get("round", 0)),
            action=str(d.get("action", "")),
            invoke=list(d.get("invoke", []) or []),
            focus_hints=dict(d.get("focus_hints", {}) or {}),
            rationale=str(d.get("rationale", "")),
            forced=bool(d.get("forced", False)),
        )
        for d in history
    ]

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
        orchestrator_rounds=int(final_round),
        orchestrator_max_rounds=int(max_rounds),
        orchestrator_history=history_dtos,
    )

    trace.update(output=result.model_dump(mode="json"))
    flush()
    return result
