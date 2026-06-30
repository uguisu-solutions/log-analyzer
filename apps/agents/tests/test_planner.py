"""解析方針プランナー (rally/planner.py) の堅牢性テスト。"""
from __future__ import annotations

import json
from types import SimpleNamespace

from log_analyzer.rally import planner as planner_mod


class _FakeMessages:
    def __init__(self, texts):
        self._texts = list(texts)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        text = self._texts.pop(0)
        return SimpleNamespace(
            stop_reason="end_turn",
            content=[SimpleNamespace(type="text", text=text)],
            usage=SimpleNamespace(input_tokens=100, output_tokens=50),
        )


class _FakeAnthropic:
    def __init__(self, texts):
        self.messages = _FakeMessages(texts)


_VALID = json.dumps({
    "situation_summary": "s",
    "primary_hypotheses": ["h1"],
    "investigation_plan": ["p1"],
    "suggested_first_node": "app",
    "focus": "f",
    "data_to_use": ["d"],
    "missing_data_notes": "",
}, ensure_ascii=False)


def test_plan_policy_parses_valid_json(monkeypatch):
    fake = _FakeAnthropic([_VALID])
    monkeypatch.setattr(planner_mod.anthropic, "Anthropic", lambda: fake)
    out = planner_mod.plan_policy("log")
    assert out["parse_error"] is None
    assert out["suggested_first_node"] == "app"
    assert len(fake.messages.calls) == 1  # リトライ不要


def test_plan_policy_retries_on_truncated_json(monkeypatch):
    # 1 回目は途中で切れた JSON (パース不可) → 2 回目で復旧
    truncated = '{"situation_summary": "途中で切れ'
    fake = _FakeAnthropic([truncated, _VALID])
    monkeypatch.setattr(planner_mod.anthropic, "Anthropic", lambda: fake)
    out = planner_mod.plan_policy("log")
    assert out["parse_error"] is None          # リトライで解消
    assert out["suggested_first_node"] == "app"
    assert len(fake.messages.calls) == 2       # リトライした
    # トークンは 2 回分合算
    assert out["tokens_in"] == 200
    assert out["tokens_out"] == 100


def test_plan_policy_falls_back_when_both_fail(monkeypatch):
    fake = _FakeAnthropic(["not json at all", "still not json"])
    monkeypatch.setattr(planner_mod.anthropic, "Anthropic", lambda: fake)
    out = planner_mod.plan_policy("log")
    assert out["parse_error"] is not None       # 2 回とも失敗 → フォールバック
    assert out["suggested_first_node"] == "fw"   # 既定方針


def test_planner_max_tokens_env_override(monkeypatch):
    monkeypatch.setenv("RALLY_PLANNER_MAX_TOKENS", "9000")
    assert planner_mod._planner_max_tokens() == 9000
    monkeypatch.setenv("RALLY_PLANNER_MAX_TOKENS", "bad")
    assert planner_mod._planner_max_tokens() == planner_mod._DEFAULT_PLANNER_MAX_TOKENS
