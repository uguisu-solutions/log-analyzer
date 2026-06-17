"""audit_agent の入力予算化テスト (トークン上限・コスト対策)。

OpenAI 呼び出しは行わず、log_text の圧縮と分析結果 JSON の圧縮のみ検証する。
"""
from __future__ import annotations

from log_analyzer import audit_agent as aa
from log_analyzer.schema import (
    AnalysisResult,
    Category,
    RecommendedAction,
    RiskLevel,
    RootCauseCandidate,
    SuspectedNodeFinding,
)


def test_truncate_lines_keeps_head_and_tail():
    text = "\n".join(f"line{i}" for i in range(100))
    out = aa._truncate_lines(text, head=3, tail=2)
    assert "line0" in out and "line2" in out  # 頭
    assert "line98" in out and "line99" in out  # 末尾
    assert "line50" not in out  # 中間は落ちる
    assert "行省略" in out


def test_truncate_lines_noop_when_small():
    text = "a\nb\nc"
    assert aa._truncate_lines(text, head=40, tail=20) == text


def test_budget_log_text_truncates_per_node():
    big = "\n".join(f"log{i}" for i in range(200))
    log_text = (
        "## トポロジー要約\n- id=fw-01\n\n"
        f"=== NODE: fw-01 (type=FW) ===\n\n[ログ] syslog:\n{big}\n\n"
        f"=== NODE: web-01 (type=Server) ===\n\n[ログ] app:\n{big}\n"
    )
    out = aa._budget_log_text(log_text, head_lines=5, tail_lines=3, max_chars=100000)
    # 両ノードのヘッダは残る
    assert "=== NODE: fw-01" in out
    assert "=== NODE: web-01" in out
    # 各ノードで省略が起きている
    assert out.count("行省略") == 2
    # 全体としては元より大幅に短い
    assert len(out) < len(log_text)


def test_budget_log_text_global_char_cap():
    log_text = "x" * 5000  # NODE 区切り無しの一枚岩
    out = aa._budget_log_text(log_text, head_lines=10000, tail_lines=10000, max_chars=1000)
    assert "文字省略" in out
    assert len(out) <= 1000 + 100  # 注記分の余白のみ


def _sample_analysis() -> AnalysisResult:
    return AnalysisResult.model_construct(
        root_cause_candidates=[
            RootCauseCandidate(
                category=Category.FW, summary="ACL 不足",
                evidence=[f"ev{i}" for i in range(10)],
            )
        ],
        recommended_actions=[
            RecommendedAction(
                action="ACL を追加", human_judgment_required=True, risk_level=RiskLevel.HIGH,
                kind="permanent", confidence=0.8,
                steps=["手順1", "手順2", "手順3"], risks=["通信断リスク"],
                rollback_possible="yes", rollback_note="長いロールバック手順の説明文..." * 20,
            )
        ],
        confidence=0.7,
        suspected_node_ids=["fw-01"],
        suspected_node_findings=[
            SuspectedNodeFinding(node_id="fw-01", summary="主原因", severity="primary")
        ],
    )


def test_compact_analysis_drops_verbose_action_fields():
    out = aa._compact_analysis(_sample_analysis())
    act = out["recommended_actions"][0]
    # 判断に必要な最小限は残る
    assert act["action"] == "ACL を追加"
    assert act["risk_level"] == "high"
    assert act["confidence"] == 0.8
    # 嵩む詳細は落ちる
    assert "steps" not in act
    assert "risks" not in act
    assert "rollback_note" not in act


def test_compact_analysis_caps_evidence(monkeypatch):
    monkeypatch.setenv("AUDIT_MAX_EVIDENCE_PER_CAUSE", "3")
    out = aa._compact_analysis(_sample_analysis())
    assert out["root_cause_candidates"][0]["evidence"] == ["ev0", "ev1", "ev2"]
    # severity 判定に使う findings は残る
    assert out["suspected_node_findings"][0]["severity"] == "primary"


def test_build_user_input_includes_bq_evidence():
    bq_evidence = [
        {"host": "ADServer", "content": "BigQuery 取得結果: host=ADServer, 2 件\n列: ts, msg\n... | 4768 失敗"},
    ]
    out = aa._build_user_input("（マーカーのみ）", None, _sample_analysis(), bq_evidence)
    # rally が取得した実ログ証拠が監査入力に含まれる
    assert "rally が BigQuery から実際に取得したログ" in out
    assert "ADServer" in out
    assert "4768 失敗" in out


def test_build_user_input_no_evidence_section_when_empty():
    out = aa._build_user_input("ログ本文", None, _sample_analysis(), None)
    assert "rally が BigQuery から実際に取得したログ" not in out
