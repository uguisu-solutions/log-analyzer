"""実行のモデル構成と入力の指紋（docs/plan/slm_support.md §9-3 / §9-6）のテスト。

クラウドと SLM の精度差を観測するため、各実行が「実際にどのモデルで動いたか」と
「モデル以外の条件が同じ実行か」を履歴から機械的に判別できるようにする。
"""
from __future__ import annotations

from log_analyzer import run_profile


def test_models_used_reads_actual_models_from_execution_graph():
    """リクエストの指定ではなく、結果に残った実績値（実行グラフの各ノードのモデル）を使う。"""
    result = {
        "execution_graph_nodes": [
            {"id": "orchestrator", "role": "orchestrator", "model": "claude-opus-4-7"},
            {"id": "fw_monitor", "role": "monitor", "model": "ollama:gemma4:26b-a4b-it-q4_K_M"},
            {"id": "input", "role": "input", "model": None},  # モデルを持たないノードは除く
            {"id": "integrator", "role": "integrator", "model": "claude-opus-4-7"},
        ]
    }

    assert run_profile.models_used(result) == {
        "orchestrator": "claude-opus-4-7",
        "fw_monitor": "ollama:gemma4:26b-a4b-it-q4_K_M",
        "integrator": "claude-opus-4-7",
    }


def test_models_used_falls_back_to_round_metrics_for_two_stage_results():
    """2 段階解析の結果は実行グラフが空で、両 Stage の round_metrics が連結されて残る。
    キーは実行グラフと同じ単位（監視は ``<役割>_monitor``）に揃える。"""
    result = {
        "execution_graph_nodes": [],
        "round_metrics": [
            {"round": 0, "role": "orchestrator", "model": "claude-opus-4-7"},
            {"round": 1, "role": "fw", "model": "ollama:gemma4:31b-it-q4_K_M"},
            {"round": 2, "role": "fw", "model": "ollama:gemma4:31b-it-q4_K_M"},
            {"round": 3, "role": "integrator", "model": "claude-opus-4-7"},
        ],
    }

    assert run_profile.models_used(result) == {
        "orchestrator": "claude-opus-4-7",
        "fw_monitor": "ollama:gemma4:31b-it-q4_K_M",
        "integrator": "claude-opus-4-7",
    }


# ─── プロファイル名（実績のモデル構成から機械的に決める）──────────────


def test_profile_cloud_when_no_slm_is_used():
    assert run_profile.profile_of({"orchestrator": "claude-opus-4-7", "fw_monitor": "claude-opus-4-7"}) == "cloud"


def test_profile_local_when_every_node_uses_the_same_slm():
    models = {"orchestrator": "ollama:gemma4:26b-a4b-it-q4_K_M", "integrator": "ollama:gemma4:26b-a4b-it-q4_K_M"}
    assert run_profile.profile_of(models) == "local:gemma4:26b-a4b-it-q4_K_M"


def test_profile_mixed_when_cloud_and_slm_or_several_slms_are_combined():
    assert run_profile.profile_of({"orchestrator": "claude-opus-4-7", "fw_monitor": "ollama:a"}) == "mixed"
    assert run_profile.profile_of({"orchestrator": "ollama:a", "fw_monitor": "ollama:b"}) == "mixed"


def test_profile_empty_when_no_model_is_recorded():
    """失敗した実行など、モデルの実績が残っていない場合は判別しない。"""
    assert run_profile.profile_of({}) == ""


# ─── 入力の指紋（モデル以外の条件が同じ実行を束ねる）──────────────────


_REQUEST = {
    "config_id": "config4",
    "analysis_mode": "two-stage",
    "single_source": None,
    "stage_order": "config-first",
    "rally_max_rounds": 3,
    "view_mode": "detail",
    "questionnaire_answers": {"q1": "API が 502", "q2": "FW 変更あり"},
    "questionnaire_confidences": {"q1": "高"},
    "topology": {"nodes": [{"id": "fw-01"}]},
    "input_files": ["scenario1_fw.log"],
    "node_bigquery": {},
}


def test_fingerprint_is_stable_regardless_of_key_order():
    reordered = dict(reversed(list(_REQUEST.items())))
    assert run_profile.input_fingerprint(reordered) == run_profile.input_fingerprint(_REQUEST)


def test_fingerprint_ignores_display_only_fields():
    """view_mode は画面の表示切替で、解析の条件ではない。"""
    other_view = {**_REQUEST, "view_mode": "summary"}
    assert run_profile.input_fingerprint(other_view) == run_profile.input_fingerprint(_REQUEST)


def test_fingerprint_changes_when_any_analysis_condition_changes():
    base = run_profile.input_fingerprint(_REQUEST)
    variants = [
        {**_REQUEST, "questionnaire_answers": {"q1": "API が 503", "q2": "FW 変更あり"}},
        {**_REQUEST, "input_files": ["scenario2_dns.log"]},
        {**_REQUEST, "rally_max_rounds": 5},
        {**_REQUEST, "stage_order": "log-first"},
    ]
    assert all(run_profile.input_fingerprint(v) != base for v in variants)
