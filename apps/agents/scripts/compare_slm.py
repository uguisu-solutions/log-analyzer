r"""クラウド LLM と SLM の一括比較実行（docs/plan/slm_support.md §9-6）。

同じシナリオを、指定した構成（``config4`` / 保存済みの ``user:N``）ごとに N 回ずつ流し、
解析履歴に保存する。保存した履歴は比較 API（``/api/analysis-history/comparison``）と
CSV（``/api/analysis-history/comparison.csv``）に、同じ入力の行として並ぶ。

起動中のバックエンドに、構成・ログ解析の画面と同じリクエストを HTTP で送る
（ログの組み立て・解析の流れ・保存先が画面からの実行と一致する）。
**実 LLM を呼ぶため課金が発生する。実行は人が行う。**

使い方:
    cd apps\agents
    .\.venv\Scripts\Activate.ps1

    # 実行計画だけ確認（LLM は呼ばない）
    python scripts\compare_slm.py --scenario ..\..\samples\topology\scenario2_api_acl_missing ^
        --configs config4,user:3 --runs 3 --dry-run

    # 実行（バックエンドを先に起動しておく）
    python scripts\compare_slm.py --scenario ..\..\samples\topology\scenario2_api_acl_missing ^
        --configs config4,user:3 --runs 3
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import Protocol

from benchmark_questionnaire import ScenarioLoad, load_scenario

__all__ = ["HttpBackend", "build_run_request", "load_scenario", "main", "plan", "run_one"]


class Backend(Protocol):
    """起動中のバックエンドとの送受信（テストでは偽物に差し替える）。"""

    def stream(self, path: str, body: dict) -> Iterator[tuple[str, dict]]: ...

    def post(self, path: str, body: dict) -> dict: ...


def plan(configs: list[str], runs: int) -> list[tuple[str, int]]:
    """``(構成, 何回目)`` の実行順。構成ごとにまとめる（Pod でのモデル入れ替えを最小にする）。"""
    return [(config, i) for config in configs for i in range(1, runs + 1)]


def build_run_request(scenario: ScenarioLoad, *, config: str, rally_max_rounds: int) -> dict:
    """``POST /api/runs/config-log-stream`` の本体。構成・ログ解析の画面が送る項目に揃える。

    1 段階・設定とログの両方（画面の既定）、方針の事前確認なし（画面の「まとめて実行」と同じ）、
    監査なし。比較の条件をクラウドと SLM で揃えるため、ここは構成以外を変えない。
    """
    return {
        "config": config,
        "rally_max_rounds": rally_max_rounds,
        "topology": {"nodes": list(scenario.nodes), "links": []},
        "node_logs": scenario.node_logs,
        "node_configs": scenario.node_configs,
        "node_bigquery": {},
        "analysis_mode": "single",
        "single_source": "both",
        "stage_order": "config_log",
        "questionnaire_answers": scenario.questionnaire,
        "questionnaire_confidences": {},
        "audit_after_integrator": False,
        "require_policy_approval": False,
    }


class HttpBackend:
    """起動中のバックエンド（uvicorn）に HTTP で送る。標準ライブラリだけで書く。"""

    # SLM の 1 解析は数分かかるため、1 回の読み取り待ちは長めに取る
    _TIMEOUT_SEC = 1800

    def __init__(self, base_url: str) -> None:
        self._base = base_url.rstrip("/")

    def _request(self, path: str, body: dict) -> urllib.request.Request:
        return urllib.request.Request(
            self._base + path,
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )

    def _open(self, path: str, body: dict):
        try:
            return urllib.request.urlopen(self._request(path, body), timeout=self._TIMEOUT_SEC)
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"バックエンドがエラーを返した（HTTP {e.code} {path}）: {detail}") from e

    def stream(self, path: str, body: dict) -> Iterator[tuple[str, dict]]:
        """SSE（``event:`` 行と ``data:`` 行、空行区切り）を ``(kind, data)`` で返す。"""
        with self._open(path, body) as resp:
            kind, data = "", ""
            for raw in resp:
                line = raw.decode("utf-8").rstrip("\r\n")
                if line.startswith("event:"):
                    kind = line[len("event:"):].strip()
                elif line.startswith("data:"):
                    data += line[len("data:"):].strip()
                elif not line and kind:
                    yield kind, json.loads(data or "{}")
                    kind, data = "", ""

    def post(self, path: str, body: dict) -> dict:
        with self._open(path, body) as resp:
            return json.loads(resp.read().decode("utf-8"))


def _input_file_names(scenario: ScenarioLoad) -> list[str]:
    """画面（collectInputFileNames）と同じく、ログ → 設定の順にファイル名だけを集める。"""
    names: list[str] = []
    for bucket in (scenario.node_logs, scenario.node_configs):
        for attachments in bucket.values():
            for a in attachments:
                if a["name"] not in names:
                    names.append(a["name"])
    return names


def run_one(backend: Backend, scenario: ScenarioLoad, *, config: str, rally_max_rounds: int) -> str:
    """1 回解析し、画面と同じ形で解析履歴に保存して ``run_id`` を返す。

    ラウンド上限の確認には「停止（その時点で統合）」を返す（CLI と同じ扱い。どの構成でも同じ条件）。
    途中でエラーになった回は保存せず ``RuntimeError`` を上げる。
    """
    body = build_run_request(scenario, config=config, rally_max_rounds=rally_max_rounds)
    run_id = ""
    result: dict | None = None
    for kind, data in backend.stream("/api/runs/config-log-stream", body):
        if kind == "run_id_assigned":
            run_id = str(data.get("run_id") or "")
        elif kind == "await_confirmation":
            backend.post(f"/api/runs/{run_id}/decision", {"action": "stop"})
        elif kind == "error":
            raise RuntimeError(f"解析が失敗した（{data.get('stage')}）: {data.get('message')}")
        elif kind == "final":
            result = data.get("result")
    if not run_id or result is None:
        raise RuntimeError("解析結果（final）を受け取れなかった")
    backend.post("/api/analysis-history", {
        "run_id": run_id,
        "kind": "config-log",
        "config_id": config,
        "analysis_mode": body["analysis_mode"],
        "single_source": body["single_source"],
        "stage_order": body["stage_order"],
        "rally_max_rounds": rally_max_rounds,
        "questionnaire_answers": body["questionnaire_answers"],
        "questionnaire_confidences": body["questionnaire_confidences"],
        "topology": body["topology"],
        "result": result,
        "input_files": _input_file_names(scenario),
        "node_bigquery": body["node_bigquery"],
    })
    return run_id


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="クラウド LLM と SLM の一括比較実行（課金が発生する）")
    parser.add_argument("--scenario", required=True, type=Path,
                        help="シナリオディレクトリ（<node>.log / <node>.conf / questionnaire.json）")
    parser.add_argument("--configs", default="config4",
                        help="カンマ区切りの構成（config4 / 保存済みの user:N）。既定 config4")
    parser.add_argument("--runs", type=int, default=3,
                        help="構成ごとの実行回数（結果の揺れを見るため既定 3）")
    parser.add_argument("--rally-max-rounds", type=int, default=3, help="委譲チェーンの上限（既定 3）")
    parser.add_argument("--api", default="http://localhost:8000", help="バックエンドの URL")
    parser.add_argument("--dry-run", action="store_true", help="実行計画だけ表示（LLM を呼ばない）")
    args = parser.parse_args(argv)

    configs = [c.strip() for c in args.configs.split(",") if c.strip()]
    scenario = load_scenario(args.scenario)
    runs = plan(configs, args.runs)
    print(f"シナリオ: {args.scenario}（ノード {len(scenario.nodes)} 個）")
    print(f"実行計画: {len(runs)} 回")
    for config, i in runs:
        print(f"  - {config} の {i} 回目")
    if args.dry_run:
        return 0

    backend = HttpBackend(args.api)
    failures: list[str] = []
    for n, (config, i) in enumerate(runs, start=1):
        started = time.perf_counter()
        print(f"[{n}/{len(runs)}] {config} の {i} 回目 ...", flush=True)
        try:
            run_id = run_one(backend, scenario, config=config, rally_max_rounds=args.rally_max_rounds)
        except Exception as e:  # noqa: BLE001 — 1 回の失敗で残りを止めない
            failures.append(f"{config} の {i} 回目: {e}")
            print(f"    失敗: {e}", file=sys.stderr, flush=True)
            continue
        print(f"    完了 run_id={run_id}（{time.perf_counter() - started:.0f} 秒）", flush=True)

    print(f"\n完了 {len(runs) - len(failures)} 件 / 失敗 {len(failures)} 件")
    for f in failures:
        print(f"  - {f}")
    print(f"比較: {args.api}/api/analysis-history/comparison.csv")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
