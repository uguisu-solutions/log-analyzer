"""llm_client（LLM 呼び出しの内側インタフェース）のテスト。

Phase 1（docs/plan/slm_support.md §4）の受け入れ条件は「振る舞いを一切変えない」こと。
そのため SDK に渡るリクエストが従来の直書きと同じ形になることを固定する。
実 API は叩かず、SDK のクライアント生成を差し替える。
"""
from __future__ import annotations

from types import SimpleNamespace

from log_analyzer import llm_client


class _FakeMessages:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self._responses.pop(0)


class _FakeAnthropic:
    def __init__(self, responses):
        self.messages = _FakeMessages(responses)


def _text_resp(text, tin=80, tout=40, **usage_extra):
    return SimpleNamespace(
        stop_reason="end_turn",
        content=[SimpleNamespace(type="text", text=text)],
        usage=SimpleNamespace(input_tokens=tin, output_tokens=tout, **usage_extra),
    )


# ─── Anthropic ──────────────────────────────────────────────────────


def test_anthropic_forwards_request_unchanged(monkeypatch):
    """tools / response_schema 未指定なら、従来どおり 4 引数だけで messages.create を呼ぶ。"""
    fake = _FakeAnthropic([_text_resp('{"ok": true}')])
    monkeypatch.setattr(llm_client.anthropic, "Anthropic", lambda: fake)

    system = [{"type": "text", "text": "sys", "cache_control": {"type": "ephemeral"}}]
    messages = [{"role": "user", "content": "log"}]
    llm_client.complete(
        model="claude-opus-4-7", system=system, messages=messages, max_tokens=2000
    )

    assert fake.messages.calls == [
        {"model": "claude-opus-4-7", "max_tokens": 2000, "system": system, "messages": messages}
    ]


def test_anthropic_normalizes_text_usage_and_stop_reason(monkeypatch):
    """text ブロックを連結した本文・usage の 4 分解・stop_reason を返す。"""
    resp = SimpleNamespace(
        stop_reason="end_turn",
        content=[
            SimpleNamespace(type="text", text="前半"),
            SimpleNamespace(type="text", text="後半"),
        ],
        usage=SimpleNamespace(
            input_tokens=10,
            output_tokens=5,
            cache_creation_input_tokens=3,
            cache_read_input_tokens=2,
        ),
    )
    monkeypatch.setattr(llm_client.anthropic, "Anthropic", lambda: _FakeAnthropic([resp]))

    out = llm_client.complete(model="claude-opus-4-7", system="s", messages=[], max_tokens=1)

    assert out.text == "前半後半"
    assert out.stop_reason == "end_turn"
    assert out.tool_calls == []
    assert out.raw is resp
    u = out.usage
    assert (u.input_tokens, u.output_tokens) == (10, 5)
    assert (u.cache_creation_input_tokens, u.cache_read_input_tokens) == (3, 2)


def test_anthropic_tolerates_untyped_blocks_and_missing_cache_usage(monkeypatch):
    """type を持たない text ブロック（既存テストの簡易モック）と cache 欄の欠落を許す。"""
    resp = SimpleNamespace(
        content=[SimpleNamespace(text="本文")],
        usage=SimpleNamespace(input_tokens=7, output_tokens=3),
    )
    monkeypatch.setattr(llm_client.anthropic, "Anthropic", lambda: _FakeAnthropic([resp]))

    out = llm_client.complete(model="claude-opus-4-7", system="s", messages=[], max_tokens=1)

    assert out.text == "本文"
    assert out.stop_reason is None
    assert (out.usage.cache_creation_input_tokens, out.usage.cache_read_input_tokens) == (0, 0)


def test_anthropic_tool_use_is_forwarded_and_normalized(monkeypatch):
    """tools は指定時だけ渡し、tool_use ブロックは tool_calls と Anthropic 形式の content_blocks に写す。

    content_blocks は tool-use ループで assistant ターンとして履歴に積み直すためのもの。
    """
    resp = SimpleNamespace(
        stop_reason="tool_use",
        content=[
            SimpleNamespace(type="text", text="調べます"),
            SimpleNamespace(type="tool_use", id="t1", name="bigquery_query", input={"host": "fw-01"}),
        ],
        usage=SimpleNamespace(input_tokens=1, output_tokens=1),
    )
    fake = _FakeAnthropic([resp])
    monkeypatch.setattr(llm_client.anthropic, "Anthropic", lambda: fake)
    tools = [{"name": "bigquery_query", "description": "d", "input_schema": {"type": "object"}}]

    out = llm_client.complete(
        model="claude-opus-4-7", system="s", messages=[], max_tokens=1, tools=tools
    )

    assert fake.messages.calls[0]["tools"] == tools
    assert out.stop_reason == "tool_use"
    assert out.text == "調べます"
    assert out.tool_calls == [
        llm_client.ToolCall(id="t1", name="bigquery_query", input={"host": "fw-01"})
    ]
    assert out.content_blocks == [
        {"type": "text", "text": "調べます"},
        {"type": "tool_use", "id": "t1", "name": "bigquery_query", "input": {"host": "fw-01"}},
    ]


# ─── OpenAI（gpt-*）─────────────────────────────────────────────────


class _FakeResponses:
    def __init__(self, response):
        self._response = response
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self._response


class _FakeOpenAI:
    def __init__(self, response):
        self.responses = _FakeResponses(response)


def test_openai_model_uses_responses_api_like_before(monkeypatch):
    """gpt-* は従来の直書きと同じ Responses API リクエスト（instructions / input /
    max_output_tokens / reasoning.effort=low / text.verbosity=low）になる。"""
    fake = _FakeOpenAI(
        SimpleNamespace(
            output_text='{"verdict": "agree"}',
            usage=SimpleNamespace(input_tokens=1200, output_tokens=300),
        )
    )
    monkeypatch.setattr(llm_client.openai, "OpenAI", lambda: fake)

    out = llm_client.complete(
        model="gpt-5.5",
        system="監査せよ",
        messages=[{"role": "user", "content": "入力"}],
        max_tokens=4000,
    )

    assert fake.responses.calls == [
        {
            "model": "gpt-5.5",
            "instructions": "監査せよ",
            "input": "入力",
            "max_output_tokens": 4000,
            "reasoning": {"effort": "low"},
            "text": {"verbosity": "low"},
        }
    ]
    assert out.text == '{"verdict": "agree"}'
    assert (out.usage.input_tokens, out.usage.output_tokens) == (1200, 300)


def test_openai_missing_output_and_usage_are_empty(monkeypatch):
    """output_text / usage が無い応答でも落ちず、空文字・0 になる（従来の getattr 防御と同じ）。"""
    fake = _FakeOpenAI(SimpleNamespace())
    monkeypatch.setattr(llm_client.openai, "OpenAI", lambda: fake)

    out = llm_client.complete(
        model="gpt-5.5", system="s", messages=[{"role": "user", "content": "x"}], max_tokens=1
    )

    assert out.text == ""
    assert (out.usage.input_tokens, out.usage.output_tokens) == (0, 0)


# ─── 非同期（構成3 / 構成5）────────────────────────────────────────


class _AsyncFakeCreate:
    def __init__(self, response):
        self._response = response
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return self._response


def test_acomplete_anthropic(monkeypatch):
    import asyncio

    create = _AsyncFakeCreate(_text_resp("非同期", tin=5, tout=2))
    monkeypatch.setattr(
        llm_client.anthropic, "AsyncAnthropic", lambda: SimpleNamespace(messages=create)
    )

    out = asyncio.run(
        llm_client.acomplete(
            model="claude-opus-4-7",
            system="s",
            messages=[{"role": "user", "content": "x"}],
            max_tokens=2000,
        )
    )

    assert create.calls == [
        {
            "model": "claude-opus-4-7",
            "max_tokens": 2000,
            "system": "s",
            "messages": [{"role": "user", "content": "x"}],
        }
    ]
    assert out.text == "非同期"
    assert (out.usage.input_tokens, out.usage.output_tokens) == (5, 2)


def test_acomplete_openai(monkeypatch):
    import asyncio

    create = _AsyncFakeCreate(
        SimpleNamespace(output_text="{}", usage=SimpleNamespace(input_tokens=3, output_tokens=1))
    )
    monkeypatch.setattr(
        llm_client.openai, "AsyncOpenAI", lambda: SimpleNamespace(responses=create)
    )

    out = asyncio.run(
        llm_client.acomplete(
            model="gpt-5.5", system="s", messages=[{"role": "user", "content": "x"}], max_tokens=4000
        )
    )

    assert create.calls[0]["instructions"] == "s"
    assert create.calls[0]["max_output_tokens"] == 4000
    assert out.text == "{}"


# ─── 集約の固定 ────────────────────────────────────────────────────


def test_only_llm_client_imports_llm_sdks():
    """SDK（anthropic / openai）を直接 import してよいのは llm_client だけ。

    呼び出しを 1 箇所に集めておかないと、Phase 3 のローカルモデル（Ollama）経路から漏れる。
    """
    import re
    from pathlib import Path

    pkg = Path(llm_client.__file__).parent
    pattern = re.compile(r"^\s*(import|from)\s+(anthropic|openai)\b", re.MULTILINE)
    offenders = sorted(
        str(p.relative_to(pkg))
        for p in pkg.rglob("*.py")
        if p.name != "llm_client.py" and pattern.search(p.read_text(encoding="utf-8"))
    )
    assert offenders == []
