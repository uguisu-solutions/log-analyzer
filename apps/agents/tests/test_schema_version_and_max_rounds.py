"""確認事項 D-1 / D-3 のテスト。

D-1: config-log 解析で結果を組み直すときに delegation_max_rounds が落ち、
     UI が「N ラウンド / 上限 0」と表示していた不具合の修正。
D-3: Langfuse の trace metadata に "v0.1" がリテラルで残っていた更新漏れの修正。
"""
from __future__ import annotations

import re
from pathlib import Path

from log_analyzer.schema import SCHEMA_VERSION, AnalysisResult, ConfigId, StageOutput

_SRC = Path(__file__).resolve().parents[1] / "src" / "log_analyzer"


# ─── D-3: schema_version ────────────────────────────────────────────


def test_analysis_result_uses_schema_version_constant():
    ar = AnalysisResult(
        config_id=ConfigId.CONFIG4, input_log_ref="inline",
        root_cause_candidates=[], recommended_actions=[], confidence=0.5,
    )
    assert ar.schema_version == SCHEMA_VERSION == "v0.2"


def test_no_hardcoded_schema_version_literals_in_sources():
    """全構成の trace metadata が定数を使い、"v0.1" のリテラルが残っていないこと。

    v0.1 → v0.2 に上げた際、5 構成すべての metadata が更新漏れしていた
    (確認事項 D-3)。リテラルを禁止して再発を防ぐ。
    """
    offenders: list[str] = []
    for path in _SRC.rglob("*.py"):
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if re.search(r'"schema_version"\s*:\s*"v', line):
                offenders.append(f"{path.name}:{i}: {line.strip()}")
    assert offenders == [], "trace metadata に版数がリテラルで書かれている: " + "; ".join(offenders)


def test_all_configs_send_schema_version_constant():
    """5 構成すべてが metadata に SCHEMA_VERSION を渡していること。"""
    for name in (
        "baseline_agent.py", "filtered_agent.py", "multi_model_agent.py",
        "pipeline_runner.py", "rally_agent.py",
    ):
        src = (_SRC / name).read_text(encoding="utf-8")
        assert '"schema_version": SCHEMA_VERSION' in src, f"{name} が定数を使っていない"


# ─── D-1: delegation_max_rounds ─────────────────────────────────────


def _stage(max_rounds: int) -> StageOutput:
    return StageOutput(
        stage="log", stage_label="Stage 1", confidence=0.8,
        delegation_rounds=2, delegation_max_rounds=max_rounds,
    )


def test_stage_output_carries_max_rounds():
    from log_analyzer.rally_two_stage import _result_to_stage_output

    ar = AnalysisResult(
        config_id=ConfigId.CONFIG4, input_log_ref="inline",
        root_cause_candidates=[], recommended_actions=[], confidence=0.8,
        delegation_rounds=2, delegation_max_rounds=3,
    )
    stage = _result_to_stage_output("log", ar)
    assert stage.delegation_max_rounds == 3


def test_final_result_keeps_max_rounds_single_stage():
    """1 段階モード: 組み直した最終結果に上限が残ること (従来は 0 になっていた)。"""
    from log_analyzer.rally_two_stage import _build_final_result

    final = _build_final_result(stage_outputs=[_stage(3)], trace_id="t", log_ref="inline")
    assert final.delegation_max_rounds == 3


def test_final_result_keeps_max_rounds_two_stage():
    """2 段階モード: 主 Stage (最新) の上限が残ること。延長後の値も反映される。"""
    from log_analyzer.rally_two_stage import _build_final_result

    final = _build_final_result(
        stage_outputs=[_stage(3), _stage(5)], trace_id="t", log_ref="inline"
    )
    assert final.delegation_max_rounds == 5


def test_old_result_without_max_rounds_still_parses():
    """対応前に保存された履歴 (フィールド無し) もそのまま読めること。"""
    old = {
        "trace_id": "t-old", "config_id": "config4", "input_log_ref": "inline",
        "root_cause_candidates": [], "recommended_actions": [], "confidence": 0.5,
        "stage_outputs": [{"stage": "log", "confidence": 0.5}],
    }
    ar = AnalysisResult.model_validate(old)
    assert ar.delegation_max_rounds == 0
    assert ar.stage_outputs[0].delegation_max_rounds == 0
