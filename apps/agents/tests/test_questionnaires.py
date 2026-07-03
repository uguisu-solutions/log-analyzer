"""問診票機能 (Phase B) のスモークテスト。

CRUD エンドポイント + デフォルトテンプレ自動投入 + log_text への注入
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from log_analyzer import api as api_mod
from log_analyzer import storage
from log_analyzer.api import _build_questionnaire_block, _build_topology_log_text


@pytest.fixture
def isolated_db(tmp_path: Path, monkeypatch):
    """各テストで独立した SQLite を使う (デフォルトテンプレ投入を含む)。"""
    monkeypatch.setattr(storage, "_DB_PATH", tmp_path / "test.sqlite3")
    storage.init_db()
    yield


def test_default_questionnaire_seeded(isolated_db):
    """init_db で新問診票 (障害状況問診票) の default テンプレが自動投入されること。"""
    rows = storage.list_questionnaires()
    assert len(rows) == 1
    assert rows[0]["name"] == "default"
    items = rows[0]["items"]
    assert len(items) == 9
    keys = {it["key"] for it in items}
    assert "事象" in keys
    assert "まだ確認していないこと" in keys
    assert "いま一番迷っていること" in keys
    # 事象は先頭かつ必須
    assert items[0]["key"] == "事象"
    assert items[0]["required"] is True
    # 必須は 事象 / いつから / 影響を受ける対象 の 3 つ
    required = {it["key"] for it in items if it["required"]}
    assert required == {"事象", "いつから（その前は正常だったか）", "影響を受ける対象と、受けない対象"}


def test_default_questionnaire_migration_upgrades_old(isolated_db):
    """旧デフォルト構造の default に init_db を再実行すると新問診票へ置き換わる。"""
    import json
    with storage._connect() as conn:
        old_items = [
            {"key": "event", "label": "概要", "type": "textarea",
             "options": [], "placeholder": "", "required": True},
            {"key": "occurred_at", "label": "x", "type": "text",
             "options": [], "placeholder": "", "required": False},
        ]
        conn.execute(
            "UPDATE questionnaire_templates SET items_json = ? WHERE name = 'default'",
            (json.dumps(old_items, ensure_ascii=False),),
        )
        conn.commit()
    storage.init_db()  # 旧→新へアップグレード
    default = next(r for r in storage.list_questionnaires() if r["name"] == "default")
    keys = [it["key"] for it in default["items"]]
    assert keys[0] == "事象"
    assert "いま一番迷っていること" in keys
    assert "occurred_at" not in keys  # 旧項目は置き換えられている


def test_default_questionnaire_custom_not_overwritten(isolated_db):
    """旧マーカーキーを含まない独自 default は上書きしない。"""
    import json
    with storage._connect() as conn:
        custom = [{"key": "my_custom", "label": "x", "type": "text",
                   "options": [], "placeholder": "", "required": False}]
        conn.execute(
            "UPDATE questionnaire_templates SET items_json = ? WHERE name = 'default'",
            (json.dumps(custom, ensure_ascii=False),),
        )
        conn.commit()
    storage.init_db()
    default = next(r for r in storage.list_questionnaires() if r["name"] == "default")
    assert [it["key"] for it in default["items"]] == ["my_custom"]


def test_default_seed_idempotent(isolated_db):
    """init_db を 2 回呼んでもデフォルトテンプレは 1 件のまま。"""
    storage.init_db()
    storage.init_db()
    assert len(storage.list_questionnaires()) == 1


def test_questionnaire_crud(isolated_db):
    """新規テンプレ作成 → 取得 → 更新 → 削除。"""
    created = storage.create_questionnaire(
        name="aws-incident-template",
        description="クラウド障害用",
        items=[
            {"key": "region", "label": "リージョン", "type": "text", "options": [], "placeholder": "", "required": True},
        ],
    )
    qid = created["id"]
    assert qid > 0
    assert created["name"] == "aws-incident-template"

    got = storage.get_questionnaire(qid)
    assert got is not None and got["items"][0]["key"] == "region"

    updated = storage.update_questionnaire(
        qid, description="更新後",
        items=[
            {"key": "region", "label": "リージョン", "type": "text", "options": [], "placeholder": "", "required": True},
            {"key": "service", "label": "サービス名", "type": "text", "options": [], "placeholder": "", "required": False},
        ],
    )
    assert updated is not None and len(updated["items"]) == 2
    assert updated["description"] == "更新後"

    assert storage.delete_questionnaire(qid) is True
    assert storage.get_questionnaire(qid) is None


def test_default_template_cannot_be_deleted(isolated_db):
    """default テンプレは削除拒否 (ValueError)。"""
    default = storage.list_questionnaires()[0]
    with pytest.raises(ValueError, match="default"):
        storage.delete_questionnaire(default["id"])


def test_build_questionnaire_block_basic():
    text = _build_questionnaire_block({
        "symptom_onset": "2026-05-26 09:00 頃",
        "scope": "特定ユーザー",
        "free_notes": "",  # 空はスキップ
    })
    assert "## 問診票回答" in text
    assert "symptom_onset" in text
    assert "2026-05-26 09:00 頃" in text
    assert "scope" in text
    # 空の値は省略
    assert "free_notes" not in text


def test_build_questionnaire_block_empty():
    assert _build_questionnaire_block({}) == ""
    assert _build_questionnaire_block(None) == ""
    # 全部空の辞書も "" 扱い
    assert _build_questionnaire_block({"k": "", "j": "  "}) == ""


def test_build_topology_log_text_prepends_questionnaire():
    """問診票回答が log_text 先頭にくっつくこと。"""
    topology = {"nodes": [{"id": "fw-01"}], "links": []}
    node_logs = {"fw-01": [{"name": "x.log", "content": "deny ..."}]}
    text, _ = _build_topology_log_text(
        topology, node_logs, None,
        questionnaire_answers={"symptom_onset": "09:00", "scope": "全ユーザー"},
    )
    assert text.startswith("## 問診票回答")
    # トポロジー要約より前に問診票が来る
    assert text.index("## 問診票回答") < text.index("## トポロジー要約")
    assert "09:00" in text
    assert "全ユーザー" in text


def test_build_topology_log_text_omits_questionnaire_when_empty():
    topology = {"nodes": [{"id": "fw-01"}], "links": []}
    text, _ = _build_topology_log_text(topology, {}, None, questionnaire_answers={})
    assert not text.startswith("## 問診票回答")


def test_questionnaire_api_list(isolated_db):
    client = TestClient(api_mod.app)
    r = client.get("/api/questionnaires")
    assert r.status_code == 200
    data = r.json()
    assert len(data["templates"]) == 1
    assert data["templates"][0]["name"] == "default"


def test_questionnaire_api_crud(isolated_db):
    client = TestClient(api_mod.app)
    # 作成
    r = client.post("/api/questionnaires", json={
        "name": "custom-1",
        "description": "テスト用",
        "items": [
            {"key": "k1", "label": "Q1", "type": "text", "options": [], "placeholder": "", "required": False},
        ],
    })
    assert r.status_code == 200, r.text
    created = r.json()
    qid = created["id"]

    # 取得
    r = client.get(f"/api/questionnaires/{qid}")
    assert r.status_code == 200
    assert r.json()["items"][0]["key"] == "k1"

    # 更新 (items 差し替え)
    r = client.put(f"/api/questionnaires/{qid}", json={
        "items": [
            {"key": "k1", "label": "Q1", "type": "text", "options": [], "placeholder": "", "required": False},
            {"key": "k2", "label": "Q2", "type": "choice", "options": ["a", "b"], "placeholder": "", "required": False},
        ],
    })
    assert r.status_code == 200
    assert len(r.json()["items"]) == 2

    # 削除
    r = client.delete(f"/api/questionnaires/{qid}")
    assert r.status_code == 200
    assert r.json()["deleted"] == qid


def test_questionnaire_api_default_protect_delete(isolated_db):
    client = TestClient(api_mod.app)
    default = client.get("/api/questionnaires").json()["templates"][0]
    r = client.delete(f"/api/questionnaires/{default['id']}")
    assert r.status_code == 400
    assert "default" in r.json()["detail"]
