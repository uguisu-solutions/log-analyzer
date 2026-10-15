"""解析履歴 (analysis_history) エンドポイントのテスト。

保存 → 一覧 → 個別取得 → 削除の往復、重複 run_id の no-op、result からの
サマリ抽出、一覧レスポンスが軽量 (request/result を含まない) ことを検証する。
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from log_analyzer import api as api_mod


def _sample_payload(run_id: str = "run-abc") -> dict:
    return {
        "run_id": run_id,
        "kind": "config-log",
        "config_id": "config4",
        "analysis_mode": "two_stage",
        "single_source": "both",
        "stage_order": "config_log",
        "rally_max_rounds": 3,
        "view_mode": "chat",
        "questionnaire_answers": {"symptom_onset": "09:00 頃"},
        "topology": {
            "image": "data:image/png;base64,AAAA",
            "imageWidth": 800,
            "imageHeight": 580,
            "nodes": [{"id": "fw-01", "type": "FW", "label": "", "ip": "10.1.1.1", "x": 0.1, "y": 0.1, "w": 0.2, "h": 0.1}],
            "links": [],
        },
        "result": {
            "schema_version": "v0.2",
            "trace_id": "trace-xyz",
            "config_id": "config4",
            "confidence": 0.83,
            "root_cause_candidates": [{"category": "fw", "summary": "ACL コメントアウト", "evidence": []}],
            "recommended_actions": [],
            "metrics": {"tokens_in": 1200, "tokens_out": 400, "latency_ms_total": 7000},
            "suspected_node_ids": ["fw-01"],
            "suspected_node_findings": [{"node_id": "fw-01", "summary": "原因", "severity": "primary"}],
            "stage_outputs": [],
            "round_metrics": [],
        },
    }


def test_analysis_history_roundtrip():
    client = TestClient(api_mod.app)

    # 保存
    r = client.post("/api/analysis-history", json=_sample_payload("run-rt-1"))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["created"] is True
    entry_id = body["id"]
    assert entry_id > 0

    # 一覧 (サマリのみ・重い JSON を含まない)
    r = client.get("/api/analysis-history")
    assert r.status_code == 200
    data = r.json()
    assert data["total"] >= 1
    found = next((e for e in data["entries"] if e["id"] == entry_id), None)
    assert found is not None
    # result からサマリが抽出されている
    assert abs(found["confidence"] - 0.83) < 1e-6
    assert found["tokens_in"] == 1200
    assert found["tokens_out"] == 400
    assert found["latency_ms"] == 7000
    assert found["top_category"] == "fw"
    assert found["top_summary"] == "ACL コメントアウト"
    assert found["trace_id"] == "trace-xyz"
    # 一覧には request_json / result_json (および request/result) が含まれない
    assert "request" not in found and "result" not in found
    assert "request_json" not in found and "result_json" not in found

    # 個別取得 (完全再現用 request/result 込み)
    r = client.get(f"/api/analysis-history/{entry_id}")
    assert r.status_code == 200
    detail = r.json()
    assert detail["request"]["topology"]["nodes"][0]["id"] == "fw-01"
    assert detail["request"]["topology"]["image"].startswith("data:image/png")
    assert detail["request"]["questionnaire_answers"]["symptom_onset"] == "09:00 頃"
    assert detail["result"]["confidence"] == 0.83
    assert detail["result"]["suspected_node_findings"][0]["node_id"] == "fw-01"

    # 削除
    r = client.delete(f"/api/analysis-history/{entry_id}")
    assert r.status_code == 200
    assert r.json()["deleted"] == entry_id
    # 削除後は 404
    assert client.get(f"/api/analysis-history/{entry_id}").status_code == 404


def test_analysis_history_duplicate_run_id_is_noop():
    client = TestClient(api_mod.app)
    payload = _sample_payload("run-dup-1")
    r1 = client.post("/api/analysis-history", json=payload)
    assert r1.json()["created"] is True
    id1 = r1.json()["id"]
    # 同一 run_id を再 POST → no-op (created=False, 同じ id)
    r2 = client.post("/api/analysis-history", json=payload)
    assert r2.status_code == 200
    assert r2.json()["created"] is False
    assert r2.json()["id"] == id1
    # 後始末
    client.delete(f"/api/analysis-history/{id1}")


def test_analysis_history_filter_by_mode():
    client = TestClient(api_mod.app)
    client.post("/api/analysis-history", json=_sample_payload("run-flt-single") | {"analysis_mode": "single", "single_source": "config"})
    client.post("/api/analysis-history", json=_sample_payload("run-flt-two"))  # two_stage

    r = client.get("/api/analysis-history", params={"analysis_mode": "single"})
    assert r.status_code == 200
    modes = {e["analysis_mode"] for e in r.json()["entries"]}
    assert modes <= {"single"}  # single のみ
    # 後始末
    for e in client.get("/api/analysis-history").json()["entries"]:
        if e["run_id"] in ("run-flt-single", "run-flt-two"):
            client.delete(f"/api/analysis-history/{e['id']}")


def test_get_missing_analysis_history_404():
    client = TestClient(api_mod.app)
    assert client.get("/api/analysis-history/999999").status_code == 404
    assert client.delete("/api/analysis-history/999999").status_code == 404


def test_reanalysis_lineage_and_input_files():
    """再解析の系譜 (parent/root/revision) と対象ファイル名が保存・取得できる。

    設計: docs/plan/reanalysis.md
    """
    client = TestClient(api_mod.app)

    # 大元 (初回解析): 系譜未指定 → root=自分, revision=0
    root_payload = _sample_payload("run-root-1") | {
        "input_files": ["fw-syslog.log", "fw-policy.conf"],
    }
    r = client.post("/api/analysis-history", json=root_payload)
    assert r.status_code == 200, r.text
    root_id = r.json()["id"]

    # 再解析 (rev1): root_run_id / parent_run_id / revision を明示
    child_payload = _sample_payload("run-child-1") | {
        "parent_run_id": "run-root-1",
        "root_run_id": "run-root-1",
        "revision": 1,
        "input_files": ["extra.log"],
    }
    r = client.post("/api/analysis-history", json=child_payload)
    assert r.status_code == 200, r.text
    child_id = r.json()["id"]

    # 一覧サマリに系譜列が出る
    entries = {e["run_id"]: e for e in client.get("/api/analysis-history").json()["entries"]}
    root_e, child_e = entries["run-root-1"], entries["run-child-1"]
    assert root_e["root_run_id"] == "run-root-1"  # 大元は自分自身が root
    assert root_e["parent_run_id"] is None
    assert root_e["revision"] == 0
    assert child_e["parent_run_id"] == "run-root-1"
    assert child_e["root_run_id"] == "run-root-1"
    assert child_e["revision"] == 1

    # root_run_id で系譜 (全版) を取得できる
    r = client.get("/api/analysis-history", params={"root_run_id": "run-root-1"})
    assert r.status_code == 200
    run_ids = {e["run_id"] for e in r.json()["entries"]}
    assert run_ids == {"run-root-1", "run-child-1"}

    # 個別取得で input_files (ファイル名のみ) が残る
    detail = client.get(f"/api/analysis-history/{root_id}").json()
    assert detail["request"]["input_files"] == ["fw-syslog.log", "fw-policy.conf"]

    # 後始末
    client.delete(f"/api/analysis-history/{root_id}")
    client.delete(f"/api/analysis-history/{child_id}")


# ─── モデル構成と入力の指紋（docs/plan/slm_support.md §9-3 / §9-6）──────────


def _with_models(payload: dict, model: str) -> dict:
    """実行グラフに orchestrator / fw 監視 / integrator のモデル実績を入れる。"""
    result = dict(payload["result"])
    result["execution_graph_nodes"] = [
        {"id": nid, "label": model, "role": nid, "model": model}
        for nid in ("orchestrator", "fw_monitor", "integrator")
    ]
    return {**payload, "result": result}


def test_history_records_model_profile_and_input_fingerprint():
    """同じ条件をクラウドと SLM で 1 回ずつ実行すると、指紋が同じでプロファイルが違う。"""
    client = TestClient(api_mod.app)
    slm = "ollama:gemma4:26b-a4b-it-q4_K_M"
    client.post("/api/analysis-history", json=_with_models(_sample_payload("run-cloud"), "claude-opus-4-7"))
    client.post("/api/analysis-history", json=_with_models(_sample_payload("run-slm"), slm))

    entries = {e["run_id"]: e for e in client.get("/api/analysis-history").json()["entries"]}
    cloud, local = entries["run-cloud"], entries["run-slm"]

    assert cloud["model_profile"] == "cloud"
    assert local["model_profile"] == f"local:{slm[len('ollama:'):]}"
    assert local["models"] == {"orchestrator": slm, "fw_monitor": slm, "integrator": slm}
    assert cloud["input_fingerprint"] == local["input_fingerprint"]
    assert cloud["input_fingerprint"]  # 空ではない


def test_history_fingerprint_differs_when_conditions_differ():
    client = TestClient(api_mod.app)
    a = _sample_payload("run-fp-a")
    b = _sample_payload("run-fp-b") | {"questionnaire_answers": {"symptom_onset": "10:00 頃"}}
    client.post("/api/analysis-history", json=a)
    client.post("/api/analysis-history", json=b)

    entries = {e["run_id"]: e for e in client.get("/api/analysis-history").json()["entries"]}
    assert entries["run-fp-a"]["input_fingerprint"] != entries["run-fp-b"]["input_fingerprint"]


def test_init_db_adds_profile_columns_to_existing_history():
    """列追加前の DB に後付けでき、既存の行は保持されて「判別不能（空）」になる。"""
    from log_analyzer import storage

    with storage._connect() as conn:
        conn.execute("DROP TABLE analysis_history")
        conn.execute(
            "CREATE TABLE analysis_history ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, created_at TEXT NOT NULL,"
            " kind TEXT NOT NULL DEFAULT 'config-log', config_id TEXT NOT NULL, analysis_mode TEXT,"
            " single_source TEXT, stage_order TEXT, title TEXT, confidence REAL, tokens_in INTEGER,"
            " tokens_out INTEGER, latency_ms INTEGER, top_category TEXT, top_summary TEXT,"
            " trace_id TEXT, request_json TEXT NOT NULL, result_json TEXT NOT NULL,"
            " parent_run_id TEXT, root_run_id TEXT, revision INTEGER NOT NULL DEFAULT 0)"
        )
        conn.execute(
            "INSERT INTO analysis_history (run_id, created_at, config_id, request_json, result_json,"
            " root_run_id) VALUES ('old-run', '2026-09-01T00:00:00', 'config4', '{}', '{}', 'old-run')"
        )
        conn.commit()

    storage.init_db()

    rows, total = storage.list_analysis_history()
    assert total == 1
    assert rows[0]["run_id"] == "old-run"
    assert (rows[0]["model_profile"], rows[0]["models_json"], rows[0]["input_fingerprint"]) == ("", "{}", "")


def _save_user_config(client, name: str, *, model: str | None = None, prompt: str | None = None) -> str:
    slots = ("orchestrator", "fw_monitor", "integrator")
    r = client.post("/api/configs/saved", json={
        "name": name,
        "base_config": "config4",
        "overrides": {"integrator": prompt} if prompt else {},
        "model_overrides": {s: model for s in slots} if model else {},
    })
    assert r.status_code == 200, r.text
    return f"user:{r.json()['id']}"


def test_fingerprint_matches_between_builtin_and_user_config_that_only_changes_models():
    """画面は選んだ構成（config4 / user:N）を config_id に入れて保存する。
    モデルだけを替えたユーザー定義構成は、元の構成と同じ入力として束ねる。"""
    client = TestClient(api_mod.app)
    slm = "ollama:gemma4:26b-a4b-it-q4_K_M"
    user_cfg = _save_user_config(client, "all-local-26b", model=slm)

    client.post("/api/analysis-history", json=_with_models(_sample_payload("run-builtin"), "claude-opus-4-7"))
    client.post("/api/analysis-history",
                json=_with_models(_sample_payload("run-user") | {"config_id": user_cfg}, slm))

    entries = {e["run_id"]: e for e in client.get("/api/analysis-history").json()["entries"]}
    assert entries["run-builtin"]["input_fingerprint"] == entries["run-user"]["input_fingerprint"]


def test_fingerprint_differs_when_user_config_overrides_prompts():
    """プロンプトを上書きした構成は条件が違うので、別の入力として扱う。"""
    client = TestClient(api_mod.app)
    user_cfg = _save_user_config(client, "prompt-tuned", prompt="別の統合プロンプト")

    client.post("/api/analysis-history", json=_sample_payload("run-base"))
    client.post("/api/analysis-history", json=_sample_payload("run-tuned") | {"config_id": user_cfg})

    entries = {e["run_id"]: e for e in client.get("/api/analysis-history").json()["entries"]}
    assert entries["run-base"]["input_fingerprint"] != entries["run-tuned"]["input_fingerprint"]
