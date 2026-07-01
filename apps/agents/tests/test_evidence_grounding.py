"""evidence_grounding（証拠グラウンディング検証）のテスト。"""
from __future__ import annotations

from log_analyzer.evidence_grounding import (
    apply_grounding,
    check_grounding,
    extract_atoms,
)


def test_extract_atoms_ids_ip_mac():
    text = "workspace_id=117 の user_id=404 が 192.168.1.5 / B8:20:8E:28:C4:5F で失敗"
    labels = {a.label for a in extract_atoms(text)}
    assert "workspace_id=117" in labels
    assert "user_id=404" in labels
    assert "192.168.1.5" in labels
    assert "b8:20:8e:28:c4:5f" in labels or "B8:20:8E:28:C4:5F" in labels


def test_extract_atoms_ignores_class_names_and_counts():
    """コード名や件数は識別子アトムに含めない（誤検知防止）。"""
    text = "Micro::Case::Result#error が 55 件、undefined method `each' が発生"
    atoms = extract_atoms(text)
    # 55 は id= 形式でないので拾わない
    assert atoms == []


def test_grounded_when_value_in_corpus():
    corpus = "2026-01-01 error workspace_id=117 documentforce ..."
    cands = [{"summary": "workspace_id=117 に集中", "evidence": []}]
    rep = check_grounding(cands, corpus)
    assert rep.total_atoms == 1
    assert rep.grounded == 1
    assert not rep.has_ungrounded


def test_ungrounded_when_value_absent():
    """コーパスに無い id をでっち上げたら未接地として検出。"""
    corpus = "2026-01-01 error workspace_id=117 ..."
    cands = [{"summary": "同一 id=91171 が長時間反復", "evidence": ["id=98807 も多発"]}]
    rep = check_grounding(cands, corpus)
    assert rep.has_ungrounded
    assert "id=91171" in rep.ungrounded
    assert "id=98807" in rep.ungrounded


def test_word_boundary_avoids_false_ground():
    """91171 は 911710 の部分一致では grounded にしない。"""
    corpus = "id=911710 のみ存在する"
    cands = [{"summary": "id=91171 が原因", "evidence": []}]
    rep = check_grounding(cands, corpus)
    assert "id=91171" in rep.ungrounded


def test_apply_grounding_caps_confidence():
    corpus = "workspace_id=117 のみ"
    cands = [{"summary": "id=91171 が主因", "evidence": []}]
    conf, rep = apply_grounding(cands, 0.85, corpus)
    assert rep.has_ungrounded
    assert conf <= 0.6  # 上限が効く


def test_apply_grounding_keeps_confidence_when_all_grounded():
    corpus = "workspace_id=117 documentforce"
    cands = [{"summary": "workspace_id=117 に集中", "evidence": []}]
    conf, rep = apply_grounding(cands, 0.85, corpus)
    assert not rep.has_ungrounded
    assert conf == 0.85  # 調整なし


def test_disabled_returns_empty_report(monkeypatch):
    monkeypatch.setenv("EVIDENCE_GROUNDING_ENABLED", "0")
    corpus = "何もない"
    cands = [{"summary": "id=91171", "evidence": []}]
    conf, rep = apply_grounding(cands, 0.85, corpus)
    assert rep.total_atoms == 0
    assert conf == 0.85  # 無効時は素通し


def test_conf_cap_env_override(monkeypatch):
    monkeypatch.setenv("EVIDENCE_GROUNDING_CONF_CAP", "0.4")
    corpus = "無関係"
    cands = [{"summary": "id=91171", "evidence": []}]
    conf, _ = apply_grounding(cands, 0.9, corpus)
    assert conf <= 0.4
