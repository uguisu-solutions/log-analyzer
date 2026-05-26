"""Phase F: benchmark_questionnaire.py のスモークテスト。

実 LLM 呼び出しは行わず、scenario ロード + CSV 出力 + Markdown 整形のみ検証。
"""
from __future__ import annotations

import csv
import sys
from pathlib import Path

# scripts/ を import path に追加
_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(_SCRIPTS))

import benchmark_questionnaire as bq  # noqa: E402


def test_infer_type_known_prefixes():
    assert bq._infer_type("fw-01") == "FW"
    assert bq._infer_type("lb-01") == "LB"
    assert bq._infer_type("web-01") == "Server"
    assert bq._infer_type("api-01") == "Server"
    assert bq._infer_type("db-01") == "DB"
    assert bq._infer_type("core-sw-01") == "L3SW"
    assert bq._infer_type("unknown-01") == ""


def test_load_scenario_from_existing_dir():
    """既存の samples/topology/scenario2_api_acl_missing を読み込めること。"""
    repo_root = Path(__file__).resolve().parents[3]
    scenario_dir = repo_root / "samples" / "topology" / "scenario2_api_acl_missing"
    s = bq.load_scenario(scenario_dir)
    node_ids = {n["id"] for n in s.nodes}
    assert {"fw-01", "lb-01", "web-01", "api-01"}.issubset(node_ids)
    assert "fw-01" in s.node_configs
    assert "fw-01" in s.node_logs
    # FW config の本文に既知の文言が含まれる
    assert "lb-to-app-01" in s.node_configs["fw-01"][0]["content"] \
        or "api-backends" in s.node_configs["fw-01"][0]["content"]
    # デフォルト問診票
    assert "symptom_onset" in s.questionnaire
    assert s.questionnaire["symptom_onset"]


def test_load_scenario_custom_questionnaire(tmp_path):
    (tmp_path / "fw-01.conf").write_text("acl xxx", encoding="utf-8")
    (tmp_path / "fw-01.log").write_text("deny\n", encoding="utf-8")
    custom = {"symptom_onset": "テスト用", "scope": "テスト"}
    (tmp_path / "questionnaire.json").write_text(__import__("json").dumps(custom), encoding="utf-8")
    s = bq.load_scenario(tmp_path)
    assert s.questionnaire == custom


def test_write_csv_and_round_trip(tmp_path):
    results = [
        bq.RunResult(
            label="q=on_r1", confidence=0.85, suspected_node_ids=["fw-01", "lb-01"],
            tokens_in=1000, tokens_out=300, latency_ms_total=5000,
            delegation_rounds=2, top_category="FW", top_summary="x", elapsed_wall_s=4.9,
        ),
        bq.RunResult(
            label="q=off_r1", confidence=0.70, suspected_node_ids=["fw-01"],
            tokens_in=900, tokens_out=280, latency_ms_total=4800,
            delegation_rounds=2, top_category="FW", top_summary="y", elapsed_wall_s=4.7,
        ),
    ]
    out_csv = tmp_path / "bench.csv"
    bq.write_csv(results, out_csv)
    assert out_csv.exists()
    with out_csv.open(encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    assert len(rows) == 2
    assert rows[0]["label"] == "q=on_r1"
    assert rows[0]["questionnaire"] == "on"
    assert rows[1]["questionnaire"] == "off"
    assert rows[0]["suspected_node_ids"] == "fw-01;lb-01"


def test_print_markdown_does_not_crash(capsys):
    results = [
        bq.RunResult(label="q=on_r1", confidence=0.85, suspected_node_ids=["fw-01"],
                     tokens_in=1000, tokens_out=300, latency_ms_total=5000,
                     delegation_rounds=2, top_category="FW", top_summary="x", elapsed_wall_s=5.0),
        bq.RunResult(label="q=off_r1", confidence=0.70, suspected_node_ids=[],
                     tokens_in=800, tokens_out=200, latency_ms_total=4000,
                     delegation_rounds=1, top_category="Unknown", top_summary="", elapsed_wall_s=4.0),
    ]
    bq.print_markdown_table(results)
    captured = capsys.readouterr()
    assert "questionnaire=on" in captured.out
    assert "q=on_r1" in captured.out
