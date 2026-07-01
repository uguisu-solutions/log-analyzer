"""解答シナリオ取込 (import_answers) と answer_scenarios ストレージのテスト。"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import openpyxl
import pytest

from log_analyzer import storage

# scripts/import_answers.py を動的ロード (scripts はパッケージ外)
_SPEC = importlib.util.spec_from_file_location(
    "import_answers",
    Path(__file__).resolve().parents[1] / "scripts" / "import_answers.py",
)
import_answers = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(import_answers)  # type: ignore[union-attr]


def _make_xlsx(path: Path) -> None:
    """テストケース2 の構造を模した最小 xlsx を作る。"""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "テストケース2"
    # ヘッダ行 (row4 相当だが位置は問わない)
    ws.append(["ID", "ステータス", "解析方式", "①", "②", "③", "④", "⑤", "⑥", "⑦", "補足"])
    # グループ A (見出し + -01 に D-K あり, -03 は空)
    ws.append(["A.事象A", None, None, None, None, None, None, None, None, None, None])
    ws.append(["A-01", "未実施", "問診票", "trigA", "hypA", "pathA", "decA", "evA", "結論A", "罠A", "補足A"])
    ws.append(["A-03", "未実施", "問診票", None, None, None, None, None, None, None, None])
    # グループ D (プレースホルダ: ⑥結論が空 → 取込対象外)
    ws.append(["D.XXXXX", None, None, None, None, None, None, None, None, None, None])
    ws.append(["D-01", "未実施", "問診票", None, None, None, None, None, None, None, None])
    # グループ B (別事象)
    ws.append(["B.事象B", None, None, None, None, None, None, None, None, None, None])
    ws.append(["B-01", "未実施", "問診票", "trigB", "hypB", "pathB", "decB", "evB", "結論B", "罠B", "補足B"])
    wb.save(path)


def test_parse_scenarios_extracts_groups_with_conclusion(tmp_path: Path):
    xlsx = tmp_path / "cases.xlsx"
    _make_xlsx(xlsx)
    scenarios = import_answers.parse_scenarios(str(xlsx))
    keys = [s["scenario_key"] for s in scenarios]
    # A, B のみ (D は ⑥ 空でスキップ)
    assert keys == ["A", "B"]


def test_parse_scenarios_maps_columns(tmp_path: Path):
    xlsx = tmp_path / "cases.xlsx"
    _make_xlsx(xlsx)
    a = next(s for s in import_answers.parse_scenarios(str(xlsx)) if s["scenario_key"] == "A")
    assert a["title"] == "事象A"
    assert a["trigger"] == "trigA"
    assert a["conclusion"] == "結論A"        # I列 ⑥ = 真因
    assert a["junior_pitfall"] == "罠A"      # J列 ⑦
    assert a["notes"] == "補足A"             # K列


def test_parse_skips_missing_sheet(tmp_path: Path):
    xlsx = tmp_path / "cases.xlsx"
    _make_xlsx(xlsx)
    with pytest.raises(ValueError):
        import_answers.parse_scenarios(str(xlsx), sheet="存在しない")


def test_storage_answer_scenario_roundtrip():
    key = "ZZ_TEST_SCENARIO"
    try:
        storage.init_db()
        saved = storage.upsert_answer_scenario(
            key, source_file="t.xlsx", title="t", conclusion="真因X", junior_pitfall="罠X"
        )
        assert saved["scenario_key"] == key
        assert saved["conclusion"] == "真因X"
        # upsert で上書きされる
        saved2 = storage.upsert_answer_scenario(key, title="t2", conclusion="真因Y")
        assert saved2["conclusion"] == "真因Y"
        assert saved2["junior_pitfall"] == ""  # 未指定は空で上書き
        got = storage.get_answer_scenario(key)
        assert got is not None and got["title"] == "t2"
        assert any(r["scenario_key"] == key for r in storage.list_answer_scenarios())
    finally:
        storage.delete_answer_scenario(key)
        assert storage.get_answer_scenario(key) is None
