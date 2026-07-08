"""トポロジー解析エンドポイント (`POST /api/runs/topology-stream`) のスモークテスト。

- リクエストペイロードのバリデーション境界（topology 空、node_logs 空、base_config 非対応）
- `_build_topology_log_text` のフォーマット検証（ヘッダ / ノードブロック / 未定義 ID 警告）
- `suspected_node_ids` フィルタの正しさ（_build_analysis_result）

実際の LLM 呼び出しはモックする — schema / バリデーション層のみを検証する。
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from log_analyzer import api as api_mod
from log_analyzer.api import _build_topology_log_text
from log_analyzer.rally_agent import _build_analysis_result


def test_build_topology_log_text_multi_attachments_per_node():
    """1 ノードに複数のログ + 複数の設定ファイルを添付できる。"""
    topology = {
        "nodes": [
            {"id": "fw-01", "type": "FW", "label": "コア FW", "ip": "10.0.0.1"},
            {"id": "sw-l2-01", "type": "L2", "label": "配下スイッチ", "ip": "10.0.0.2"},
        ],
        "links": [{"source": "fw-01", "target": "sw-l2-01"}],
    }
    node_logs = {
        "fw-01": [
            {"name": "fw-syslog.log", "content": "DENY src=1.2.3.4 dst=10.0.0.1\n"},
            {"name": "fw-deny.log", "content": "deny rule=default\n"},
        ],
        "sw-l2-01": [{"name": "", "content": ""}],  # 空はスキップ
    }
    node_configs = {
        "fw-01": [
            {"name": "fw-policy.conf", "content": "rule allow lb-to-app-02\n"},
        ],
    }
    text, nodes = _build_topology_log_text(topology, node_logs, node_configs)
    assert "## トポロジー要約" in text
    assert "id=fw-01" in text
    assert "id=sw-l2-01" in text
    assert "fw-01 → sw-l2-01" in text
    # fw-01 のブロックに 2 つのログと 1 つの設定が含まれる
    assert "=== NODE: fw-01" in text
    assert "[ログ] fw-syslog.log:" in text
    assert "DENY src=1.2.3.4" in text
    assert "[ログ] fw-deny.log:" in text
    assert "deny rule=default" in text
    assert "[設定] fw-policy.conf:" in text
    assert "rule allow lb-to-app-02" in text
    # sw-l2-01 は中身が空だったので NODE ヘッダごとスキップ
    assert "=== NODE: sw-l2-01" not in text
    assert [n["id"] for n in nodes] == ["fw-01", "sw-l2-01"]


def test_build_topology_log_text_accepts_string_legacy_shape():
    """旧形式 (str) もフォールバックで受け入れる。"""
    topology = {"nodes": [{"id": "fw-01"}], "links": []}
    node_logs = {"fw-01": "DENY src=1.2.3.4\n"}  # 旧 str 形式
    text, _ = _build_topology_log_text(topology, node_logs, None)
    assert "DENY src=1.2.3.4" in text
    assert "=== NODE: fw-01" in text


def test_build_topology_log_text_unnamed_attachment_gets_index_label():
    """name 空のアタッチメントは log_1 / config_1 ... の通番ラベルが振られる。"""
    topology = {"nodes": [{"id": "fw-01"}], "links": []}
    node_logs = {"fw-01": [
        {"name": "", "content": "first"},
        {"name": "", "content": "second"},
    ]}
    node_configs = {"fw-01": [{"name": "", "content": "cfg_a"}]}
    text, _ = _build_topology_log_text(topology, node_logs, node_configs)
    assert "[ログ] log_1:" in text
    assert "[ログ] log_2:" in text
    assert "[設定] config_1:" in text


def test_build_topology_log_text_warns_orphan_attachments():
    topology = {"nodes": [{"id": "fw-01"}], "links": []}
    node_logs = {"fw-01": [{"name": "ok.log", "content": "ok"}],
                 "unknown-id": [{"name": "stray", "content": "should-warn"}]}
    node_configs = {"ghost-node": [{"name": "stray.conf", "content": "x"}]}
    text, _ = _build_topology_log_text(topology, node_logs, node_configs)
    assert "未定義の node_id" in text
    assert "unknown-id" in text
    assert "ghost-node" in text


def test_build_topology_log_text_empty_nodes():
    text, nodes = _build_topology_log_text({"nodes": [], "links": []}, {}, {})
    assert nodes == []
    assert "(ノード定義なし)" in text


def test_questionnaire_block_renders_confidence():
    topo = {"nodes": [{"id": "n1", "label": "N1"}], "links": []}
    text, _ = _build_topology_log_text(
        topo, {"n1": "log"}, None,
        questionnaire_answers={"事象": "無線が切れる", "原因の見当": "配線かも"},
        questionnaire_confidences={"事象": "高", "原因の見当": "低"},
    )
    # 確信度が併記される
    assert "**事象**（確信度: 高）: 無線が切れる" in text
    assert "**原因の見当**（確信度: 低）: 配線かも" in text
    # 確信度の使い方ガイド (低=検証対象) が入る
    assert "確信度" in text and "検証対象" in text


def test_questionnaire_block_omits_confidence_when_absent():
    topo = {"nodes": [{"id": "n1", "label": "N1"}], "links": []}
    text, _ = _build_topology_log_text(
        topo, {"n1": "log"}, None,
        questionnaire_answers={"事象": "無線が切れる"},
    )
    assert "**事象**: 無線が切れる" in text
    assert "確信度" not in text


def test_build_analysis_result_filters_suspected_node_ids_to_known():
    # 旧フォーマット (suspected_node_ids 単体) でもフォールバックが効くこと
    integrator_result = {
        "root_cause_candidates": [],
        "recommended_actions": [],
        "confidence": 0.5,
        "suspected_node_ids": ["fw-01", "ghost-node", "sw-l2-01", "fw-01"],
    }
    result = _build_analysis_result(
        log_ref="test",
        trace_id="trace-x",
        integrator_result=integrator_result,
        token_log=[],
        delegation_history=[],
        rally_round=1,
        rally_max_rounds=3,
        wall_ms=100,
        topology_node_ids=["fw-01", "sw-l2-01", "app-01"],
    )
    # 既知 ID のみ、重複は排除、順序は LLM 出力に忠実
    assert result.suspected_node_ids == ["fw-01", "sw-l2-01"]
    # フォールバックなので findings は summary/severity が空
    assert [f.node_id for f in result.suspected_node_findings] == ["fw-01", "sw-l2-01"]
    assert all(f.summary == "" and f.severity == "" for f in result.suspected_node_findings)


def test_build_analysis_result_parses_structured_suspected_nodes():
    """新フォーマット suspected_nodes = [{node_id, summary, severity}] を優先採用する。"""
    integrator_result = {
        "root_cause_candidates": [],
        "recommended_actions": [],
        "confidence": 0.5,
        "suspected_nodes": [
            {"node_id": "fw-01", "summary": "policy reload で lb-to-app-01 が欠落", "severity": "primary"},
            {"node_id": "lb-01", "summary": "app-01 のヘルスチェック連続失敗", "severity": "secondary"},
            {"node_id": "ghost-node", "summary": "存在しない", "severity": "primary"},
            {"node_id": "fw-01", "summary": "重複", "severity": "info"},  # 重複は無視
        ],
    }
    result = _build_analysis_result(
        log_ref="test",
        trace_id="trace-x",
        integrator_result=integrator_result,
        token_log=[],
        delegation_history=[],
        rally_round=1,
        rally_max_rounds=3,
        wall_ms=100,
        topology_node_ids=["fw-01", "lb-01", "app-01"],
    )
    assert result.suspected_node_ids == ["fw-01", "lb-01"]
    findings = result.suspected_node_findings
    assert [f.node_id for f in findings] == ["fw-01", "lb-01"]
    assert findings[0].severity == "primary"
    assert findings[0].summary == "policy reload で lb-to-app-01 が欠落"
    assert findings[1].severity == "secondary"


def test_build_analysis_result_accepts_alias_keys_and_case():
    """LLM が `id` キーや大文字 severity を返してきても拾えること。"""
    integrator_result = {
        "root_cause_candidates": [],
        "recommended_actions": [],
        "confidence": 0.5,
        "suspected_nodes": [
            {"id": " fw-01 ", "summary": "  primary cause  ", "severity": " PRIMARY "},
            {"nodeId": "lb-01", "description": "downstream symptom", "severity": "Secondary"},
        ],
    }
    result = _build_analysis_result(
        log_ref="test",
        trace_id="trace-x",
        integrator_result=integrator_result,
        token_log=[],
        delegation_history=[],
        rally_round=1,
        rally_max_rounds=3,
        wall_ms=100,
        topology_node_ids=["fw-01", "lb-01"],
    )
    assert result.suspected_node_ids == ["fw-01", "lb-01"]
    f = result.suspected_node_findings
    assert f[0].node_id == "fw-01" and f[0].severity == "primary"
    assert f[0].summary == "primary cause"  # 前後空白除去
    # description キーも summary にフォールバック
    assert f[1].summary == "downstream symptom"
    assert f[1].severity == "secondary"


def test_build_analysis_result_rejects_invalid_severity():
    integrator_result = {
        "root_cause_candidates": [],
        "recommended_actions": [],
        "confidence": 0.5,
        "suspected_nodes": [
            {"node_id": "fw-01", "summary": "x", "severity": "CRITICAL"},  # 規定外
        ],
    }
    result = _build_analysis_result(
        log_ref="test",
        trace_id="trace-x",
        integrator_result=integrator_result,
        token_log=[],
        delegation_history=[],
        rally_round=1,
        rally_max_rounds=3,
        wall_ms=100,
        topology_node_ids=["fw-01"],
    )
    # ノード自体は採用、severity は空文字に正規化
    assert result.suspected_node_ids == ["fw-01"]
    assert result.suspected_node_findings[0].severity == ""


def test_build_analysis_result_no_topology_returns_empty_suspected():
    integrator_result = {
        "root_cause_candidates": [],
        "recommended_actions": [],
        "confidence": 0.5,
        "suspected_node_ids": ["fw-01"],
    }
    result = _build_analysis_result(
        log_ref="test",
        trace_id="trace-x",
        integrator_result=integrator_result,
        token_log=[],
        delegation_history=[],
        rally_round=1,
        rally_max_rounds=3,
        wall_ms=100,
        topology_node_ids=None,
    )
    # topology が無ければ suspected_node_ids は捨てる（誤ハイライト防止）
    assert result.suspected_node_ids == []
    assert result.suspected_node_findings == []


def test_topology_stream_rejects_non_config4():
    client = TestClient(api_mod.app)
    r = client.post(
        "/api/runs/topology-stream",
        json={
            "config": "config1",
            "topology": {"nodes": [{"id": "n1"}], "links": []},
            "node_logs": {"n1": [{"name": "x.log", "content": "x"}]},
        },
    )
    assert r.status_code == 400
    assert "config4" in r.json()["detail"]


def test_topology_stream_rejects_empty_nodes():
    client = TestClient(api_mod.app)
    r = client.post(
        "/api/runs/topology-stream",
        json={
            "config": "config4",
            "topology": {"nodes": [], "links": []},
            "node_logs": {},
            "node_configs": {},
        },
    )
    assert r.status_code == 400


def test_topology_stream_rejects_when_all_attachments_empty():
    client = TestClient(api_mod.app)
    r = client.post(
        "/api/runs/topology-stream",
        json={
            "config": "config4",
            "topology": {"nodes": [{"id": "fw-01"}], "links": []},
            "node_logs": {"fw-01": [{"name": "x.log", "content": "   "}]},
            "node_configs": {},
        },
    )
    assert r.status_code == 400
    # メッセージは "ログまたは設定ファイル" を含む新文言
    assert "ログ" in r.json()["detail"]


def test_topology_stream_accepts_config_only_node():
    """ログがなくても設定ファイルだけで実行リクエストは通る (400 にならない)。

    実行自体は LLM 呼び出しがあるのでこのテストでは検証しないが、
    バリデーション段階で 400 にならないことを確認する。
    """
    client = TestClient(api_mod.app)
    # 不正な config を指定すれば LLM 呼び出し前で 404/400 で止まる。
    # ここでは config="user:99999" (存在しない) を使ってバリデーション後・実行前の
    # フローまで進ませる。
    r = client.post(
        "/api/runs/topology-stream",
        json={
            "config": "user:99999",
            "topology": {"nodes": [{"id": "fw-01"}], "links": []},
            "node_logs": {},
            "node_configs": {
                "fw-01": [{"name": "fw.conf", "content": "rule x permit"}],
            },
        },
    )
    # 存在しない saved_config なので 404、ログ・config 不在の 400 ではないことを確認
    assert r.status_code == 404
