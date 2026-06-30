"""ソースインデックス（indexer.py）の単体テスト。"""
from __future__ import annotations

from pathlib import Path

from log_analyzer.source.indexer import (
    build_source_index,
    get_or_build_index,
    load_index,
    save_index,
)


def _make_codebase(root: Path) -> None:
    (root / "app").mkdir(parents=True)
    (root / "app" / "charge.py").write_text(
        "class Payment:\n"
        "    def charge(self, amount):\n"
        "        return amount\n"
        "\n"
        "def helper(x):\n"
        "    return x * 2\n",
        encoding="utf-8",
    )
    (root / "web").mkdir()
    (root / "web" / "api.ts").write_text(
        "export function postCharge(req){ return req }\n"
        "export const fmt = (n) => `${n}`\n"
        "class Client {\n"
        "  send() { return 1 }\n"
        "}\n",
        encoding="utf-8",
    )
    # 除外対象（走査されてはいけない）
    (root / "node_modules").mkdir()
    (root / "node_modules" / "junk.js").write_text("function nope(){}", encoding="utf-8")
    (root / "bundle.min.js").write_text("var a=1", encoding="utf-8")


def test_index_extracts_python_and_ts_symbols(tmp_path: Path):
    _make_codebase(tmp_path)
    idx = build_source_index(tmp_path)
    by_path = {f.path: f for f in idx.files}

    assert "app/charge.py" in by_path
    assert "web/api.ts" in by_path
    py = by_path["app/charge.py"]
    names = {(s.name, s.kind) for s in py.symbols}
    assert ("Payment", "class") in names
    assert ("Payment.charge", "method") in names
    assert ("helper", "function") in names

    ts = by_path["web/api.ts"]
    ts_names = {(s.name, s.kind) for s in ts.symbols}
    assert ("postCharge", "function") in ts_names
    assert ("fmt", "function") in ts_names  # アロー代入
    assert ("Client", "class") in ts_names
    assert ("Client.send", "method") in ts_names


def test_index_excludes_vendored_and_minified(tmp_path: Path):
    _make_codebase(tmp_path)
    idx = build_source_index(tmp_path)
    paths = {f.path for f in idx.files}
    assert not any("node_modules" in p for p in paths)
    assert "bundle.min.js" not in paths


def test_search_ranks_relevant_file_first(tmp_path: Path):
    _make_codebase(tmp_path)
    idx = build_source_index(tmp_path)
    hits = idx.search("charge payment failed")
    assert hits, "検索ヒットが空"
    assert hits[0]["path"] == "app/charge.py"
    matched = {m["name"] for m in hits[0]["matched_symbols"]}
    assert "Payment.charge" in matched


def test_search_lang_filter(tmp_path: Path):
    _make_codebase(tmp_path)
    idx = build_source_index(tmp_path)
    hits = idx.search("charge client", lang="ts")
    assert all(h["language"] in ("typescript", "tsx") for h in hits)


def test_read_slices_symbol(tmp_path: Path):
    _make_codebase(tmp_path)
    idx = build_source_index(tmp_path)
    out = idx.read("app/charge.py", symbol="Payment.charge")
    assert "def charge" in out
    assert "def helper" not in out  # 関数単位スライス


def test_read_method_short_name(tmp_path: Path):
    _make_codebase(tmp_path)
    idx = build_source_index(tmp_path)
    out = idx.read("app/charge.py", symbol="charge")  # 短縮名でも引ける
    assert "def charge" in out


def test_read_truncates_to_max_chars(tmp_path: Path):
    (tmp_path / "big.py").write_text("x = 1\n" * 5000, encoding="utf-8")
    idx = build_source_index(tmp_path)
    out = idx.read("big.py", max_chars=200)
    assert "文字省略" in out
    assert len(out) < 600


def test_read_rejects_outside_path(tmp_path: Path):
    _make_codebase(tmp_path)
    idx = build_source_index(tmp_path)
    out = idx.read("../escape.py")
    assert out.startswith("エラー")


def test_index_cache_roundtrip(tmp_path: Path):
    _make_codebase(tmp_path)
    idx = build_source_index(tmp_path)
    save_index(idx)
    loaded = load_index(tmp_path)
    assert loaded is not None
    assert {f.path for f in loaded.files} == {f.path for f in idx.files}
    # get_or_build はキャッシュを使う
    again = get_or_build_index(tmp_path)
    assert {f.path for f in again.files} == {f.path for f in idx.files}


def test_syntax_error_file_yields_empty_symbols(tmp_path: Path):
    (tmp_path / "broken.py").write_text("def (((:\n", encoding="utf-8")
    idx = build_source_index(tmp_path)
    broken = next(f for f in idx.files if f.path == "broken.py")
    assert broken.symbols == []  # パース失敗でも全体は止まらない
