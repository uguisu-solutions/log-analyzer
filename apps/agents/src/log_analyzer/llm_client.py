"""LLM 呼び出しの内側インタフェース（docs/plan/slm_support.md §4）。

各モジュールは SDK を直接生成せず、ここの :func:`complete` / :func:`acomplete` を呼ぶ。
入力の形は Anthropic Messages API に揃え（``system`` は文字列またはブロック配列、
``messages`` は Anthropic 形式）、プロバイダは ``model`` 文字列の接頭辞で選ぶ。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import anthropic
import openai


@dataclass
class Usage:
    """Anthropic の ``response.usage`` と同じ属性名で持つ（``tracing.usage_components`` がそのまま使える）。"""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0


@dataclass
class ToolCall:
    id: str
    name: str
    input: dict


@dataclass
class LlmResponse:
    text: str
    usage: Usage = field(default_factory=Usage)
    stop_reason: str | None = None  # Anthropic の値に揃える（"end_turn" / "tool_use" / "max_tokens" ...）
    tool_calls: list[ToolCall] = field(default_factory=list)
    # assistant ターンを Anthropic 形式のブロック配列で持つ（tool-use ループで履歴に積み直す用）
    content_blocks: list[dict] = field(default_factory=list)
    raw: Any = None  # プロバイダの生応答


def _int_attr(obj: Any, name: str) -> int:
    return int(getattr(obj, name, 0) or 0)


def _from_anthropic(response: Any) -> LlmResponse:
    texts: list[str] = []
    tool_calls: list[ToolCall] = []
    blocks: list[dict] = []
    for b in getattr(response, "content", None) or []:
        # type を持たないブロックは text とみなす（既存テストの簡易モック）
        btype = getattr(b, "type", "text")
        if btype == "text":
            t = getattr(b, "text", "") or ""
            texts.append(t)
            blocks.append({"type": "text", "text": t})
        elif btype == "tool_use":
            call = ToolCall(id=b.id, name=b.name, input=dict(b.input or {}))
            tool_calls.append(call)
            blocks.append(
                {"type": "tool_use", "id": call.id, "name": call.name, "input": call.input}
            )
    usage = getattr(response, "usage", None)
    return LlmResponse(
        text="".join(texts),
        usage=Usage(
            input_tokens=_int_attr(usage, "input_tokens"),
            output_tokens=_int_attr(usage, "output_tokens"),
            cache_creation_input_tokens=_int_attr(usage, "cache_creation_input_tokens"),
            cache_read_input_tokens=_int_attr(usage, "cache_read_input_tokens"),
        ),
        stop_reason=getattr(response, "stop_reason", None),
        tool_calls=tool_calls,
        content_blocks=blocks,
        raw=response,
    )


def _is_openai(model: str) -> bool:
    return model.startswith("gpt-")


def _openai_kwargs(
    model: str, system: str | list[dict], messages: list[dict], max_tokens: int
) -> dict[str, Any]:
    """GPT-5.x は Responses API + reasoning.effort / text.verbosity が推奨（OpenAI 公式ガイダンス）。

    呼び出し元（構成3・監査）はいずれも user 1 通の単発呼び出しなので、その形だけを受ける。
    """
    if len(messages) != 1 or not isinstance(messages[0].get("content"), str):
        raise ValueError("OpenAI 経路は user メッセージ 1 通（文字列）のみ対応")
    if not isinstance(system, str):
        system = "".join(b.get("text", "") for b in system)
    return {
        "model": model,
        "instructions": system,
        "input": messages[0]["content"],
        "max_output_tokens": max_tokens,
        "reasoning": {"effort": "low"},
        "text": {"verbosity": "low"},
    }


def _from_openai(response: Any) -> LlmResponse:
    usage = getattr(response, "usage", None)
    return LlmResponse(
        text=getattr(response, "output_text", None) or "",
        usage=Usage(
            input_tokens=_int_attr(usage, "input_tokens"),
            output_tokens=_int_attr(usage, "output_tokens"),
        ),
        raw=response,
    )


def _anthropic_kwargs(
    model: str,
    system: str | list[dict],
    messages: list[dict],
    max_tokens: int,
    tools: list[dict] | None,
) -> dict[str, Any]:
    # tools は指定時だけ付ける（従来の直書きと同じリクエストにするため）
    kwargs: dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        "system": system,
        "messages": messages,
    }
    if tools:
        kwargs["tools"] = tools
    return kwargs


def complete(
    *,
    model: str,
    system: str | list[dict],
    messages: list[dict],
    max_tokens: int,
    tools: list[dict] | None = None,
) -> LlmResponse:
    """同期版。プロバイダの例外はそのまま上げる（握りつぶしは呼び出し側の責務）。"""
    if _is_openai(model):
        return _from_openai(
            openai.OpenAI().responses.create(**_openai_kwargs(model, system, messages, max_tokens))
        )
    return _from_anthropic(
        anthropic.Anthropic().messages.create(
            **_anthropic_kwargs(model, system, messages, max_tokens, tools)
        )
    )


async def acomplete(
    *,
    model: str,
    system: str | list[dict],
    messages: list[dict],
    max_tokens: int,
    tools: list[dict] | None = None,
) -> LlmResponse:
    """非同期版（構成3 の並列呼び出し・構成5 の DAG 実行用）。"""
    if _is_openai(model):
        return _from_openai(
            await openai.AsyncOpenAI().responses.create(
                **_openai_kwargs(model, system, messages, max_tokens)
            )
        )
    return _from_anthropic(
        await anthropic.AsyncAnthropic().messages.create(
            **_anthropic_kwargs(model, system, messages, max_tokens, tools)
        )
    )
