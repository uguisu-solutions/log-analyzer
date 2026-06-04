from log_analyzer.schema import (
    AnalysisResult,
    Category,
    ConfigId,
    RecommendedAction,
    RiskLevel,
    RootCauseCandidate,
)


def test_minimal_result_serializes():
    result = AnalysisResult(
        config_id=ConfigId.CONFIG1,
        input_log_ref="s3://bucket/log",
        root_cause_candidates=[],
        recommended_actions=[],
        confidence=0.5,
    )
    payload = result.model_dump_json()
    assert '"config_id":"config1"' in payload
    assert '"schema_version":"v0.2"' in payload


def test_human_judgment_flag_required():
    action = RecommendedAction(
        action="rollback firewall config",
        human_judgment_required=True,
        risk_level=RiskLevel.HIGH,
    )
    assert action.human_judgment_required is True


def test_root_cause_categories_constrained():
    candidate = RootCauseCandidate(
        category=Category.FW,
        summary="firewall rule mismatch",
        evidence=["DENY 10.0.0.1 -> 10.0.0.2"],
    )
    assert candidate.category == "FW"


def test_root_cause_ignores_legacy_rank_field():
    """schema v0.2 で撤去された rank が旧 JSON に残っていてもデコードできること。"""
    candidate = RootCauseCandidate.model_validate({
        "rank": 1,  # 古いデータに残っていても無視される
        "category": "FW",
        "summary": "x",
        "evidence": [],
    })
    assert not hasattr(candidate, "rank") or getattr(candidate, "rank", None) is None
    assert candidate.category == "FW"


def test_recommended_action_kind_default_and_override():
    """RecommendedAction.kind は既定 permanent、provisional 指定で上書きできる。"""
    a = RecommendedAction(action="応急処置", human_judgment_required=False, risk_level=RiskLevel.LOW)
    assert a.kind == "permanent"  # 旧データ互換: 未指定は本質対応扱い
    b = RecommendedAction(
        action="暫定回避", human_judgment_required=True, risk_level=RiskLevel.MID, kind="provisional"
    )
    assert b.kind == "provisional"
    # ラウンドトリップで保持される
    restored = RecommendedAction.model_validate(b.model_dump())
    assert restored.kind == "provisional"
