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
    assert '"schema_version":"v0.1"' in payload


def test_human_judgment_flag_required():
    action = RecommendedAction(
        action="rollback firewall config",
        human_judgment_required=True,
        risk_level=RiskLevel.HIGH,
    )
    assert action.human_judgment_required is True


def test_root_cause_categories_constrained():
    candidate = RootCauseCandidate(
        rank=1,
        category=Category.FW,
        summary="firewall rule mismatch",
        evidence=["DENY 10.0.0.1 -> 10.0.0.2"],
    )
    assert candidate.category == "FW"
