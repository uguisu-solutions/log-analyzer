r"""Phase F — 問診票あり/なしの同一シナリオ比較ベンチマーク。

議事録「問診票（指標）の有無両条件で同一シナリオを評価し、スコアリング、
ラウンド数、精度、速度を網羅する」に対応。

使い方:
    cd apps\agents
    .\.venv\Scripts\Activate.ps1

    # scenario2 (FW ACL コメントアウト) で問診票あり/なしを比較
    python scripts\benchmark_questionnaire.py ^
        --scenario ..\..\samples\topology\scenario2_api_acl_missing ^
        --runs 1 ^
        --output bench-scenario2.csv

シナリオディレクトリの規約:
    <scenario_dir>/
        diagram.svg          (UI 用、本スクリプトは不要)
        <node-id>.conf       → node_configs[<node-id>]
        <node-id>.log        → node_logs[<node-id>]
        questionnaire.json   (任意、無ければデフォルトの 5 項目を埋めて使う)

出力:
    - 標準出力に Markdown 表
    - --output <path> で CSV を出力
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

from log_analyzer.api import _build_topology_log_text
from log_analyzer.rally_agent import run_rally_async
from log_analyzer.schema import AnalysisResult


# 名前 prefix からノード type を推定する規約
_TYPE_HINT: dict[str, str] = {
    "fw": "FW",
    "lb": "LB",
    "rt": "Router",
    "sw": "Switch",
    "web": "Server",
    "api": "Server",
    "db": "DB",
    "srv": "Server",
    "app": "Server",
    "core": "L3SW",
    "acc": "L2SW",
}

# デフォルト問診票 (シナリオ間で共通の汎用回答)。
# 本来は scenario_dir/questionnaire.json で上書きする。
_DEFAULT_ANSWERS_NONEMPTY: dict[str, str] = {
    "symptom_onset": "今朝 09:00 頃から特定機能が応答しない",
    "scope": "特定機能のみ (API 経路)",
    "reproducibility": "常に再現",
    "recent_changes": "前日 18:30 にネットワーク機器のポリシ変更があった",
    "free_notes": "通常運用パスは健全だが、新しく追加した API 経路だけが断続的",
}


@dataclass
class ScenarioLoad:
    nodes: list[dict] = field(default_factory=list)  # [{id, type, label, ip}]
    node_configs: dict[str, list[dict]] = field(default_factory=dict)  # {id: [{name, content}]}
    node_logs: dict[str, list[dict]] = field(default_factory=dict)
    questionnaire: dict[str, str] = field(default_factory=dict)


def _infer_type(node_id: str) -> str:
    prefix = node_id.split("-", 1)[0].lower()
    return _TYPE_HINT.get(prefix, "")


def load_scenario(scenario_dir: Path) -> ScenarioLoad:
    if not scenario_dir.exists():
        raise FileNotFoundError(f"scenario not found: {scenario_dir}")
    s = ScenarioLoad()
    # node_id 候補は .conf / .log のファイル名 stem の合集合
    node_ids: set[str] = set()
    for p in scenario_dir.glob("*.conf"):
        node_ids.add(p.stem)
    for p in scenario_dir.glob("*.log"):
        node_ids.add(p.stem)
    # 一貫した順序を出すため sorted
    for nid in sorted(node_ids):
        s.nodes.append({"id": nid, "type": _infer_type(nid), "label": "", "ip": ""})
    for nid in sorted(node_ids):
        conf = scenario_dir / f"{nid}.conf"
        if conf.exists():
            s.node_configs[nid] = [{"name": conf.name, "content": conf.read_text(encoding="utf-8", errors="replace")}]
        log = scenario_dir / f"{nid}.log"
        if log.exists():
            s.node_logs[nid] = [{"name": log.name, "content": log.read_text(encoding="utf-8", errors="replace")}]
    # 問診票は scenario_dir/questionnaire.json を最優先、無ければデフォルト
    q_path = scenario_dir / "questionnaire.json"
    if q_path.exists():
        s.questionnaire = json.loads(q_path.read_text(encoding="utf-8"))
    else:
        s.questionnaire = dict(_DEFAULT_ANSWERS_NONEMPTY)
    return s


@dataclass
class RunResult:
    label: str
    confidence: float
    suspected_node_ids: list[str]
    tokens_in: int
    tokens_out: int
    latency_ms_total: int
    delegation_rounds: int
    top_category: str
    top_summary: str
    elapsed_wall_s: float


async def _execute_single(
    *,
    label: str,
    scenario: ScenarioLoad,
    use_questionnaire: bool,
    rally_max_rounds: int,
) -> RunResult:
    topology = {
        "nodes": scenario.nodes,
        "links": [],
    }
    answers = dict(scenario.questionnaire) if use_questionnaire else {}
    log_text, normalized_nodes = _build_topology_log_text(
        topology, scenario.node_logs, scenario.node_configs,
        questionnaire_answers=answers,
    )
    topology_context = {"nodes": normalized_nodes, "links": []}
    wall_start = time.perf_counter()
    result: AnalysisResult = await run_rally_async(
        log_text,
        f"bench::{label}",
        rally_max_rounds=rally_max_rounds,
    )
    elapsed = time.perf_counter() - wall_start
    top = result.root_cause_candidates[0] if result.root_cause_candidates else None
    return RunResult(
        label=label,
        confidence=float(result.confidence),
        suspected_node_ids=list(result.suspected_node_ids),
        tokens_in=int(result.metrics.tokens_in),
        tokens_out=int(result.metrics.tokens_out),
        latency_ms_total=int(result.metrics.latency_ms_total),
        delegation_rounds=int(result.delegation_rounds),
        top_category=(top.category.value if top and hasattr(top.category, "value") else (top.category if top else "")),
        top_summary=(top.summary if top else ""),
        elapsed_wall_s=elapsed,
    )


async def run_matrix(
    scenario: ScenarioLoad,
    *,
    runs: int,
    rally_max_rounds: int,
) -> list[RunResult]:
    """questionnaire on / off の 2 × runs 回を直列実行 (LLM レート制限避け)。"""
    out: list[RunResult] = []
    for use_q in (True, False):
        for i in range(runs):
            label = f"q={'on' if use_q else 'off'}_r{i + 1}"
            print(f"[bench] running {label} ...", file=sys.stderr)
            res = await _execute_single(
                label=label,
                scenario=scenario,
                use_questionnaire=use_q,
                rally_max_rounds=rally_max_rounds,
            )
            out.append(res)
    return out


_CSV_COLUMNS = (
    "label",
    "questionnaire",
    "confidence",
    "suspected_node_ids",
    "delegation_rounds",
    "tokens_in",
    "tokens_out",
    "latency_ms_total",
    "elapsed_wall_s",
    "top_category",
    "top_summary",
)


def _row_for(res: RunResult) -> dict:
    return {
        "label": res.label,
        "questionnaire": "on" if res.label.startswith("q=on") else "off",
        "confidence": f"{res.confidence:.3f}",
        "suspected_node_ids": ";".join(res.suspected_node_ids),
        "delegation_rounds": res.delegation_rounds,
        "tokens_in": res.tokens_in,
        "tokens_out": res.tokens_out,
        "latency_ms_total": res.latency_ms_total,
        "elapsed_wall_s": f"{res.elapsed_wall_s:.2f}",
        "top_category": res.top_category,
        "top_summary": res.top_summary,
    }


def write_csv(results: list[RunResult], path: Path) -> None:
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=_CSV_COLUMNS)
        writer.writeheader()
        for r in results:
            writer.writerow(_row_for(r))


def print_markdown_table(results: list[RunResult]) -> None:
    print()
    print("| label | q | confidence | rounds | tokens (in/out) | latency (s) | suspected_node_ids | top_category | top_summary |")
    print("|---|---|---|---|---|---|---|---|---|")
    for r in results:
        q = "on" if r.label.startswith("q=on") else "off"
        print(
            f"| {r.label} | {q} | {r.confidence:.2f} | {r.delegation_rounds} | "
            f"{r.tokens_in:,}/{r.tokens_out:,} | {r.latency_ms_total / 1000:.1f} | "
            f"{', '.join(r.suspected_node_ids) or '-'} | {r.top_category} | "
            f"{r.top_summary[:60]}{'…' if len(r.top_summary) > 60 else ''} |"
        )
    # 比較サマリ
    on = [r for r in results if r.label.startswith("q=on")]
    off = [r for r in results if r.label.startswith("q=off")]
    if on and off:
        def avg(rs, attr):
            return sum(getattr(r, attr) for r in rs) / len(rs)
        print()
        print("**集計 (平均):**")
        print()
        print("| 指標 | questionnaire=on | questionnaire=off | 差分 (on - off) |")
        print("|---|---|---|---|")
        for attr, label, fmt in (
            ("confidence", "confidence", "{:.2f}"),
            ("delegation_rounds", "rounds", "{:.1f}"),
            ("tokens_in", "tokens_in", "{:.0f}"),
            ("tokens_out", "tokens_out", "{:.0f}"),
            ("latency_ms_total", "latency_ms_total", "{:.0f}"),
            ("elapsed_wall_s", "wall_s", "{:.2f}"),
        ):
            a, b = avg(on, attr), avg(off, attr)
            print(f"| {label} | {fmt.format(a)} | {fmt.format(b)} | {fmt.format(a - b)} |")


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description="問診票あり/なしの比較ベンチマーク (Phase F)")
    parser.add_argument("--scenario", required=True, type=Path,
                        help="シナリオディレクトリ (例: samples/topology/scenario2_api_acl_missing)")
    parser.add_argument("--runs", type=int, default=1,
                        help="各組み合わせを何回実行するか (default: 1)")
    parser.add_argument("--rally-max-rounds", type=int, default=3,
                        help="rally の上限ラウンド数 (default: 3)")
    parser.add_argument("--output", type=Path, default=None, help="CSV 出力先")
    args = parser.parse_args(argv)

    scenario = load_scenario(args.scenario)
    print(
        f"[bench] scenario={args.scenario} nodes={len(scenario.nodes)} "
        f"configs={sum(1 for v in scenario.node_configs.values() if v)} "
        f"logs={sum(1 for v in scenario.node_logs.values() if v)} runs={args.runs}",
        file=sys.stderr,
    )

    results = asyncio.run(run_matrix(
        scenario, runs=args.runs, rally_max_rounds=args.rally_max_rounds
    ))
    print_markdown_table(results)
    if args.output:
        write_csv(results, args.output)
        print(f"\n[bench] CSV written to {args.output}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
