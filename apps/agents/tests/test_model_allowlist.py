"""UI でノードごとに選べるモデル（許可リスト）のテスト（docs/plan/slm_support.md §8 / §9-1）。

クラウド（既定）に加えて SLM（Ollama）を選べるようにする。既定モデルは変えない。
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from log_analyzer import api as api_mod
from log_analyzer import pipeline_runner, prompt_slots

_CLOUD = "claude-opus-4-7"
_SLM_26B = "ollama:gemma4:26b-a4b-it-q4_K_M"
_SLM_31B = "ollama:gemma4:31b-it-q4_K_M"
_SLM_LOCAL = "ollama:gemma4:e4b"  # 手元の配線確認用
_EXPECTED = [_CLOUD, _SLM_26B, _SLM_31B, _SLM_LOCAL]


def test_overridable_slots_offer_cloud_then_slm_models():
    for sid in ("orchestrator", "fw_monitor", "integrator"):
        slot = next(s for s in prompt_slots.get_slots("config4") if s["slot_id"] == sid)
        assert slot["allowed_models"] == _EXPECTED
        assert slot["default_model"] == _CLOUD  # 既定はクラウドのまま


def test_fixed_slot_stays_non_overridable():
    """構成3 の並列分析段は複数モデル並列が設計の本質なのでモデル上書き不可のまま。"""
    slot = next(s for s in prompt_slots.get_slots("config3") if s["slot_id"] == "analyze")
    assert slot["allowed_models"] == []


def test_pipeline_nodes_allow_the_same_models():
    assert pipeline_runner.ALLOWED_MODELS == _EXPECTED


def test_api_accepts_slm_override_and_rejects_unknown_model():
    api_mod._validate_overrides("config4", {}, {"fw_monitor": _SLM_31B})  # 例外にならない
    with pytest.raises(HTTPException):
        api_mod._validate_overrides("config4", {}, {"fw_monitor": "ollama:unknown"})


def test_user_config_with_all_slm_models_can_be_saved():
    """運用の流れ：構成4 を元に全ノード SLM のユーザー定義構成を保存し、構成・ログ解析で選ぶ。"""
    from fastapi.testclient import TestClient

    slots = [s["slot_id"] for s in prompt_slots.get_slots("config4") if s["allowed_models"]]
    r = TestClient(api_mod.app).post("/api/configs/saved", json={
        "name": "all-local-26b",
        "base_config": "config4",
        "overrides": {},
        "model_overrides": {sid: _SLM_26B for sid in slots},
    })

    assert r.status_code == 200, r.text
    assert r.json()["model_overrides"]["integrator"] == _SLM_26B


# ─── 画面の「推論モデル」選択欄（GET /api/inference-models）──────────────


def test_inference_models_marks_what_the_current_endpoint_has(monkeypatch):
    """Runpod のトンネルにつないでいれば 26B / 31B だけが使える（手元用の e4b は無い）。"""
    from fastapi.testclient import TestClient

    from log_analyzer import llm_client

    monkeypatch.setattr(llm_client, "ollama_base_url", lambda: "http://localhost:11435")
    monkeypatch.setattr(llm_client, "ollama_models", lambda timeout=5: [_SLM_26B, _SLM_31B])

    data = TestClient(api_mod.app).get("/api/inference-models").json()

    assert data["ollama_base_url"] == "http://localhost:11435"
    assert data["ollama_reachable"] is True
    assert data["models"] == [
        {"id": _CLOUD, "available": True},
        {"id": _SLM_26B, "available": True},
        {"id": _SLM_31B, "available": True},
        {"id": _SLM_LOCAL, "available": False},
    ]
    # 選んだモデルを適用する構成4 のノード（モデルを替えられるもの全部）。方針プランナーも含める
    # （all-local-* は評価役以外の全ノードを SLM にする。docs/plan/slm_support.md §9-2）
    assert data["slots"] == [
        "planner",
        "orchestrator", "fw_monitor", "routing_monitor", "app_monitor", "dns_monitor", "sec_monitor",
        "integrator",
    ]


def test_inference_models_when_ollama_is_unreachable(monkeypatch):
    from fastapi.testclient import TestClient

    from log_analyzer import llm_client

    def down(timeout=5):
        raise OSError("接続できません")

    monkeypatch.setattr(llm_client, "ollama_models", down)

    data = TestClient(api_mod.app).get("/api/inference-models").json()

    assert data["ollama_reachable"] is False
    assert "接続できません" in data["ollama_error"]
    assert [m["available"] for m in data["models"]] == [True, False, False, False]


def test_what_the_ui_sends_for_every_inference_slot_passes_validation(monkeypatch):
    """画面は /api/inference-models の slots 全部に選んだモデルを入れて送る。それが実行前の検証を通ること。

    方針プランナー（planner）は slot 定義に無いが、モデルだけは替えられる（プロンプトの上書きは不可）。
    """
    from fastapi.testclient import TestClient

    from log_analyzer import llm_client

    monkeypatch.setattr(llm_client, "ollama_models", lambda timeout=5: [_SLM_26B])
    slots = TestClient(api_mod.app).get("/api/inference-models").json()["slots"]

    api_mod._validate_overrides("config4", {}, {sid: _SLM_26B for sid in slots})  # 例外にならない
    with pytest.raises(HTTPException):
        api_mod._validate_overrides("config4", {}, {"planner": "ollama:unknown"})
    with pytest.raises(HTTPException):
        api_mod._validate_overrides("config4", {"planner": "独自プロンプト"}, {})
