"""監査エージェント (Phase C) のスモークテスト。

実 OpenAI API は呼ばず、API キー未設定パスとモック JSON のパースを検証する。
"""
from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

from log_analyzer.audit_agent import run_audit
from log_analyzer.schema import (
    AnalysisResult,
    AuditReport,
    Category,
    ConfigId,
    RootCauseCandidate,
)


def _sample_analysis() -> AnalysisResult:
    return AnalysisResult(
        config_id=ConfigId.CONFIG4,
        input_log_ref="test",
        root_cause_candidates=[
            RootCauseCandidate(category=Category.FW, summary="ACL コメントアウト"),
        ],
        recommended_actions=[],
        confidence=0.85,
    )


def test_audit_returns_uncertain_without_api_key(monkeypatch):
    """OPENAI_API_KEY が無ければスキップ verdict='uncertain' を返し例外を投げない。"""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    report = run_audit("log content", {"nodes": [{"id": "fw-01"}]}, _sample_analysis())
    assert isinstance(report, AuditReport)
    assert report.verdict == "uncertain"
    assert "OPENAI_API_KEY" in report.summary
    assert report.tokens_in == 0
    assert report.tokens_out == 0


def test_audit_parses_agree_verdict(monkeypatch):
    """モック GPT 応答を JSON で返したとき verdict / concerns / alternative_hypotheses が拾えること。"""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-fake")
    fake_message = MagicMock()
    fake_message.content = (
        '{"verdict": "agree", "confidence": 0.9, '
        '"summary": "主原因と severity 共に妥当", '
        '"concerns": ["lb-01 の確信度は若干高すぎるかも"], '
        '"alternative_hypotheses": []}'
    )
    fake_choice = MagicMock(message=fake_message)
    fake_usage = MagicMock(prompt_tokens=1200, completion_tokens=80)
    fake_response = MagicMock(choices=[fake_choice], usage=fake_usage)
    fake_client = MagicMock()
    fake_client.chat.completions.create.return_value = fake_response

    with patch("log_analyzer.audit_agent.openai.OpenAI", return_value=fake_client):
        report = run_audit("log", {"nodes": [{"id": "fw-01"}]}, _sample_analysis())
    assert report.verdict == "agree"
    assert report.confidence == 0.9
    assert "妥当" in report.summary
    assert len(report.concerns) == 1
    assert report.alternative_hypotheses == []
    assert report.tokens_in == 1200
    assert report.tokens_out == 80


def test_audit_normalizes_bad_verdict(monkeypatch):
    """規定外 verdict は 'uncertain' に正規化。"""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-fake")
    fake_message = MagicMock()
    fake_message.content = '{"verdict": "STRONGLY_AGREE", "confidence": 0.99, "summary": "x"}'
    fake_choice = MagicMock(message=fake_message)
    fake_usage = MagicMock(prompt_tokens=10, completion_tokens=5)
    fake_response = MagicMock(choices=[fake_choice], usage=fake_usage)
    fake_client = MagicMock()
    fake_client.chat.completions.create.return_value = fake_response
    with patch("log_analyzer.audit_agent.openai.OpenAI", return_value=fake_client):
        report = run_audit("log", None, _sample_analysis())
    assert report.verdict == "uncertain"


def test_audit_handles_openai_exception(monkeypatch):
    """OpenAI 呼び出しで例外が出ても uncertain で返り、上層に伝播しない。"""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-fake")
    fake_client = MagicMock()
    fake_client.chat.completions.create.side_effect = RuntimeError("network error")
    with patch("log_analyzer.audit_agent.openai.OpenAI", return_value=fake_client):
        report = run_audit("log", None, _sample_analysis())
    assert report.verdict == "uncertain"
    assert "network error" in report.summary


def test_audit_uses_custom_system_prompt(monkeypatch):
    """system_prompt を渡すと OpenAI 呼び出しの system メッセージに反映される。"""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-fake")
    fake_message = MagicMock()
    fake_message.content = '{"verdict": "agree", "confidence": 0.7, "summary": "ok"}'
    fake_response = MagicMock(choices=[MagicMock(message=fake_message)], usage=MagicMock(prompt_tokens=1, completion_tokens=1))
    fake_client = MagicMock()
    fake_client.chat.completions.create.return_value = fake_response
    custom = "あなたは厳格な監査者です。必ず disagree を疑え。"
    with patch("log_analyzer.audit_agent.openai.OpenAI", return_value=fake_client):
        run_audit("log", None, _sample_analysis(), system_prompt=custom)
    _, kwargs = fake_client.chat.completions.create.call_args
    sys_msg = next(m for m in kwargs["messages"] if m["role"] == "system")
    assert sys_msg["content"] == custom


def test_audit_blank_system_prompt_falls_back_to_default(monkeypatch):
    """空文字の system_prompt は既定 SYSTEM_PROMPT にフォールバックする。"""
    from log_analyzer.audit_agent import SYSTEM_PROMPT
    monkeypatch.setenv("OPENAI_API_KEY", "sk-fake")
    fake_message = MagicMock()
    fake_message.content = '{"verdict": "agree", "confidence": 0.7, "summary": "ok"}'
    fake_response = MagicMock(choices=[MagicMock(message=fake_message)], usage=MagicMock(prompt_tokens=1, completion_tokens=1))
    fake_client = MagicMock()
    fake_client.chat.completions.create.return_value = fake_response
    with patch("log_analyzer.audit_agent.openai.OpenAI", return_value=fake_client):
        run_audit("log", None, _sample_analysis(), system_prompt="   ")
    _, kwargs = fake_client.chat.completions.create.call_args
    sys_msg = next(m for m in kwargs["messages"] if m["role"] == "system")
    assert sys_msg["content"] == SYSTEM_PROMPT


def test_audit_prompt_endpoint_returns_default():
    """GET /api/audit-prompt が既定プロンプトを返す。"""
    from fastapi.testclient import TestClient
    from log_analyzer import api as api_mod
    from log_analyzer.audit_agent import SYSTEM_PROMPT

    client = TestClient(api_mod.app)
    r = client.get("/api/audit-prompt")
    assert r.status_code == 200
    body = r.json()
    assert body["prompt"] == SYSTEM_PROMPT
    assert body["model"]


def test_audit_report_attached_to_analysis_result(monkeypatch):
    """AnalysisResult.audit_report に AuditReport がそのまま入ること (schema 側の挙動確認)。"""
    ar = _sample_analysis()
    assert ar.audit_report is None
    ar.audit_report = AuditReport(verdict="agree", confidence=0.8, summary="ok")
    dumped = ar.model_dump(mode="json")
    assert dumped["audit_report"]["verdict"] == "agree"
    # 復元
    restored = AnalysisResult.model_validate(dumped)
    assert restored.audit_report is not None
    assert restored.audit_report.verdict == "agree"
