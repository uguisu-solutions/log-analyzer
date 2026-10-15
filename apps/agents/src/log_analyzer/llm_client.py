"""LLM 呼び出しの内側インタフェース（docs/plan/slm_support.md §4）。

各モジュールは SDK を直接生成せず、ここの :func:`complete` / :func:`acomplete` を呼ぶ。
入力の形は Anthropic Messages API に揃え（``system`` は文字列またはブロック配列、
``messages`` は Anthropic 形式）、プロバイダは ``model`` 文字列の接頭辞で選ぶ
（``gpt-*`` → OpenAI、``ollama:<タグ>`` → Ollama、それ以外 → Anthropic）。
"""
from __future__ import annotations

import asyncio
import json
import os
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from typing import Any

import anthropic
import openai

_OLLAMA_PREFIX = "ollama:"
# 複数の text ブロックを 1 つの文字列にするときの区切り。コードベースの既存の約束
# （monitors._blocks_to_text / integrator の user_input）に合わせる
_BLOCK_SEP = "\n\n"


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
    # SLM（Ollama）で入力が num_ctx に達し、切り捨てられた可能性がある。
    # 黙って比較に混ぜないために実行記録へ残す（docs/plan/slm_support.md §7-1）
    input_truncated: bool = False


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


# ─── Ollama（SLM）─────────────────────────────────────────────────
# 推論方法はクラウドと同一にする（docs/plan/slm_support.md §3-1）。送るのは Anthropic 形式の
# 呼び出しを機械的に書き換えたものだけで、プロンプト・入力・ツール・出力上限は変えない。


def _is_ollama(model: str) -> bool:
    return model.startswith(_OLLAMA_PREFIX)


def _ollama_base_url() -> str:
    # Runpod は SSH トンネル越しに http://localhost:11435 で見せる（docs/plan/slm_support.md §1）
    return os.environ.get("OLLAMA_BASE_URL", "http://127.0.0.1:11434").rstrip("/")


def _ollama_num_ctx() -> int:
    return int(os.environ.get("OLLAMA_NUM_CTX") or 32768)


def _ollama_timeout() -> float:
    # 31B で長い出力を生成すると数分かかるため、既定は長めに取る
    return float(os.environ.get("OLLAMA_TIMEOUT_SEC") or 600)


def _ollama_post(url: str, payload: dict, timeout: float) -> dict:
    """Ollama のネイティブ API に JSON を POST する（テストではここを差し替える）。"""
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        # Ollama はエラー理由を JSON 本文で返す（例: モデル未取得）。原因が分かるよう残す
        detail = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Ollama がエラーを返した（HTTP {e.code}）: {detail}") from e


def _text_of(content: str | list[dict]) -> str:
    if isinstance(content, str):
        return content
    return _BLOCK_SEP.join(b.get("text", "") for b in content if b.get("type") == "text")


def _ollama_messages(system: str | list[dict], messages: list[dict]) -> list[dict]:
    """Anthropic 形式の system / messages を Ollama の messages に書き換える。

    - system は先頭の ``role: "system"`` メッセージにする
    - tool_use ブロックは assistant の ``tool_calls`` に、tool_result ブロックは
      ``role: "tool"`` メッセージ（``tool_name`` 付き）にする
    - ``cache_control`` は送らない（料金にだけ効き、出力は変わらない）
    """
    out: list[dict] = [{"role": "system", "content": _text_of(system)}]
    tool_names: dict[str, str] = {}
    for m in messages:
        content = m["content"]
        blocks = content if isinstance(content, list) else [{"type": "text", "text": content}]
        for b in blocks:
            if b.get("type") == "tool_result":
                out.append({
                    "role": "tool",
                    "tool_name": tool_names.get(b["tool_use_id"], ""),
                    "content": _text_of(b["content"]),
                })
        text = _text_of([b for b in blocks if b.get("type") == "text"])
        calls = []
        for b in blocks:
            if b.get("type") == "tool_use":
                tool_names[b["id"]] = b["name"]
                calls.append({"function": {"name": b["name"], "arguments": b["input"]}})
        if text or calls:
            msg: dict[str, Any] = {"role": m["role"], "content": text}
            if calls:
                msg["tool_calls"] = calls
            out.append(msg)
    return out


def _ollama_payload(
    model: str,
    system: str | list[dict],
    messages: list[dict],
    max_tokens: int,
    tools: list[dict] | None,
) -> dict[str, Any]:
    # think=False・サンプリング指定なしは、基準線の Opus 4.7（思考なし・既定サンプリング）に揃えるため。
    # num_ctx は必ず明示する（Ollama の既定 4,096 では長いログが黙って切られる。§7-1）
    payload: dict[str, Any] = {
        "model": model[len(_OLLAMA_PREFIX):],
        "messages": _ollama_messages(system, messages),
        "stream": False,
        "think": False,
        "options": {"num_predict": max_tokens, "num_ctx": _ollama_num_ctx()},
    }
    if tools:
        payload["tools"] = [
            {
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t["description"],
                    "parameters": t["input_schema"],
                },
            }
            for t in tools
        ]
    return payload


def _from_ollama(data: dict) -> LlmResponse:
    """Ollama の応答を Anthropic と同じ形（本文・tool_use ブロック・stop_reason・usage）にする。

    ``message.thinking`` は本文に混ぜない（Anthropic 経路でも text ブロックだけを読むのと同じ）。
    """
    message = data.get("message") or {}
    text = message.get("content") or ""
    blocks: list[dict] = [{"type": "text", "text": text}] if text else []
    tool_calls: list[ToolCall] = []
    for c in message.get("tool_calls") or []:
        fn = c.get("function") or {}
        args = fn.get("arguments") or {}
        if isinstance(args, str):  # OpenAI 互換 /v1 は JSON 文字列で返す
            args = json.loads(args)
        # Ollama は ID を返さないことがあるため振る。履歴で tool_result と対応付けるのに使う
        call_id = c.get("id") or f"ollama_{uuid.uuid4().hex[:12]}"
        call = ToolCall(id=call_id, name=fn["name"], input=dict(args))
        tool_calls.append(call)
        blocks.append({"type": "tool_use", "id": call.id, "name": call.name, "input": call.input})
    # Ollama は tool 要求でも done_reason が "stop" のままなので tool_calls の有無で判定する（§5）
    if tool_calls:
        stop_reason = "tool_use"
    elif data.get("done_reason") == "length":
        stop_reason = "max_tokens"
    else:
        stop_reason = "end_turn"
    prompt_tokens = int(data.get("prompt_eval_count") or 0)
    return LlmResponse(
        text=text,
        usage=Usage(
            input_tokens=prompt_tokens,
            output_tokens=int(data.get("eval_count") or 0),
        ),
        stop_reason=stop_reason,
        tool_calls=tool_calls,
        content_blocks=blocks,
        raw=data,
        input_truncated=prompt_tokens >= _ollama_num_ctx(),
    )


def _ollama_complete(
    model: str,
    system: str | list[dict],
    messages: list[dict],
    max_tokens: int,
    tools: list[dict] | None,
) -> LlmResponse:
    return _from_ollama(
        _ollama_post(
            f"{_ollama_base_url()}/api/chat",
            _ollama_payload(model, system, messages, max_tokens, tools),
            timeout=_ollama_timeout(),
        )
    )


def complete(
    *,
    model: str,
    system: str | list[dict],
    messages: list[dict],
    max_tokens: int,
    tools: list[dict] | None = None,
) -> LlmResponse:
    """同期版。プロバイダの例外はそのまま上げる（握りつぶしは呼び出し側の責務）。"""
    if _is_ollama(model):
        return _ollama_complete(model, system, messages, max_tokens, tools)
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
    if _is_ollama(model):
        # Ollama は標準ライブラリの同期 HTTP で呼ぶため、スレッドに逃がす
        return await asyncio.to_thread(
            _ollama_complete, model, system, messages, max_tokens, tools
        )
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
