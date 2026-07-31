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


def test_root_cause_category_accepts_nonstandard_and_normalizes():
    """SaaS/クラウド系の 'DB' 等・未知カテゴリでも失敗せず保持し、空/None は Unknown。

    以前は Category enum 限定で 'DB' が ValidationError を投げ解析全体が落ちていた。
    """
    # ドメイン固有値は綴りを保持 (UI バッジ cat-DB 用)
    assert RootCauseCandidate(category="DB", summary="ロック競合").category == "DB"
    # enum を渡しても値文字列になる
    assert RootCauseCandidate(category=Category.NET, summary="x").category == "Net"
    # 空/None/未指定は Unknown に正規化
    assert RootCauseCandidate(category="", summary="x").category == "Unknown"
    assert RootCauseCandidate(summary="x").category == "Unknown"
    assert RootCauseCandidate.model_validate({"category": None, "summary": "x"}).category == "Unknown"


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


def test_recommended_action_confidence_steps_rollback():
    """confidence / steps / risks / rollback_possible / rollback_note の既定値と上書き・往復。"""
    a = RecommendedAction(action="x", human_judgment_required=False, risk_level=RiskLevel.LOW)
    assert a.confidence == 0.0
    assert a.steps == [] and a.risks == []
    assert a.rollback_possible == "unknown"
    assert a.rollback_note == ""

    b = RecommendedAction(
        action="ポート付替え", human_judgment_required=True, risk_level=RiskLevel.MID,
        kind="provisional", confidence=0.82,
        steps=["SSH 接続", "対象ポートを予備へ付替え", "疎通確認"],
        risks=["他通信への波及"],
        rollback_possible="yes", rollback_note="元ポートへ戻す",
    )
    assert b.confidence == 0.82
    assert b.steps[0] == "SSH 接続"
    assert b.rollback_possible == "yes"
    restored = RecommendedAction.model_validate(b.model_dump())
    assert restored.confidence == 0.82
    assert restored.steps == ["SSH 接続", "対象ポートを予備へ付替え", "疎通確認"]
    assert restored.rollback_note == "元ポートへ戻す"
