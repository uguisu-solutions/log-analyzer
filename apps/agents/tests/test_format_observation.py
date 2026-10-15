"""出力の形の遵守と入力の切り捨てを、実行結果まで届けるテスト（docs/plan/slm_support.md §9-4 / §9-6）。

クラウドと SLM の精度差を「推論の誤り」と「形を守れず再生成・フォールバックした」
「入力が切り捨てられた」に分けて見るため、各ノードの再生成回数と切り捨ての印を記録する。
LLM は Ollama 経路の送信関数（llm_client._ollama_post）を差し替えて模す。
"""
from __future__ import annotations

from log_analyzer import llm_client
from log_analyzer.rally import monitors as mon_mod


def _ollama_sequence(monkeypatch, responses: list[dict]) -> None:
    queue = list(responses)
    monkeypatch.setattr(llm_client, "_ollama_post", lambda url, payload, timeout: queue.pop(0))


def _reply(text: str, prompt_tokens: int = 10) -> dict:
    return {
        "message": {"content": text},
        "done_reason": "stop",
        "prompt_eval_count": prompt_tokens,
        "eval_count": 5,
    }


# ─── 監視ノードの LLM 呼び出し ──────────────────────────────────────


def test_monitor_llm_counts_format_retry_and_truncation(monkeypatch):
    """1 回目が JSON でなく再生成し、その呼び出しで入力が num_ctx に達していた。"""
    monkeypatch.setenv("OLLAMA_NUM_CTX", "100")
    _ollama_sequence(monkeypatch, [
        _reply("分析しました。結論は後で述べます。", prompt_tokens=100),
        _reply('{"findings": [], "next": "integrator"}', prompt_tokens=50),
    ])
    stats: dict = {}

    text, *_ = mon_mod._run_monitor_llm(
        model="ollama:m",
        system_prompt="sys",
        user_blocks=[{"type": "text", "text": "x"}],
        bq_sources={},
        stats=stats,
    )

    assert '"next": "integrator"' in text
    assert stats == {"format_retries": 1, "input_truncated": True}


def test_monitor_llm_stats_stay_zero_when_output_is_clean(monkeypatch):
    _ollama_sequence(monkeypatch, [_reply('{"next": "integrator"}')])
    stats: dict = {}

    mon_mod._run_monitor_llm(
        model="ollama:m", system_prompt="sys",
        user_blocks=[{"type": "text", "text": "x"}], bq_sources={}, stats=stats,
    )

    assert stats == {"format_retries": 0, "input_truncated": False}


def _state(model: str) -> dict:
    return {
        "log_text": "DENY src=10.0.40.17 dst=10.0.20.5 dport=80",
        "prompt_overrides": {},
        "model_overrides": {
            "orchestrator": model, "fw_monitor": model, "routing_monitor": model, "integrator": model,
        },
        "monitor_results": {},
    }


def test_monitor_node_reports_retries_and_truncation(monkeypatch):
    monkeypatch.setenv("OLLAMA_NUM_CTX", "100")
    _ollama_sequence(monkeypatch, [
        _reply("散文です", prompt_tokens=100),
        _reply('{"findings": [], "next": "integrator"}'),
    ])

    out = mon_mod.fw_monitor(_state("ollama:m"))

    assert out["format_retries"] == 1
    assert out["input_truncated"] is True


# ─── orchestrator / integrator ─────────────────────────────────────


def test_orchestrator_reports_retries_and_truncation(monkeypatch):
    from log_analyzer.rally.orchestrator import orchestrator_select_first

    monkeypatch.setenv("OLLAMA_NUM_CTX", "100")
    _ollama_sequence(monkeypatch, [
        _reply("fw から始めるのがよいでしょう", prompt_tokens=100),
        _reply('{"first_node": "fw", "focus_hint": "", "rationale": "r"}'),
    ])

    out = orchestrator_select_first(_state("ollama:m"))

    assert out["first_node"] == "fw"
    assert out["format_retries"] == 1
    assert out["input_truncated"] is True


def test_orchestrator_clean_output_has_no_retry(monkeypatch):
    from log_analyzer.rally.orchestrator import orchestrator_select_first

    _ollama_sequence(monkeypatch, [_reply('{"first_node": "dns", "focus_hint": "", "rationale": "r"}')])

    out = orchestrator_select_first(_state("ollama:m"))

    assert (out["format_retries"], out["input_truncated"]) == (0, False)


def test_integrator_reports_retries_and_truncation(monkeypatch):
    from log_analyzer.rally.integrator import integrator_node

    monkeypatch.setenv("OLLAMA_NUM_CTX", "100")
    _ollama_sequence(monkeypatch, [
        _reply("統合結果は次のとおりです", prompt_tokens=100),
        _reply('{"root_cause_candidates": [], "recommended_actions": [], "confidence": 0.5}'),
    ])

    out = integrator_node(_state("ollama:m"))

    assert out["format_retries"] == 1
    assert out["input_truncated"] is True


# ─── 構成4 全体：最終結果の info_loss_flags に届く ──────────────────────


def test_rally_result_flags_retries_and_truncation_without_touching_prompts(monkeypatch):
    import asyncio
    from unittest.mock import MagicMock

    from log_analyzer import rally_agent

    monkeypatch.setattr(rally_agent, "get_client", lambda: MagicMock())
    monkeypatch.setattr(rally_agent, "flush", lambda: None)
    monkeypatch.setenv("OLLAMA_NUM_CTX", "100")
    sent: list[dict] = []
    queue = [
        _reply('{"first_node": "fw", "focus_hint": "", "rationale": "r"}'),        # orchestrator
        _reply("FW を見ました", prompt_tokens=50),                                  # fw（散文）
        _reply('{"findings": [], "confidence": 0.5, "next": "integrator"}'),        # fw 再生成
        _reply('{"root_cause_candidates": [], "recommended_actions": [], "confidence": 0.4}',
               prompt_tokens=100),                                                   # integrator（切り捨て）
    ]

    def post(url, payload, timeout):
        sent.append(payload)
        return queue.pop(0)

    monkeypatch.setattr(llm_client, "_ollama_post", post)
    slm = "ollama:m"
    overrides = {s: slm for s in (
        "orchestrator", "fw_monitor", "routing_monitor", "app_monitor", "dns_monitor", "sec_monitor",
        "integrator",
    )}

    async def _run():
        return [ev async for ev in rally_agent.run_rally_stream(
            "DENY dst=10.0.20.5", "test", model_overrides=overrides, rally_max_rounds=3,
            decision_waiter=None,
        )]

    events = asyncio.run(_run())
    final = next(ev for ev in events if ev.kind == "final")
    flags = final.data["result"]["info_loss_flags"]

    assert "format_retry: fw (round 1)" in flags
    assert "input_truncated: integrator" in flags
    assert not any(f.startswith(("format_retry: orchestrator", "input_truncated: fw")) for f in flags)
    # 観測用のキーはモデルへの入力（プロンプト）に混ざらない
    integrator_request = sent[-1]
    assert "format_retries" not in str(integrator_request["messages"])
    assert "input_truncated" not in str(integrator_request["messages"])


# ─── 構成1 ─────────────────────────────────────────────────────────


def _run_config1(monkeypatch, prompt_tokens: int):
    from unittest.mock import MagicMock

    from log_analyzer import baseline_agent

    monkeypatch.setattr(baseline_agent, "get_client", lambda: MagicMock())
    monkeypatch.setattr(baseline_agent, "flush", lambda: None)
    monkeypatch.setenv("OLLAMA_NUM_CTX", "100")
    _ollama_sequence(monkeypatch, [_reply(
        '{"root_cause_candidates": [], "recommended_actions": [], "confidence": 0.3}',
        prompt_tokens=prompt_tokens,
    )])
    return baseline_agent.run_baseline("log", model_overrides={"analyze": "ollama:m"})


def test_config1_flags_input_truncation(monkeypatch):
    assert "input_truncated: analyze" in _run_config1(monkeypatch, prompt_tokens=100).info_loss_flags


def test_config1_has_no_flag_when_input_fits(monkeypatch):
    assert _run_config1(monkeypatch, prompt_tokens=99).info_loss_flags == []
