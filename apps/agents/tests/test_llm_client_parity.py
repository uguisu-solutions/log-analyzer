"""クラウド（Anthropic）経路と SLM（Ollama）経路の送信内容の同一性テスト。

docs/plan/slm_support.md §3-1：推論方法はクラウドと同一で、違うのはモデルだけ。
同じ ``llm_client.complete`` 呼び出しを両経路に通し、モデルに届く中身
（system・会話の各ターン・ツール定義・出力上限）を共通の形に直して比べる。
API ごとの書式の差（ブロック配列か文字列か、tool_result の載せ方など）は正規化で吸収し、
**中身の差だけが検出される**ようにしている。実 API・実 Ollama には接続しない。
"""
from __future__ import annotations

import copy
from types import SimpleNamespace

from log_analyzer import llm_client
from log_analyzer.rally.tools import BIGQUERY_SCHEMA_TOOL_SCHEMA, BIGQUERY_TOOL_SCHEMA

# 複数の text ブロックを 1 つの文字列として見るときの区切り。
# コードベースの既存の約束（monitors._blocks_to_text / integrator の user_input）に合わせる。
_BLOCK_SEP = "\n\n"

# 監視ノードの tool-use の往復を模した呼び出し（system はキャッシュ指定付きブロック、
# user は安定ブロック＋動的ブロックの 2 つ、assistant はテキスト＋tool_use、続く tool_result）
_SYSTEM = [{"type": "text", "text": "あなたは FW 監視です。", "cache_control": {"type": "ephemeral"}}]
_MESSAGES = [
    {
        "role": "user",
        "content": [
            {"type": "text", "text": "## ログ\nDENY src=10.0.1.10", "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": "## 委譲\n初手です"},
        ],
    },
    {
        "role": "assistant",
        "content": [
            {"type": "text", "text": "BigQuery を確認します"},
            {"type": "tool_use", "id": "t1", "name": "bigquery_query", "input": {"host": "fw-01"}},
        ],
    },
    {
        "role": "user",
        "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "1 件: DENY"}],
    },
]
_TOOLS = [BIGQUERY_SCHEMA_TOOL_SCHEMA, BIGQUERY_TOOL_SCHEMA]
_MAX_TOKENS = 8000


# ─── 両経路の送信内容を共通の形に直す ─────────────────────────────


def _text_of(content) -> str:
    if isinstance(content, str):
        return content
    return _BLOCK_SEP.join(b["text"] for b in content if b.get("type") == "text")


def _anthropic_view(kwargs: dict) -> dict:
    tool_names: dict[str, str] = {}
    turns: list[tuple] = []
    for m in kwargs["messages"]:
        content = m["content"]
        blocks = content if isinstance(content, list) else [{"type": "text", "text": content}]
        for b in blocks:
            if b.get("type") == "tool_use":
                tool_names[b["id"]] = b["name"]
        for b in blocks:
            if b.get("type") == "tool_result":
                turns.append(("tool", tool_names[b["tool_use_id"]], b["content"]))
        text = _text_of([b for b in blocks if b.get("type") == "text"])
        calls = [(b["name"], b["input"]) for b in blocks if b.get("type") == "tool_use"]
        if text or calls:
            turns.append((m["role"], text, calls))
    return {
        "system": _text_of(kwargs["system"]),
        "turns": turns,
        "tools": [(t["name"], t["description"], t["input_schema"]) for t in kwargs.get("tools", [])],
        "max_tokens": kwargs["max_tokens"],
    }


def _ollama_view(payload: dict) -> dict:
    msgs = payload["messages"]
    assert msgs[0]["role"] == "system"
    turns: list[tuple] = []
    for m in msgs[1:]:
        if m["role"] == "tool":
            turns.append(("tool", m["tool_name"], m["content"]))
            continue
        calls = [
            (c["function"]["name"], c["function"]["arguments"]) for c in m.get("tool_calls", [])
        ]
        turns.append((m["role"], m["content"], calls))
    return {
        "system": msgs[0]["content"],
        "turns": turns,
        "tools": [
            (t["function"]["name"], t["function"]["description"], t["function"]["parameters"])
            for t in payload.get("tools", [])
        ],
        "max_tokens": payload["options"]["num_predict"],
    }


# ─── 送信内容の捕捉 ──────────────────────────────────────────────


def _capture_anthropic(monkeypatch) -> list[dict]:
    calls: list[dict] = []

    def create(**kwargs):
        calls.append(copy.deepcopy(kwargs))
        return SimpleNamespace(
            stop_reason="end_turn",
            content=[SimpleNamespace(type="text", text="{}")],
            usage=SimpleNamespace(input_tokens=1, output_tokens=1),
        )

    monkeypatch.setattr(
        llm_client.anthropic,
        "Anthropic",
        lambda: SimpleNamespace(messages=SimpleNamespace(create=create)),
    )
    return calls


def _capture_ollama(monkeypatch) -> list[dict]:
    calls: list[dict] = []

    def post(url, payload, timeout):
        calls.append(copy.deepcopy(payload))
        return {
            "message": {"role": "assistant", "content": "{}"},
            "done_reason": "stop",
            "prompt_eval_count": 1,
            "eval_count": 1,
        }

    monkeypatch.setattr(llm_client, "_ollama_post", post)
    return calls


def test_same_call_sends_same_content_to_anthropic_and_ollama(monkeypatch):
    sent_anthropic = _capture_anthropic(monkeypatch)
    sent_ollama = _capture_ollama(monkeypatch)

    for model in ("claude-opus-4-7", "ollama:gemma4:26b-a4b-it-q4_K_M"):
        llm_client.complete(
            model=model,
            system=copy.deepcopy(_SYSTEM),
            messages=copy.deepcopy(_MESSAGES),
            max_tokens=_MAX_TOKENS,
            tools=copy.deepcopy(_TOOLS),
        )

    (a,) = sent_anthropic
    (o,) = sent_ollama
    assert _ollama_view(o) == _anthropic_view(a)


def test_ollama_request_disables_thinking_and_sends_no_sampling(monkeypatch):
    """基準線の Opus 4.7 は思考なし・サンプリング既定で動くので、SLM 側も揃える（§3-1）。"""
    sent = _capture_ollama(monkeypatch)
    monkeypatch.delenv("OLLAMA_NUM_CTX", raising=False)

    llm_client.complete(
        model="ollama:gemma4:31b-it-q4_K_M", system="s", messages=[{"role": "user", "content": "x"}],
        max_tokens=2000,
    )

    (payload,) = sent
    assert payload["model"] == "gemma4:31b-it-q4_K_M"
    assert payload["think"] is False
    assert payload["stream"] is False
    # 入力が黙って切られないよう num_ctx は必ず明示する（Ollama の既定は 4,096）
    assert payload["options"] == {"num_predict": 2000, "num_ctx": 32768}
    assert "tools" not in payload


def test_ollama_num_ctx_and_base_url_from_env(monkeypatch):
    urls: list[str] = []
    payloads: list[dict] = []

    def post(url, payload, timeout):
        urls.append(url)
        payloads.append(payload)
        return {"message": {"content": ""}, "done_reason": "stop"}

    monkeypatch.setattr(llm_client, "_ollama_post", post)
    monkeypatch.setenv("OLLAMA_NUM_CTX", "65536")
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://localhost:11435/")  # Runpod の SSH トンネル

    llm_client.complete(
        model="ollama:m", system="s", messages=[{"role": "user", "content": "x"}], max_tokens=1
    )

    assert urls == ["http://localhost:11435/api/chat"]
    assert payloads[0]["options"]["num_ctx"] == 65536


# ─── 応答の正規化（Anthropic の応答と同じ形で呼び出し側に返す）──────────


def _ollama_returning(monkeypatch, data: dict) -> None:
    monkeypatch.setattr(llm_client, "_ollama_post", lambda url, payload, timeout: data)


def _call_ollama() -> llm_client.LlmResponse:
    return llm_client.complete(
        model="ollama:m", system="s", messages=[{"role": "user", "content": "x"}], max_tokens=10
    )


def test_ollama_text_response_is_normalized(monkeypatch):
    _ollama_returning(monkeypatch, {
        "message": {"role": "assistant", "content": '{"ok": true}', "thinking": "考え中"},
        "done_reason": "stop",
        "prompt_eval_count": 120,
        "eval_count": 30,
    })

    out = _call_ollama()

    assert out.text == '{"ok": true}'  # thinking は本文に混ぜない
    assert out.stop_reason == "end_turn"
    assert out.tool_calls == []
    assert out.content_blocks == [{"type": "text", "text": '{"ok": true}'}]
    u = out.usage
    assert (u.input_tokens, u.output_tokens) == (120, 30)
    assert (u.cache_creation_input_tokens, u.cache_read_input_tokens) == (0, 0)


def test_ollama_length_stop_maps_to_max_tokens(monkeypatch):
    _ollama_returning(monkeypatch, {"message": {"content": '{"a": '}, "done_reason": "length"})
    assert _call_ollama().stop_reason == "max_tokens"


def test_ollama_tool_calls_are_normalized(monkeypatch):
    """tool_calls があれば stop_reason は tool_use（Ollama の done_reason は stop のまま）。
    arguments はネイティブ API ではオブジェクトだが、文字列で来ても受ける。"""
    _ollama_returning(monkeypatch, {
        "message": {
            "content": "",
            "tool_calls": [
                {"function": {"name": "bigquery_query", "arguments": {"host": "fw-01"}}},
                {"function": {"name": "bigquery_schema", "arguments": '{"host": "fw-02"}'}},
            ],
        },
        "done_reason": "stop",
    })

    out = _call_ollama()

    assert out.stop_reason == "tool_use"
    assert [(c.name, c.input) for c in out.tool_calls] == [
        ("bigquery_query", {"host": "fw-01"}),
        ("bigquery_schema", {"host": "fw-02"}),
    ]
    ids = [c.id for c in out.tool_calls]
    assert len(set(ids)) == 2 and all(ids)  # 履歴で tool_result と対応付けるため一意な ID を振る
    assert out.content_blocks == [
        {"type": "tool_use", "id": ids[0], "name": "bigquery_query", "input": {"host": "fw-01"}},
        {"type": "tool_use", "id": ids[1], "name": "bigquery_schema", "input": {"host": "fw-02"}},
    ]


# ─── 呼び出し側を通した同一性（監視ノードの tool-use ループ全体）──────────


def test_monitor_tool_loop_sends_same_content_on_every_turn(monkeypatch):
    """監視ノードの tool-use ループを両経路で回し、全往復のリクエスト中身が一致すること。

    応答側は「BigQuery を 1 回呼ぶ → 最終 JSON」を両経路で同じ内容になるよう用意する。
    """
    from log_analyzer.rally import monitors as mon_mod

    final_json = '{"findings": [], "next": "integrator"}'

    sent_anthropic: list[dict] = []
    anthropic_responses = [
        SimpleNamespace(
            stop_reason="tool_use",
            content=[
                SimpleNamespace(type="text", text="確認します"),
                SimpleNamespace(type="tool_use", id="t1", name="bigquery_query", input={"host": "fw-01"}),
            ],
            usage=SimpleNamespace(input_tokens=1, output_tokens=1),
        ),
        SimpleNamespace(
            stop_reason="end_turn",
            content=[SimpleNamespace(type="text", text=final_json)],
            usage=SimpleNamespace(input_tokens=1, output_tokens=1),
        ),
    ]

    def a_create(**kwargs):
        sent_anthropic.append(copy.deepcopy(kwargs))
        return anthropic_responses.pop(0)

    monkeypatch.setattr(
        llm_client.anthropic, "Anthropic",
        lambda: SimpleNamespace(messages=SimpleNamespace(create=a_create)),
    )

    sent_ollama: list[dict] = []
    ollama_responses = [
        {"message": {"content": "確認します", "tool_calls": [
            {"function": {"name": "bigquery_query", "arguments": {"host": "fw-01"}}}
        ]}, "done_reason": "stop"},
        {"message": {"content": final_json}, "done_reason": "stop"},
    ]

    def o_post(url, payload, timeout):
        sent_ollama.append(copy.deepcopy(payload))
        return ollama_responses.pop(0)

    monkeypatch.setattr(llm_client, "_ollama_post", o_post)
    monkeypatch.setattr(mon_mod, "run_bigquery_tool", lambda tool_input, allowed: "1 件: DENY")

    results = []
    for model in ("claude-opus-4-7", "ollama:gemma4:26b-a4b-it-q4_K_M"):
        results.append(mon_mod._run_monitor_llm(
            model=model,
            system_prompt="あなたは FW 監視です。",
            user_blocks=[
                {"type": "text", "text": "## ログ\nDENY", "cache_control": {"type": "ephemeral"}},
                {"type": "text", "text": "## 委譲\n初手"},
            ],
            bq_sources={"fw-01": {"table": "device_logs"}},
        ))

    assert len(sent_anthropic) == len(sent_ollama) == 2
    for a, o in zip(sent_anthropic, sent_ollama):
        assert _ollama_view(o) == _anthropic_view(a)
    # 最終テキストと実行したツールの記録も同じ
    (a_text, *_, a_calls, a_fetched), (o_text, *_, o_calls, o_fetched) = results
    assert (a_text, a_calls, a_fetched) == (o_text, o_calls, o_fetched)


# ─── 入力の切り捨て検知（§7-1）─────────────────────────────────────


def test_ollama_flags_input_truncation_when_prompt_fills_context(monkeypatch):
    """評価された入力トークンが num_ctx に達したら、切り捨てられた可能性として印を付ける。
    黙って比較に混ぜないため（Anthropic 経路では常に False）。"""
    monkeypatch.setenv("OLLAMA_NUM_CTX", "4096")
    _ollama_returning(monkeypatch, {"message": {"content": "x"}, "done_reason": "stop",
                                    "prompt_eval_count": 4096, "eval_count": 1})
    assert _call_ollama().input_truncated is True

    _ollama_returning(monkeypatch, {"message": {"content": "x"}, "done_reason": "stop",
                                    "prompt_eval_count": 4000, "eval_count": 1})
    assert _call_ollama().input_truncated is False


def test_anthropic_response_is_never_flagged_truncated(monkeypatch):
    _capture_anthropic(monkeypatch)
    out = llm_client.complete(model="claude-opus-4-7", system="s", messages=[], max_tokens=1)
    assert out.input_truncated is False


def test_acomplete_routes_ollama_with_same_request(monkeypatch):
    """非同期版（構成3 / 構成5）も同じリクエストを送る。"""
    import asyncio

    sent = _capture_ollama(monkeypatch)
    kwargs = dict(
        model="ollama:m", system="s", messages=[{"role": "user", "content": "x"}], max_tokens=2000
    )

    out = asyncio.run(llm_client.acomplete(**kwargs))
    llm_client.complete(**kwargs)

    assert out.text == "{}"
    assert sent[0] == sent[1]


# ─── 送信部分（localhost の擬似 Ollama で実際に HTTP を通す）──────────────


def _serve(handler_cls):
    import threading
    from http.server import HTTPServer

    server = HTTPServer(("127.0.0.1", 0), handler_cls)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def test_ollama_post_round_trips_json_over_http():
    import json
    from http.server import BaseHTTPRequestHandler

    received: list[dict] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            received.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            body = json.dumps({"message": {"content": "応答"}, "done_reason": "stop"}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = _serve(Handler)
    try:
        url = f"http://127.0.0.1:{server.server_port}/api/chat"
        out = llm_client._ollama_post(url, {"model": "m", "messages": []}, timeout=5)
    finally:
        server.shutdown()

    assert received == [{"model": "m", "messages": []}]
    assert out["message"]["content"] == "応答"


def test_ollama_post_surfaces_error_body():
    """モデル未取得などのエラーで、Ollama が返した理由を例外メッセージに残す。"""
    import json
    from http.server import BaseHTTPRequestHandler

    import pytest

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            self.rfile.read(int(self.headers["Content-Length"]))
            body = json.dumps({"error": "model 'gemma4:31b' not found"}).encode()
            self.send_response(404)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = _serve(Handler)
    try:
        url = f"http://127.0.0.1:{server.server_port}/api/chat"
        with pytest.raises(RuntimeError, match="not found"):
            llm_client._ollama_post(url, {}, timeout=5)
    finally:
        server.shutdown()


def test_ollama_timeout_from_env(monkeypatch):
    timeouts: list[float] = []

    def post(url, payload, timeout):
        timeouts.append(timeout)
        return {"message": {"content": ""}, "done_reason": "stop"}

    monkeypatch.setattr(llm_client, "_ollama_post", post)
    monkeypatch.delenv("OLLAMA_TIMEOUT_SEC", raising=False)
    _call_ollama()
    monkeypatch.setenv("OLLAMA_TIMEOUT_SEC", "1800")
    _call_ollama()
    assert timeouts == [600.0, 1800.0]


# ─── 接続先で使えるモデルの一覧（画面の選択欄用）─────────────────────


def test_ollama_models_lists_tags_at_current_endpoint(monkeypatch):
    """接続先（手元 or Runpod トンネル）にあるモデルを ``ollama:<タグ>`` で返す。"""
    urls: list[str] = []

    def get(url, timeout):
        urls.append(url)
        return {"models": [{"name": "gemma4:26b-a4b-it-q4_K_M"}, {"name": "gemma4:31b-it-q4_K_M"}]}

    monkeypatch.setattr(llm_client, "_ollama_get", get)
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://localhost:11435")

    assert llm_client.ollama_models() == [
        "ollama:gemma4:26b-a4b-it-q4_K_M", "ollama:gemma4:31b-it-q4_K_M",
    ]
    assert urls == ["http://localhost:11435/api/tags"]


def test_ollama_get_reads_json_over_http():
    import json
    from http.server import BaseHTTPRequestHandler

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            body = json.dumps({"models": [{"name": "gemma4:e4b"}]}).encode()
            self.send_response(200)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = _serve(Handler)
    try:
        out = llm_client._ollama_get(f"http://127.0.0.1:{server.server_port}/api/tags", timeout=5)
    finally:
        server.shutdown()
    assert out == {"models": [{"name": "gemma4:e4b"}]}
