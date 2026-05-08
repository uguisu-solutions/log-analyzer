r"""W9-W10 評価突合の予行演習: 複数ログ × 複数構成 を一括実行して差分を CSV にする。

使い方:
    cd apps\agents

    # 既定: builtin 4 構成を全ログに当てる
    python scripts\compare_configs.py ..\..\samples\logs\*.log

    # ユーザー定義構成も含める（SQLite に保存済の全 user 構成を自動的に追加）
    python scripts\compare_configs.py ..\..\samples\logs\*.log --include-user

    # 構成を明示指定（builtin id / "user:<id>" / "user:<name>" を混在可）
    python scripts\compare_configs.py log.log --configs config1 user:1 user:my-strict

    # CSV に保存
    python scripts\compare_configs.py log.log --include-user --csv out.csv

出力:
    - 標準出力に Markdown 表
    - --csv <path> 指定時は CSV を書き出し（W9 の機械突合に流用可）

各 user 構成は base_config + slot 別 overrides（prompt + model）として保存されている。
本スクリプトは [storage.get_saved_config](../src/log_analyzer/storage.py) で読み出し、
runner に prompt_overrides / model_overrides を渡して実行する。
"""
from __future__ import annotations

import argparse
import csv
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

from dotenv import load_dotenv

from log_analyzer import storage
from log_analyzer.baseline_agent import run_baseline
from log_analyzer.filtered_agent import run_filtered
from log_analyzer.multi_model_agent import run_multi_model
from log_analyzer.rally_agent import run_rally
from log_analyzer.schema import AnalysisResult

CONFIG_RUNNERS: dict[str, Callable] = {
    "config1": run_baseline,
    "config2": run_filtered,
    "config3": run_multi_model,
    "config4": run_rally,
}

COLUMNS = (
    "log_file",
    "log_bytes",
    "config",
    "base_config",
    "confidence",
    "top_category",
    "top_summary",
    "candidates",
    "actions",
    "tokens_in",
    "tokens_out",
    "latency_ms",
    "compression_ratio",
    "info_loss_flags",
)


@dataclass
class ResolvedConfig:
    spec: str  # CLI で指定された / 表示用 ID
    display_name: str  # 出力時の表示名（user 構成は "user:5(my-strict)" 等）
    base_config: str
    runner: Callable
    prompt_overrides: dict[str, str]
    model_overrides: dict[str, str]


def resolve_config(spec: str) -> ResolvedConfig:
    """``spec`` を ResolvedConfig に解決する。

    builtin: ``"config1"``..``"config4"``
    user (id): ``"user:5"``
    user (name): ``"user:my-strict"``
    """
    if spec in CONFIG_RUNNERS:
        return ResolvedConfig(
            spec=spec,
            display_name=spec,
            base_config=spec,
            runner=CONFIG_RUNNERS[spec],
            prompt_overrides={},
            model_overrides={},
        )
    if spec.startswith("user:"):
        ref = spec.split(":", 1)[1]
        saved = None
        # 数値なら ID として、それ以外は name として lookup
        try:
            saved = storage.get_saved_config(int(ref))
        except ValueError:
            for sc in storage.list_saved_configs():
                if sc["name"] == ref:
                    saved = sc
                    break
        if saved is None:
            raise ValueError(f"saved config not found: {spec}")
        base = saved["base_config"]
        if base not in CONFIG_RUNNERS:
            raise ValueError(f"saved config has unknown base: {base}")
        return ResolvedConfig(
            spec=spec,
            display_name=f"user:{saved['id']}({saved['name']})",
            base_config=base,
            runner=CONFIG_RUNNERS[base],
            prompt_overrides=dict(saved.get("overrides", {})),
            model_overrides=dict(saved.get("model_overrides", {})),
        )
    raise ValueError(f"unknown config spec: {spec}")


def _row(log_path: Path, rc: ResolvedConfig, result: AnalysisResult) -> dict[str, str]:
    top = result.root_cause_candidates[0] if result.root_cause_candidates else None
    return {
        "log_file": log_path.name,
        "log_bytes": str(log_path.stat().st_size),
        "config": rc.display_name,
        "base_config": rc.base_config,
        "confidence": f"{result.confidence:.2f}",
        "top_category": top.category.value if top else "-",
        "top_summary": (top.summary[:60] + "…") if top and len(top.summary) > 60 else (top.summary if top else "-"),
        "candidates": str(len(result.root_cause_candidates)),
        "actions": str(len(result.recommended_actions)),
        "tokens_in": str(result.metrics.tokens_in),
        "tokens_out": str(result.metrics.tokens_out),
        "latency_ms": str(result.metrics.latency_ms_total),
        "compression_ratio": f"{result.metrics.compression_ratio:.3f}",
        "info_loss_flags": "; ".join(result.info_loss_flags) if result.info_loss_flags else "-",
    }


def _print_markdown(rows: Iterable[dict[str, str]]) -> None:
    rows = list(rows)
    sys.stdout.write("| " + " | ".join(COLUMNS) + " |\n")
    sys.stdout.write("|" + "|".join(["---"] * len(COLUMNS)) + "|\n")
    for row in rows:
        sys.stdout.write("| " + " | ".join(row[c] for c in COLUMNS) + " |\n")


def _write_csv(rows: Iterable[dict[str, str]], path: Path) -> None:
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNS)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def _resolve_specs(specs: list[str]) -> list[ResolvedConfig]:
    resolved: list[ResolvedConfig] = []
    for s in specs:
        try:
            resolved.append(resolve_config(s))
        except ValueError as e:
            sys.stderr.write(f"skip: {e}\n")
    return resolved


def main() -> int:
    load_dotenv()
    storage.init_db()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(description="Run multiple configs across logs and emit a comparison table")
    parser.add_argument("logs", nargs="+", type=Path, help="Log files to evaluate")
    parser.add_argument(
        "--configs",
        nargs="+",
        default=None,
        help='Configs to run. Accepts builtin IDs ("config1".."config4") and '
        '"user:<id>" / "user:<name>". Default: builtin 4 configs.',
    )
    parser.add_argument(
        "--include-user",
        action="store_true",
        help="Also run all saved user configs (in addition to --configs / default)",
    )
    parser.add_argument("--csv", type=Path, help="Write result rows to a CSV file")
    args = parser.parse_args()

    base_specs = args.configs if args.configs else list(CONFIG_RUNNERS.keys())
    targets = _resolve_specs(base_specs)
    if args.include_user:
        existing_ids = {rc.spec for rc in targets}
        for sc in storage.list_saved_configs():
            spec = f"user:{sc['id']}"
            if spec in existing_ids:
                continue
            try:
                targets.append(resolve_config(spec))
            except ValueError as e:
                sys.stderr.write(f"skip: {e}\n")

    if not targets:
        sys.stderr.write("error: no configs resolved (check --configs and saved configs)\n")
        return 2

    sys.stderr.write(f"resolved {len(targets)} target(s): {', '.join(rc.display_name for rc in targets)}\n")

    rows: list[dict[str, str]] = []
    for log_path in args.logs:
        if not log_path.exists():
            sys.stderr.write(f"skip: {log_path} not found\n")
            continue
        log_text = log_path.read_text(encoding="utf-8", errors="replace")
        for rc in targets:
            sys.stderr.write(f"running {rc.display_name} on {log_path.name}...\n")
            result = rc.runner(
                log_text,
                log_ref=str(log_path),
                prompt_overrides=rc.prompt_overrides,
                model_overrides=rc.model_overrides,
            )
            rows.append(_row(log_path, rc, result))

    _print_markdown(rows)
    if args.csv:
        _write_csv(rows, args.csv)
        sys.stderr.write(f"wrote {args.csv}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
