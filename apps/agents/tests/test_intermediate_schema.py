"""構成4 の中間出力モデル（docs/plan/slm_support.md §6-1）のテスト。

中間出力（orchestrator / 監視 / integrator）は、これまでプロンプト内の JSON 例としてしか
存在しなかった。Pydantic モデルを足すにあたり、**プロンプトの出力例とモデルが食い違わない**
ことをここで固定する（CLAUDE.md「プロンプトの出力形式と schema は対。片方だけ変えない」）。
"""
from __future__ import annotations

import json
import re

import pytest

from log_analyzer import schema
from log_analyzer.rally.integrator import INTEGRATOR_PROMPT
from log_analyzer.rally.monitors import (
    DEFAULT_MONITOR_PROMPTS,
    FW_PROMPT,
    _normalize_monitor_output,
)
from log_analyzer.rally.orchestrator import ORCHESTRATOR_PROMPT, _normalize_decision


def _json_examples(prompt: str) -> list[dict]:
    """プロンプト中の「行頭 { 〜 行頭 }」の JSON 例をすべて取り出す。"""
    return [json.loads(m) for m in re.findall(r"^\{\n.*?^\}$", prompt, re.MULTILINE | re.DOTALL)]


def _fields(model) -> set[str]:
    return set(model.model_fields)


# ─── orchestrator ───────────────────────────────────────────────────


def test_orchestrator_prompt_example_matches_model():
    (example,) = _json_examples(ORCHESTRATOR_PROMPT)
    assert set(example) == _fields(schema.OrchestratorDecision)
    schema.OrchestratorDecision.model_validate(example)


# ─── 監視 ──────────────────────────────────────────────────────────


def test_monitor_prompt_example_matches_model():
    """5 監視は出力形式を共有する（_NEXT_NODE_GUIDANCE）。FW で代表して確かめる。"""
    examples = _json_examples(FW_PROMPT)
    assert len(examples) == 1
    (example,) = examples
    assert set(example) == _fields(schema.MonitorOutput)
    assert set(example["findings"][0]) == _fields(schema.MonitorFinding)
    schema.MonitorOutput.model_validate(example)


# ─── integrator ────────────────────────────────────────────────────


def test_integrator_prompt_examples_match_model():
    """通常の出力例と、トポロジー時の出力例（suspected_nodes 付き）の 2 つがある。"""
    template, topology = _json_examples(INTEGRATOR_PROMPT)

    # 通常時は suspected_nodes を出さない
    assert set(template) == _fields(schema.IntegratorOutput) - {"suspected_nodes"}
    assert set(template["root_cause_candidates"][0]) == _fields(schema.RootCauseCandidate)
    assert set(template["recommended_actions"][0]) == _fields(schema.RecommendedAction)

    assert set(topology) == _fields(schema.IntegratorOutput)
    assert set(topology["suspected_nodes"][0]) == _fields(schema.SuspectedNodeFinding)
    out = schema.IntegratorOutput.model_validate(topology)
    assert out.suspected_nodes[0].node_id == "fw-01"


@pytest.mark.parametrize("slot_id", sorted(DEFAULT_MONITOR_PROMPTS))
def test_all_monitor_prompts_share_the_same_output_example(slot_id):
    assert _json_examples(DEFAULT_MONITOR_PROMPTS[slot_id]) == _json_examples(FW_PROMPT)


# ─── 正規化処理の出力もモデルに収まる ──────────────────────────────


@pytest.mark.parametrize(
    "raw",
    [
        {"first_node": "dns", "focus_hint": "h", "rationale": "r"},
        {},  # パース失敗時のフォールバック
        {"first_node": "unknown"},
    ],
)
def test_normalized_decision_conforms_to_model(raw):
    schema.OrchestratorDecision.model_validate(_normalize_decision(raw))


@pytest.mark.parametrize(
    "raw",
    [
        {"findings": [{"category": "FW", "summary": "s", "evidence": ["e"]}],
         "confidence": 0.8, "next": "routing", "focus_hint_for_next": "f", "rationale": "r"},
        {"next": "fw"},  # 自己遷移 → integrator にフォールバック
        {},  # パース失敗時のフォールバック
    ],
)
def test_normalized_monitor_output_conforms_to_model(raw):
    normalized, _ = _normalize_monitor_output(raw, role="fw", previous_node=None)
    schema.MonitorOutput.model_validate(normalized)
