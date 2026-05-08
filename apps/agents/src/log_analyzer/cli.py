"""Command-line entry point: `log-analyze [--config configN] <path-to-log>`."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from dotenv import load_dotenv

from log_analyzer.baseline_agent import run_baseline
from log_analyzer.filtered_agent import run_filtered
from log_analyzer.multi_model_agent import run_multi_model
from log_analyzer.rally_agent import run_rally

CONFIG_RUNNERS = {
    "config1": run_baseline,
    "config2": run_filtered,
    "config3": run_multi_model,
    "config4": run_rally,
}


def main() -> int:
    load_dotenv()
    # Windows のデフォルト stdout encoding（CP932 等）だと日本語出力が文字化けするため UTF-8 に揃える
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Run a baseline configuration against a log file")
    parser.add_argument("log_path", type=Path, help="Path to a log file")
    parser.add_argument(
        "--config",
        choices=list(CONFIG_RUNNERS.keys()),
        default="config1",
        help="Configuration to run (default: config1)",
    )
    args = parser.parse_args()

    if not args.log_path.exists():
        sys.stderr.write(f"log file not found: {args.log_path}\n")
        return 2

    log_text = args.log_path.read_text(encoding="utf-8", errors="replace")
    runner = CONFIG_RUNNERS[args.config]
    result = runner(log_text, log_ref=str(args.log_path))
    sys.stdout.write(result.model_dump_json(indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
