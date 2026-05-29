"""Config-First 2 段階解析エンドポイントのスモークテスト。

実 LLM 呼び出しはモックして、バリデーション境界 + 仮説ブロック生成 + StageOutput
構築のみを検証する。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from log_analyzer import api as api_mod
from log_analyzer.rally_two_stage import (
    _build_final_result,
    _build_stage_one_hypothesis_block,
    _result_to_stage_output,
)
from log_analyzer.schema import (
    AnalysisResult,
    ConfigId,
    Metrics,
    RecommendedAction,
    RiskLevel,
    RootCauseCandidate,
    Category,
    StageOutput,
    SuspectedNodeFinding,
)


# ─── 単体テスト: ヘルパ関数 ─────────────────────────────────────


def test_build_stage_one_hypothesis_block_basic():
    so = StageOutput(
        stage="config",
        stage_label="Stage 1: コンフィグ解析",
        confidence=0.7,
        summary="FW のポリシ反映漏れの疑い",
        suspected_node_findings=[
            SuspectedNodeFinding(node_id="fw-01", summary="policy reload の差分", severity="primary"),
            SuspectedNodeFinding(node_id="lb-01", summary="影響先", severity="secondary"),
        ],
    )
    text = _build_stage_one_hypothesis_block(so)
    assert "Stage 1 仮説" in text
    assert "FW のポリシ反映漏れの疑い" in text
    assert "fw-01 [primary]" in text
    assert "lb-01 [secondary]" in text
    assert "検証" in text  # 「ログで検証してください」のガイドが含まれること


def test_build_stage_one_hypothesis_block_empty():
    """findings も summary も空なら何も出力しない (Stage 2 で仮説ブロックを省略)。"""
    so = StageOutput(stage="config", stage_label="x")
    text = _build_stage_one_hypothesis_block(so)
    assert text == ""


def test_result_to_stage_output_extracts_summary_from_candidates():
    result = AnalysisResult(
        config_id=ConfigId.CONFIG4,
        input_log_ref="x",
        root_cause_candidates=[
            RootCauseCandidate(category=Category.FW, summary="lb-to-app-01 が欠落"),
            RootCauseCandidate(category=Category.NET, summary="下流のヘルスチェック失敗"),
        ],
        recommended_actions=[],
        confidence=0.8,
        suspected_node_ids=["fw-01", "lb-01"],
        suspected_node_findings=[
            SuspectedNodeFinding(node_id="fw-01", severity="primary", summary="root"),
            SuspectedNodeFinding(node_id="lb-01", severity="secondary", summary="downstream"),
        ],
        metrics=Metrics(tokens_in=1000, tokens_out=300, latency_ms_total=5000),
        delegation_rounds=2,
    )
    so = _result_to_stage_output("config", result)
    assert so.stage == "config"
    assert so.stage_label == "Stage 1: コンフィグ解析"
    assert "lb-to-app-01" in so.summary  # 上位 2 候補から組み立て
    assert so.suspected_node_ids == ["fw-01", "lb-01"]
    assert so.tokens_in == 1000
    assert so.delegation_rounds == 2
    assert len(so.root_cause_candidates) == 2


def test_build_final_result_aggregates_two_stages():
    s1 = StageOutput(
        stage="config", stage_label="Stage 1",
        confidence=0.6, tokens_in=500, tokens_out=150, latency_ms_total=3000,
        delegation_rounds=2,
        suspected_node_ids=["fw-01"],
        suspected_node_findings=[SuspectedNodeFinding(node_id="fw-01", severity="primary")],
        root_cause_candidates=[RootCauseCandidate(category=Category.FW, summary="hypothesis")],
        recommended_actions=[],
    )
    s2 = StageOutput(
        stage="log", stage_label="Stage 2",
        confidence=0.85, tokens_in=1200, tokens_out=400, latency_ms_total=7000,
        delegation_rounds=3,
        suspected_node_ids=["fw-01", "lb-01"],
        suspected_node_findings=[
            SuspectedNodeFinding(node_id="fw-01", severity="primary"),
            SuspectedNodeFinding(node_id="lb-01", severity="secondary"),
        ],
        root_cause_candidates=[RootCauseCandidate(category=Category.FW, summary="verified")],
        recommended_actions=[
            RecommendedAction(action="rollback", human_judgment_required=True, risk_level=RiskLevel.HIGH)
        ],
    )
    result = _build_final_result(
        stage_outputs=[s1, s2], trace_id="trace-x", log_ref="run-1"
    )
    # 主結果は Stage 2
    assert result.confidence == 0.85
    assert result.suspected_node_ids == ["fw-01", "lb-01"]
    assert result.delegation_rounds == 3
    assert len(result.recommended_actions) == 1
    # tokens / latency は両 Stage の合算
    assert result.metrics.tokens_in == 1700
    assert result.metrics.tokens_out == 550
    assert result.metrics.latency_ms_total == 10000
    # stage_outputs に両方残る
    assert len(result.stage_outputs) == 2
    assert result.stage_outputs[0].stage == "config"
    assert result.stage_outputs[1].stage == "log"


def test_build_final_result_abort_only_stage_one():
    """abort 時は Stage 1 のみで最終結果が組み立てられる。"""
    s1 = StageOutput(
        stage="config", confidence=0.55, tokens_in=400, tokens_out=120,
        suspected_node_ids=["fw-01"],
        root_cause_candidates=[RootCauseCandidate(category=Category.FW, summary="guess only")],
    )
    result = _build_final_result(
        stage_outputs=[s1], trace_id="trace-y", log_ref="aborted"
    )
    assert result.confidence == 0.55
    assert len(result.stage_outputs) == 1
    assert result.stage_outputs[0].stage == "config"
    # Stage 2 の確信度上昇は無いので Stage 1 の値そのまま
    assert result.metrics.tokens_in == 400


# ─── エンドポイント側のバリデーション境界 ──────────────────────


def test_config_first_rejects_non_config4():
    client = TestClient(api_mod.app)
    r = client.post(
        "/api/runs/config-first-stream",
        json={
            "config": "config1",
            "topology": {"nodes": [{"id": "n1"}], "links": []},
            "node_configs": {"n1": [{"name": "x.conf", "content": "x"}]},
        },
    )
    assert r.status_code == 400
    assert "config4" in r.json()["detail"]


def test_config_first_rejects_empty_nodes():
    client = TestClient(api_mod.app)
    r = client.post(
        "/api/runs/config-first-stream",
        json={
            "config": "config4",
            "topology": {"nodes": [], "links": []},
            "node_configs": {},
        },
    )
    assert r.status_code == 400


def test_config_first_requires_at_least_one_config():
    """Config-First では configs が 1 件もなければ実行不可 (Stage 1 で渡せるものが無い)。"""
    client = TestClient(api_mod.app)
    r = client.post(
        "/api/runs/config-first-stream",
        json={
            "config": "config4",
            "topology": {"nodes": [{"id": "fw-01"}], "links": []},
            "node_logs": {"fw-01": [{"name": "fw.log", "content": "deny ..."}]},
            "node_configs": {},  # 空
        },
    )
    assert r.status_code == 400
    assert "Config" in r.json()["detail"] or "config" in r.json()["detail"].lower()


def test_skip_config_stage_accepts_logs_only():
    """skip_config_stage=True 時は configs 不要、logs が 1 件以上あれば実行可能。

    実 LLM 呼び出しの手前まで進ませる: 不正な saved_config を指定して 404 で止まる
    ことを確認 (通常モードと同様の経路バリデーション通過)。
    """
    client = TestClient(api_mod.app)
    r = client.post(
        "/api/runs/config-first-stream",
        json={
            "config": "user:99999",  # 存在しない
            "topology": {"nodes": [{"id": "fw-01"}], "links": []},
            "node_logs": {"fw-01": [{"name": "fw.log", "content": "deny ..."}]},
            "node_configs": {},  # 空でも OK
            "skip_config_stage": True,
        },
    )
    assert r.status_code == 404  # saved_config 未登録、ログ・config 不在の 400 ではない


def test_skip_config_stage_still_requires_at_least_one_log():
    """skip 時は configs 不要だが、代わりに logs が 1 件以上必須。"""
    client = TestClient(api_mod.app)
    r = client.post(
        "/api/runs/config-first-stream",
        json={
            "config": "config4",
            "topology": {"nodes": [{"id": "fw-01"}], "links": []},
            "node_logs": {},
            "node_configs": {"fw-01": [{"name": "x.conf", "content": "x"}]},  # configs はあるが skip
            "skip_config_stage": True,
        },
    )
    assert r.status_code == 400
    assert "ログ" in r.json()["detail"]


def test_decision_api_accepts_advance_abort():
    """decision エンドポイントが新規 advance / abort を受け付けること。

    pending decision が無いので 404 になるが、これは「アクション名は OK だが run_id が無い」
    という意味で、不正アクションの 400 (action は ... のいずれか) とは区別される。
    """
    client = TestClient(api_mod.app)
    for action in ["advance", "abort"]:
        r = client.post(
            "/api/runs/no-such-run/decision",
            json={"action": action},
        )
        assert r.status_code == 404, f"{action} should be valid action but got: {r.json()}"


def test_decision_api_rejects_unknown_action():
    client = TestClient(api_mod.app)
    r = client.post(
        "/api/runs/no-such-run/decision",
        json={"action": "frobnicate"},
    )
    assert r.status_code == 400
    assert "action" in r.json()["detail"]


# ─── 統合フロー: run_two_stage_stream の動作 (LLM モック) ─────


def _make_fake_rally(call_count: dict, distinguish_stage_two: bool = False):
    """Stage 用にモック化された run_rally_stream を返す。"""
    from log_analyzer.rally_agent import StreamEvent as RealStreamEvent

    async def fake_run_rally_stream(*args, **kwargs):
        call_count["n"] = call_count.get("n", 0) + 1
        is_stage_two = distinguish_stage_two and "Stage 1 仮説" in args[0]
        ar = AnalysisResult(
            config_id=ConfigId.CONFIG4,
            input_log_ref="x",
            root_cause_candidates=[
                RootCauseCandidate(
                    category=Category.FW,
                    summary="verified by logs" if is_stage_two else "cfg-only guess",
                )
            ],
            recommended_actions=[],
            confidence=0.85 if is_stage_two else 0.6,
            suspected_node_ids=["fw-01"],
            suspected_node_findings=[
                SuspectedNodeFinding(node_id="fw-01", severity="primary")
            ],
            metrics=Metrics(tokens_in=400, tokens_out=100, latency_ms_total=2000),
            delegation_rounds=2,
        )
        yield RealStreamEvent("final", {"result": ar.model_dump(mode="json")})

    return fake_run_rally_stream


async def _collect_events(gen):
    out = []
    async for ev in gen:
        out.append(ev)
    return out


def test_run_two_stage_stream_abort_emits_stage_one_only(monkeypatch):
    """Stage 1 完了直後に abort を選んだら final が emit され、stage_outputs は 1 件のみ。"""
    import asyncio
    from log_analyzer.rally_two_stage import run_two_stage_stream

    call_count: dict = {}
    monkeypatch.setattr(
        "log_analyzer.rally_two_stage.run_rally_stream",
        _make_fake_rally(call_count),
    )

    async def abort_decision() -> dict:
        return {"action": "abort"}

    events = asyncio.run(_collect_events(
        run_two_stage_stream(
            stage_one_log_text="dummy stage1",
            stage_two_log_text_template=lambda so: "stage2 should not be reached",
            log_ref="test",
            topology_context={"nodes": [{"id": "fw-01"}], "links": []},
            decision_waiter=abort_decision,
        )
    ))

    kinds = [e.kind for e in events]
    assert "stage_one_start" in kinds
    assert "stage_one_complete" in kinds
    assert "user_decision" in kinds
    assert "stage_two_start" not in kinds
    assert "final" in kinds
    final = next(e for e in events if e.kind == "final")
    result = final.data["result"]
    assert len(result["stage_outputs"]) == 1
    assert result["stage_outputs"][0]["stage"] == "config"
    assert call_count["n"] == 1  # Stage 1 のみ走った


def test_run_two_stage_stream_advance_emits_both_stages(monkeypatch):
    import asyncio
    from log_analyzer.rally_two_stage import run_two_stage_stream

    call_count: dict = {}
    monkeypatch.setattr(
        "log_analyzer.rally_two_stage.run_rally_stream",
        _make_fake_rally(call_count, distinguish_stage_two=True),
    )

    async def advance_decision() -> dict:
        return {"action": "advance"}

    events = asyncio.run(_collect_events(
        run_two_stage_stream(
            stage_one_log_text="cfgs only here",
            stage_two_log_text_template=lambda so: _build_stage_one_hypothesis_block(so) + "logs here",
            log_ref="test",
            topology_context={"nodes": [{"id": "fw-01"}], "links": []},
            decision_waiter=advance_decision,
        )
    ))

    kinds = [e.kind for e in events]
    assert "stage_one_complete" in kinds
    assert "stage_two_start" in kinds
    assert "final" in kinds
    assert call_count["n"] == 2  # rally が 2 回呼ばれた

    final = next(e for e in events if e.kind == "final")
    result = final.data["result"]
    assert len(result["stage_outputs"]) == 2
    stages = [s["stage"] for s in result["stage_outputs"]]
    assert stages == ["config", "log"]
    assert result["confidence"] == 0.85
    assert result["metrics"]["tokens_in"] == 800
