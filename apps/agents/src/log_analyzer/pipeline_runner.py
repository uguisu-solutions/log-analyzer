"""構成5（user_pipeline）— UI で組み立てたパイプラインの汎用実行ランナー。

データモデル:
    pipeline_def = {
        "nodes": [
            {"id": "input", "type": "input"},
            {"id": "<n>", "type": "llm", "prompt": "...",
             "model": "claude-opus-4-7", "input_template": "..."},
            {"id": "output", "type": "output", "prompt": "...",
             "model": "claude-opus-4-7", "input_template": "..."},
        ],
        "edges": [{"source": "<from>", "target": "<to>"}, ...]
    }

実行モデル:
    1. nodes を依存順にトポロジカルソート
    2. 同じ依存深度のノードは asyncio.gather で並列実行
    3. 各 LLM ノードは ``input_template`` を使って上流ノード出力を結合
       （プレースホルダ ``{input}`` は元ログ、``{<node_id>}`` は該当ノード出力）
    4. ``output`` タイプノードの応答は AnalysisResult JSON としてパースされる

制約（MVP）:
    - 1 つの ``input`` ノード（id 固定 "input"、削除不可）
    - 1 つの ``output`` ノード（type=output、AnalysisResult を返す）
    - サイクルなし（DAG 必須）
    - 全ノードが output から到達可能
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from collections import defaultdict, deque
from typing import Any

import anthropic

from log_analyzer.schema import (
    AnalysisResult,
    ConfigId,
    GraphEdge,
    GraphNode,
    Metrics,
    RecommendedAction,
    RootCauseCandidate,
)
from log_analyzer.tracing import flush, get_client, usage_for


# ─── デフォルトプロンプト ──────────────────────────────────────────────

DEFAULT_LLM_PROMPT = """\
あなたはネットワーク／システムログ分析の補助エージェントです。
渡された情報から、自分が担当する観点で重要な事実・所見を整理して短く返してください。

ルール:
- 出力は自然文（JSON 不要）。次のノードがさらに統合・整形する前提
- 200 〜 500 文字程度
- 推測は控えめに、ログ行の根拠とともに記述
"""

DEFAULT_OUTPUT_PROMPT = """\
あなたはログ分析の最終統合エージェントです。
上流ノードから渡された情報を統合し、共通スキーマ AnalysisResult を JSON のみで返してください。

出力 (JSON のみ):
{
  "root_cause_candidates": [
    {"category": "FW|Net|App|DNS|Sec|Unknown", "summary": "...", "evidence": ["..."]}
  ],
  "recommended_actions": [
    {"action": "...", "human_judgment_required": true, "risk_level": "low|mid|high"}
  ],
  "confidence": 0.0
}

ルール:
- 候補は最大 3 件まで。配列順は確信度順でよいが「rank 1 が最良」のような順位強調表現は使わない (UI は並列表示)
- ロールバック・再起動・設定変更・データ削除を伴うアクションは必ず `human_judgment_required: true`（議事録 L3）
- フィールド名・enum 値は英語、`summary` / `action` の自然文は日本語
- コードフェンスで囲まない
"""

DEFAULT_INPUT_TEMPLATE_LLM = "{input}"
DEFAULT_INPUT_TEMPLATE_OUTPUT = "## 元ログ\n{input}\n\n## 上流ノードの出力\n{__upstream__}"

# ノードタイプの仕様（API /api/node-types で配信）
NODE_TYPE_DEFS = [
    {
        "type": "input",
        "label": "Input（入力）",
        "description": "log_text を下流に流す。1 個固定、削除不可、プロンプト/モデル無し。",
        "fixed": True,
        "editable_fields": [],
    },
    {
        "type": "llm",
        "label": "LLM Call",
        "description": "上流テキストを system prompt + model で LLM に投入し、自然文を返す。",
        "fixed": False,
        "editable_fields": ["prompt", "model", "input_template"],
        "default_prompt": DEFAULT_LLM_PROMPT,
        "default_model": "claude-opus-4-7",
        "default_input_template": DEFAULT_INPUT_TEMPLATE_LLM,
    },
    {
        "type": "output",
        "label": "Output（出力、AnalysisResult JSON）",
        "description": "1 個固定、削除不可。prompt 末尾に JSON スキーマ強制が必要。",
        "fixed": True,
        "editable_fields": ["prompt", "model", "input_template"],
        "default_prompt": DEFAULT_OUTPUT_PROMPT,
        "default_model": "claude-opus-4-7",
        "default_input_template": DEFAULT_INPUT_TEMPLATE_OUTPUT,
    },
]

ALLOWED_MODELS = ["claude-opus-4-7"]


# ─── パイプライン検証 ─────────────────────────────────────────────────


class PipelineValidationError(ValueError):
    pass


def validate_pipeline(pipeline_def: dict) -> None:
    """pipeline_def の構造的整合性を検証する。違反時は ``PipelineValidationError`` を raise。"""
    nodes = pipeline_def.get("nodes", [])
    edges = pipeline_def.get("edges", [])
    if not isinstance(nodes, list) or not isinstance(edges, list):
        raise PipelineValidationError("nodes と edges は list であること")

    ids: set[str] = set()
    type_counts: dict[str, int] = defaultdict(int)
    for n in nodes:
        nid = n.get("id")
        ntype = n.get("type")
        if not nid or not isinstance(nid, str):
            raise PipelineValidationError(f"ノード id が不正: {n}")
        if nid in ids:
            raise PipelineValidationError(f"ノード id が重複: {nid}")
        ids.add(nid)
        if ntype not in {"input", "llm", "output"}:
            raise PipelineValidationError(f"不明なノードタイプ: {ntype}（id={nid}）")
        type_counts[ntype] += 1
        if ntype in {"llm", "output"}:
            model = n.get("model", "claude-opus-4-7")
            if model not in ALLOWED_MODELS:
                raise PipelineValidationError(f"許可されないモデル: {model}（id={nid}）")

    if type_counts["input"] != 1:
        raise PipelineValidationError(f"input ノードは 1 個必須（現在 {type_counts['input']} 個）")
    if type_counts["output"] != 1:
        raise PipelineValidationError(f"output ノードは 1 個必須（現在 {type_counts['output']} 個）")

    for e in edges:
        s, t = e.get("source"), e.get("target")
        if s not in ids or t not in ids:
            raise PipelineValidationError(f"未知のノードを参照する edge: {e}")
        if s == t:
            raise PipelineValidationError(f"自己ループ edge: {e}")

    # サイクル検出: トポロジカルソートを試みる
    try:
        _topological_layers(nodes, edges)
    except PipelineValidationError:
        raise
    except Exception as e:
        raise PipelineValidationError(f"DAG として処理できない: {e}")


def _topological_layers(nodes: list[dict], edges: list[dict]) -> list[list[str]]:
    """ノードを依存深度ごとのレイヤに分割（同レイヤは並列実行可能）。"""
    indeg: dict[str, int] = {n["id"]: 0 for n in nodes}
    out_edges: dict[str, list[str]] = defaultdict(list)
    for e in edges:
        indeg[e["target"]] = indeg.get(e["target"], 0) + 1
        out_edges[e["source"]].append(e["target"])

    layers: list[list[str]] = []
    queue: deque[str] = deque([nid for nid, d in indeg.items() if d == 0])
    visited = 0
    while queue:
        layer = list(queue)
        layers.append(layer)
        next_queue: deque[str] = deque()
        for nid in layer:
            visited += 1
            for tgt in out_edges[nid]:
                indeg[tgt] -= 1
                if indeg[tgt] == 0:
                    next_queue.append(tgt)
        queue = next_queue
    if visited != len(nodes):
        raise PipelineValidationError("グラフにサイクルが含まれている、または到達不能ノードがある")
    return layers


# ─── 実行エンジン ─────────────────────────────────────────────────────


def _format_input(node: dict, log_text: str, upstream_outputs: dict[str, str]) -> str:
    """node.input_template のプレースホルダを実際の値で埋める。

    予約プレースホルダ:
        ``{input}``: 元のログ全文
        ``{__upstream__}``: 全上流ノード出力を ``id: 出力\\n`` で連結
        ``{<node_id>}``: 該当ノードの出力
    """
    template = node.get("input_template") or DEFAULT_INPUT_TEMPLATE_LLM
    upstream_block = "\n\n".join(
        f"### {nid}\n{out}" for nid, out in upstream_outputs.items()
    )
    fmt_args: dict[str, str] = {
        "input": log_text,
        "__upstream__": upstream_block,
        **upstream_outputs,
    }
    try:
        return template.format_map(_DefaultDict(fmt_args))
    except Exception as e:
        # format エラーは debug しやすく
        raise PipelineValidationError(f"node {node.get('id')} の input_template に問題: {e}")


class _DefaultDict(dict):
    """str.format_map で未知のキーが渡された時に空文字を返す（テンプレ書き間違いに寛容）。"""

    def __missing__(self, key: str) -> str:
        return f"{{{key}}}"  # 元のプレースホルダ表記をそのまま残す（debug 容易）


async def _run_llm_node(
    node: dict,
    log_text: str,
    upstream_outputs: dict[str, str],
) -> tuple[str, dict[str, Any]]:
    """LLM / output ノードを実行し、(出力テキスト, トレース用 metadata) を返す。"""
    model = node.get("model", "claude-opus-4-7")
    system_prompt = node.get("prompt") or (
        DEFAULT_OUTPUT_PROMPT if node.get("type") == "output" else DEFAULT_LLM_PROMPT
    )
    user_input = _format_input(node, log_text, upstream_outputs)

    client = anthropic.AsyncAnthropic()
    started = time.perf_counter()
    response = await client.messages.create(
        model=model,
        max_tokens=2000,
        system=system_prompt,
        messages=[{"role": "user", "content": user_input}],
    )
    latency_ms = int((time.perf_counter() - started) * 1000)
    text = response.content[0].text
    return text, {
        "model": model,
        "latency_ms": latency_ms,
        "tokens_in": response.usage.input_tokens,
        "tokens_out": response.usage.output_tokens,
        "user_input_preview": user_input[:1500],
        "raw_output": text,
    }


def run_user_pipeline(
    log_text: str,
    log_ref: str = "inline",
    prompt_overrides: dict[str, str] | None = None,
    model_overrides: dict[str, str] | None = None,
    pipeline_def: dict | None = None,
) -> AnalysisResult:
    """構成5: ユーザー定義パイプライン実行のエントリポイント。

    ``prompt_overrides`` と ``model_overrides`` は構成1〜4 との API 互換のため受け取るが、
    本ランナーは pipeline_def 内のノード定義を優先する（slot ベース上書きは未対応）。
    """
    if not pipeline_def:
        raise PipelineValidationError("config5 (user_pipeline) は pipeline_def が必須")
    validate_pipeline(pipeline_def)
    return asyncio.run(_run_pipeline_async(log_text, log_ref, pipeline_def))


async def _run_pipeline_async(
    log_text: str, log_ref: str, pipeline_def: dict
) -> AnalysisResult:
    nodes = pipeline_def["nodes"]
    edges = pipeline_def["edges"]
    nodes_by_id: dict[str, dict] = {n["id"]: n for n in nodes}
    layers = _topological_layers(nodes, edges)
    upstream_map: dict[str, list[str]] = defaultdict(list)
    for e in edges:
        upstream_map[e["target"]].append(e["source"])

    langfuse = get_client()
    trace = langfuse.trace(
        name="config5-pipeline",
        input={
            "log_ref": log_ref,
            "log_size_bytes": len(log_text),
            "node_count": len(nodes),
        },
        metadata={"config_id": ConfigId.CONFIG5.value, "schema_version": "v0.1"},
    )

    outputs: dict[str, str] = {}
    per_node_meta: dict[str, dict] = {}

    wall_start = time.perf_counter()
    for layer in layers:
        # 同レイヤのノードを並列実行
        tasks = []
        layer_node_ids: list[str] = []
        for nid in layer:
            node = nodes_by_id[nid]
            ntype = node["type"]
            if ntype == "input":
                outputs[nid] = log_text
                continue
            upstreams = upstream_map[nid]
            up_outputs = {u: outputs[u] for u in upstreams if u in outputs}
            tasks.append(_run_llm_node(node, log_text, up_outputs))
            layer_node_ids.append(nid)
        if tasks:
            results = await asyncio.gather(*tasks)
            for nid, (text, meta) in zip(layer_node_ids, results):
                outputs[nid] = text
                per_node_meta[nid] = meta
                trace.generation(
                    name=f"config5-{nid}",
                    model=meta["model"],
                    input=meta["user_input_preview"],
                    output=meta["raw_output"],
                    usage=usage_for(meta["model"], meta["tokens_in"], meta["tokens_out"]),
                )
    wall_ms = int((time.perf_counter() - wall_start) * 1000)

    # output ノードの結果を AnalysisResult 形式にパース
    output_node = next(n for n in nodes if n["type"] == "output")
    output_text = outputs[output_node["id"]]
    parsed = _extract_json(output_text)

    total_tokens_in = sum(m["tokens_in"] for m in per_node_meta.values())
    total_tokens_out = sum(m["tokens_out"] for m in per_node_meta.values())

    info_loss: list[str] = []
    info_loss.append(f"node_count: {len(nodes)} (layers={len(layers)})")
    info_loss.append(
        f"timing: wall_clock_ms={wall_ms}, "
        f"sum_ms={sum(m['latency_ms'] for m in per_node_meta.values())}"
    )
    for nid, m in per_node_meta.items():
        info_loss.append(
            f"node {nid} model={m['model']} "
            f"tokens={m['tokens_in']}/{m['tokens_out']} latency={m['latency_ms']}ms"
        )

    # execution_graph をパイプライン定義から組み立てる
    graph_nodes_out: list[GraphNode] = []
    graph_edges_out: list[GraphEdge] = []
    for n in nodes:
        nid = n["id"]
        meta = per_node_meta.get(nid)
        role = (
            "model_call"
            if n["type"] == "llm"
            else ("integrator" if n["type"] == "output" else "filter")
        )
        graph_nodes_out.append(
            GraphNode(
                id=nid,
                label=meta["model"] if meta else n["type"],
                role=role,
                model=meta["model"] if meta else None,
                latency_ms=meta["latency_ms"] if meta else None,
                tokens_in=meta["tokens_in"] if meta else None,
                tokens_out=meta["tokens_out"] if meta else None,
                metadata={"node_type": n["type"]},
            )
        )
    for e in edges:
        graph_edges_out.append(GraphEdge(source=e["source"], target=e["target"]))

    result = AnalysisResult(
        trace_id=str(trace.id),
        config_id=ConfigId.CONFIG5,
        input_log_ref=log_ref,
        root_cause_candidates=[
            RootCauseCandidate(**c) for c in parsed.get("root_cause_candidates", [])
        ],
        recommended_actions=[
            RecommendedAction(**a) for a in parsed.get("recommended_actions", [])
        ],
        confidence=float(parsed.get("confidence", 0.0)),
        metrics=Metrics(
            tokens_in=total_tokens_in,
            tokens_out=total_tokens_out,
            latency_ms_total=wall_ms,
            latency_ms_p50=sorted(m["latency_ms"] for m in per_node_meta.values())[
                len(per_node_meta) // 2
            ]
            if per_node_meta
            else 0,
        ),
        info_loss_flags=info_loss,
        execution_graph_nodes=graph_nodes_out,
        execution_graph_edges=graph_edges_out,
    )
    trace.update(output=result.model_dump(mode="json"))
    flush()
    return result


def _extract_json(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        parts = text.split("```")
        if len(parts) >= 2:
            inner = parts[1]
            if inner.startswith("json"):
                inner = inner[4:]
            text = inner
    return json.loads(text.strip())
