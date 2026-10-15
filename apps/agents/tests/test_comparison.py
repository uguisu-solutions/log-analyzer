"""クラウドと SLM の精度差の比較 API（docs/plan/slm_support.md §9-6）のテスト。

同じ入力（指紋）の実行を、プロファイル（cloud / local:<タグ> / mixed）ごとに並べる。
1 つの入力をプロファイルごとに複数回流す前提なので、各セルは実行のリストになる。
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from log_analyzer import api as api_mod

_CLOUD = "claude-opus-4-7"
_SLM = "ollama:gemma4:26b-a4b-it-q4_K_M"


def _save(client, run_id: str, model: str, answers: dict, *, confidence=0.8, flags=None) -> int:
    payload = {
        "run_id": run_id,
        "config_id": "config4",
        "analysis_mode": "single",
        "single_source": "both",
        "questionnaire_answers": answers,
        "input_files": ["scenario1_fw.log"],
        "result": {
            "confidence": confidence,
            "root_cause_candidates": [{"category": "FW", "summary": f"{run_id} の結論", "evidence": []}],
            "recommended_actions": [],
            "metrics": {"tokens_in": 10, "tokens_out": 5, "latency_ms_total": 100},
            "info_loss_flags": flags or [],
            "execution_graph_nodes": [
                {"id": nid, "label": model, "role": nid, "model": model}
                for nid in ("orchestrator", "fw_monitor", "integrator")
            ],
        },
    }
    r = client.post("/api/analysis-history", json=payload)
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _setup(client) -> dict[str, int]:
    a = {"q1": "API が 502"}
    b = {"q1": "DNS が引けない"}
    return {
        "a_cloud_1": _save(client, "a-cloud-1", _CLOUD, a, confidence=0.9),
        "a_cloud_2": _save(client, "a-cloud-2", _CLOUD, a, confidence=0.7),
        "a_slm_1": _save(client, "a-slm-1", _SLM, a, confidence=0.4,
                         flags=["format_retry: fw (round 1)", "input_truncated: integrator"]),
        "b_cloud_1": _save(client, "b-cloud-1", _CLOUD, b),
    }


def test_comparison_groups_runs_by_input_and_profile():
    client = TestClient(api_mod.app)
    ids = _setup(client)

    data = client.get("/api/analysis-history/comparison").json()

    assert data["profiles"] == ["cloud", "local:gemma4:26b-a4b-it-q4_K_M"]  # cloud を先頭に
    assert len(data["rows"]) == 2
    row_a = next(r for r in data["rows"] if "a-slm-1" in str(r))
    assert row_a["input_files"] == ["scenario1_fw.log"]
    cloud_runs = row_a["cells"]["cloud"]
    assert [c["id"] for c in cloud_runs] == [ids["a_cloud_1"], ids["a_cloud_2"]]  # 実行順
    assert [c["confidence"] for c in cloud_runs] == [0.9, 0.7]
    slm_run = row_a["cells"]["local:gemma4:26b-a4b-it-q4_K_M"][0]
    assert slm_run["top_summary"] == "a-slm-1 の結論"
    assert slm_run["format_issues"] == {
        "format_retries": 1, "parse_fallbacks": 0, "routing_violations": 0, "input_truncated": 1,
    }
    row_b = next(r for r in data["rows"] if r is not row_a)
    assert list(row_b["cells"]) == ["cloud"]  # SLM を流していない入力には SLM の列が無い


def test_comparison_shows_latest_evaluation_score():
    """各実行の採点（evaluation_agent、Opus 固定）を並べる。再採点したら最新の点。未採点は None。"""
    from log_analyzer import storage

    client = TestClient(api_mod.app)
    ids = _setup(client)
    common = dict(scenario_key="M", axis_assessment=[], good_points=[], bad_points=[],
                  pitfalls_avoided=[], pitfalls_hit=[], summary="", model=_CLOUD)
    storage.insert_evaluation(analysis_history_id=ids["a_slm_1"], score=3, **common)
    storage.insert_evaluation(analysis_history_id=ids["a_slm_1"], score=4, **common)

    data = client.get("/api/analysis-history/comparison").json()

    row_a = next(r for r in data["rows"] if "a-slm-1" in str(r))
    assert row_a["cells"]["local:gemma4:26b-a4b-it-q4_K_M"][0]["eval_score"] == 4
    assert row_a["cells"]["cloud"][0]["eval_score"] is None


# ─── CSV 書き出し（手採点の Excel に貼る）──────────────────────────────


def test_comparison_csv_has_one_row_per_run_with_blank_manual_score():
    import csv
    import io

    client = TestClient(api_mod.app)
    _setup(client)

    r = client.get("/api/analysis-history/comparison.csv")

    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    # Excel で文字化けしないよう UTF-8 の BOM を付ける
    assert r.content.startswith("﻿".encode("utf-8"))
    rows = list(csv.DictReader(io.StringIO(r.content.decode("utf-8-sig"))))
    assert [row["run_id"] for row in rows] == ["a-cloud-1", "a-cloud-2", "a-slm-1", "b-cloud-1"]
    slm = rows[2]
    assert slm["model_profile"] == "local:gemma4:26b-a4b-it-q4_K_M"
    assert slm["input_files"] == "scenario1_fw.log"
    assert slm["top_summary"] == "a-slm-1 の結論"
    assert (slm["format_retries"], slm["input_truncated"]) == ("1", "1")
    assert slm["manual_score"] == ""  # 手採点を書き込む空欄
    assert rows[0]["input_fingerprint"] == rows[2]["input_fingerprint"] != rows[3]["input_fingerprint"]
