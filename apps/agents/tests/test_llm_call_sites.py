"""構成1・2・3・5 の LLM 呼び出しの特性テスト。

llm_client への集約（docs/plan/slm_support.md §10 Phase 1）の前後で、
SDK に渡るリクエストと結果の読み取りが変わらないことを固定する。
SDK のクライアント生成を差し替え、実 API は叩かない。
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

from log_analyzer import llm_client

_RESULT_JSON = json.dumps(
    {"root_cause_candidates": [], "recommended_actions": [], "confidence": 0.5}
)


class _FakeMessages:
    def __init__(self, texts, tin=11, tout=7):
        self._texts = list(texts)
        self._tin, self._tout = tin, tout
        self.calls: list[dict] = []

    def _next(self, kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            stop_reason="end_turn",
            content=[SimpleNamespace(type="text", text=self._texts.pop(0))],
            usage=SimpleNamespace(input_tokens=self._tin, output_tokens=self._tout),
        )

    def create(self, **kwargs):
        return self._next(kwargs)


class _AsyncFakeMessages(_FakeMessages):
    async def create(self, **kwargs):
        return self._next(kwargs)


def _patch_langfuse(monkeypatch, module):
    monkeypatch.setattr(module, "get_client", lambda: MagicMock())
    monkeypatch.setattr(module, "flush", lambda: None)


# ─── 構成1 ─────────────────────────────────────────────────────────


def test_baseline_request_and_result(monkeypatch):
    from log_analyzer import baseline_agent

    _patch_langfuse(monkeypatch, baseline_agent)
    msgs = _FakeMessages([_RESULT_JSON])
    monkeypatch.setattr(
        llm_client.anthropic, "Anthropic", lambda: SimpleNamespace(messages=msgs)
    )

    result = baseline_agent.run_baseline(
        "ログ本文", model_overrides={"analyze": "claude-opus-4-7"}
    )

    assert msgs.calls == [
        {
            "model": "claude-opus-4-7",
            "max_tokens": 2000,
            "system": baseline_agent.SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": "ログ本文"}],
        }
    ]
    assert result.confidence == 0.5
    assert (result.metrics.tokens_in, result.metrics.tokens_out) == (11, 7)


# ─── 構成2 ─────────────────────────────────────────────────────────


def test_filtered_two_calls_and_triage_is_stripped(monkeypatch):
    from log_analyzer import filtered_agent

    _patch_langfuse(monkeypatch, filtered_agent)
    msgs = _FakeMessages(["  トリアージ結果  \n", _RESULT_JSON])
    monkeypatch.setattr(
        llm_client.anthropic, "Anthropic", lambda: SimpleNamespace(messages=msgs)
    )

    result = filtered_agent.run_filtered("2026-01-01 ERROR deny dst=10.0.0.1")

    assert [c["max_tokens"] for c in msgs.calls] == [800, 2000]
    assert msgs.calls[0]["system"] == filtered_agent.HAIKU_TRIAGE_PROMPT
    assert msgs.calls[1]["system"] == filtered_agent.SYSTEM_PROMPT
    # トリアージ結果は前後空白を落として 2 段目の入力に入る
    assert "トリアージ結果" in msgs.calls[1]["messages"][0]["content"]
    assert "  トリアージ結果  " not in msgs.calls[1]["messages"][0]["content"]
    assert (result.metrics.tokens_in, result.metrics.tokens_out) == (22, 14)


# ─── 構成3 ─────────────────────────────────────────────────────────


def test_multi_model_anthropic_and_openai_calls(monkeypatch):
    from log_analyzer import multi_model_agent as mm

    msgs = _AsyncFakeMessages([_RESULT_JSON, _RESULT_JSON])
    monkeypatch.setattr(
        llm_client.anthropic, "AsyncAnthropic", lambda: SimpleNamespace(messages=msgs)
    )
    responses_calls: list[dict] = []

    async def _responses_create(**kwargs):
        responses_calls.append(kwargs)
        return SimpleNamespace(
            output_text=_RESULT_JSON, usage=SimpleNamespace(input_tokens=5, output_tokens=3)
        )

    monkeypatch.setattr(
        llm_client.openai,
        "AsyncOpenAI",
        lambda: SimpleNamespace(responses=SimpleNamespace(create=_responses_create)),
    )

    a = asyncio.run(mm._analyze_with_anthropic("ログ", "claude-opus-4-7", "sonnet", "SYS"))
    o = asyncio.run(mm._analyze_with_openai("ログ", "gpt-5.5", "openai", "SYS"))
    i = asyncio.run(mm._analyze_with_anthropic_raw("SYS2", "統合入力", "claude-opus-4-7", "integrate"))

    assert msgs.calls[0] == {
        "model": "claude-opus-4-7",
        "max_tokens": 2000,
        "system": "SYS",
        "messages": [{"role": "user", "content": "ログ"}],
    }
    assert msgs.calls[1]["messages"] == [{"role": "user", "content": "統合入力"}]
    assert responses_calls == [
        {
            "model": "gpt-5.5",
            "instructions": "SYS",
            "input": "ログ",
            "max_output_tokens": 4000,
            "reasoning": {"effort": "low"},
            "text": {"verbosity": "low"},
        }
    ]
    assert (a.tokens_in, a.tokens_out, a.parsed["confidence"]) == (11, 7, 0.5)
    assert (o.tokens_in, o.tokens_out, o.parsed["confidence"]) == (5, 3, 0.5)
    assert i.role == "integrate"


# ─── 構成5 ─────────────────────────────────────────────────────────


def test_pipeline_llm_node_request_and_meta(monkeypatch):
    from log_analyzer import pipeline_runner as pr

    msgs = _AsyncFakeMessages(["ノード出力"])
    monkeypatch.setattr(
        llm_client.anthropic, "AsyncAnthropic", lambda: SimpleNamespace(messages=msgs)
    )
    node = {"id": "n1", "type": "llm", "model": "claude-opus-4-7", "prompt": "P"}

    text, meta = asyncio.run(pr._run_llm_node(node, "ログ", {}))

    assert msgs.calls[0]["model"] == "claude-opus-4-7"
    assert msgs.calls[0]["max_tokens"] == 2000
    assert msgs.calls[0]["system"] == "P"
    assert text == "ノード出力"
    assert (meta["tokens_in"], meta["tokens_out"]) == (11, 7)
    assert meta["raw_output"] == "ノード出力"
