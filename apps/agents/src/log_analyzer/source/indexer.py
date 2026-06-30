"""ソースコードの決定論インデックス（Phase 1）。

取り込み済みコードベース（``samples/source/<name>/``）を走査し、ファイル一覧と
シンボル（関数・クラス・メソッド）を抽出する。Python は標準ライブラリ ``ast``、
TS/JS は ``tree-sitter`` ＋ ``tree-sitter-language-pack`` の文法で AST から取る。

設計方針（docs/plan/source_code_analysis.md §3 input トークン配慮）:
- **本文はインデックスに保持しない**。署名（path / symbol / 行範囲）だけを持ち、
  本文は ``read()`` 時にディスクから都度読む（オンデマンド前提）。
- ``node_modules`` 等はそもそも走査対象から除外。

tree-sitter の注意:
    ``tree_sitter_language_pack.get_parser`` は非標準バインディングを返すため使わない。
    ``get_language(name)`` ＋ 標準 ``tree_sitter.Parser(lang)`` を使うと標準 API
    （``root_node`` プロパティ / ``node.type`` / ``node.children`` / ``start_point.row``）
    が得られる。
"""
from __future__ import annotations

import ast
import json
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from log_analyzer.schema import SourceFile, SourceSymbol

# ─── 走査対象・除外ルール ─────────────────────────────────────────────

# 1 ファイルあたりの上限（超過は索引対象外）。巨大な生成物・データを弾く。
MAX_FILE_BYTES = 512 * 1024

# ディレクトリ名で除外（vendored / 生成物 / VCS / キャッシュ）
EXCLUDED_DIRS: frozenset[str] = frozenset(
    {
        "node_modules", ".venv", "venv", ".git", "dist", "build", "__pycache__",
        ".ruff_cache", ".pytest_cache", ".mypy_cache", "coverage", ".next",
        ".turbo", ".cache", ".idea", ".vscode", "vendor", "target",
    }
)

# ファイル名（完全一致 / サフィックス）で除外
EXCLUDED_FILENAMES: frozenset[str] = frozenset(
    {"package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock", "uv.lock"}
)
EXCLUDED_SUFFIXES: tuple[str, ...] = (
    ".min.js", ".min.css", ".map", ".lock",
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".webp",
    ".woff", ".woff2", ".ttf", ".eot",
    ".zip", ".gz", ".tar", ".pdf", ".bin", ".so", ".dll", ".dylib", ".pyc",
)

# 拡張子 → 言語。シンボル抽出の対象になるのはここに載る言語のみ。
LANGUAGE_BY_EXT: dict[str, str] = {
    ".py": "python", ".pyi": "python",
    ".ts": "typescript", ".mts": "typescript", ".cts": "typescript",
    ".tsx": "tsx",
    ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript", ".cjs": "javascript",
}

# 言語 → tree-sitter 文法名
_TS_GRAMMAR: dict[str, str] = {
    "typescript": "typescript",
    "tsx": "tsx",
    "javascript": "javascript",
}


def language_for(path: Path) -> str | None:
    return LANGUAGE_BY_EXT.get(path.suffix.lower())


def is_excluded_dir(name: str) -> bool:
    return name in EXCLUDED_DIRS or name.startswith(".")


def is_excluded_file(path: Path) -> bool:
    name = path.name
    if name in EXCLUDED_FILENAMES:
        return True
    low = name.lower()
    return any(low.endswith(suf) for suf in EXCLUDED_SUFFIXES)


# ─── tree-sitter パーサ（言語ごとに 1 個キャッシュ）─────────────────


@lru_cache(maxsize=None)
def _get_parser(grammar: str):
    from tree_sitter import Parser
    from tree_sitter_language_pack import get_language

    return Parser(get_language(grammar))


# ─── シンボル抽出 ─────────────────────────────────────────────────────


def _py_end_line(node: ast.AST) -> int:
    return int(getattr(node, "end_lineno", None) or getattr(node, "lineno", 1))


def _extract_python_symbols(text: str) -> list[SourceSymbol]:
    """Python ソースから関数・クラス・メソッドの署名を抽出する。"""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []

    syms: list[SourceSymbol] = []

    def visit(node: ast.AST, class_name: str | None) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            kind = "method" if class_name else "function"
            name = f"{class_name}.{node.name}" if class_name else node.name
            syms.append(
                SourceSymbol(
                    name=name, kind=kind,
                    start_line=node.lineno, end_line=_py_end_line(node),
                )
            )
            # 入れ子関数（クラス文脈はリセット）
            for child in ast.iter_child_nodes(node):
                visit(child, None)
        elif isinstance(node, ast.ClassDef):
            syms.append(
                SourceSymbol(
                    name=node.name, kind="class",
                    start_line=node.lineno, end_line=_py_end_line(node),
                )
            )
            for child in ast.iter_child_nodes(node):
                visit(child, node.name)

    for top in ast.iter_child_nodes(tree):
        visit(top, None)
    return syms


def _node_name(node) -> str:
    n = node.child_by_field_name("name")
    return n.text.decode("utf-8", "replace") if n is not None else ""


def _extract_ts_symbols(text: str, grammar: str) -> list[SourceSymbol]:
    """tree-sitter で TS/JS の関数・クラス・メソッド・アロー代入を抽出する。

    パース例外は握りつぶし、空リストで返す（解析全体は止めない）。
    """
    try:
        parser = _get_parser(grammar)
        tree = parser.parse(text.encode("utf-8"))
    except Exception:  # noqa: BLE001 — 1 ファイル失敗で全体を止めない
        return []

    syms: list[SourceSymbol] = []

    def add(node, name: str, kind: str) -> None:
        if name:
            syms.append(
                SourceSymbol(
                    name=name, kind=kind,
                    start_line=node.start_point.row + 1,
                    end_line=node.end_point.row + 1,
                )
            )

    def visit(node, class_name: str | None) -> None:
        t = node.type
        if t in ("function_declaration", "generator_function_declaration"):
            add(node, _node_name(node), "function")
        elif t == "class_declaration":
            cname = _node_name(node)
            add(node, cname, "class")
            for child in node.children:
                visit(child, cname or class_name)
            return  # body は手動再帰済み（二重走査回避）
        elif t == "method_definition":
            nm = _node_name(node)
            add(node, f"{class_name}.{nm}" if class_name else nm, "method")
        elif t == "variable_declarator":
            val = node.child_by_field_name("value")
            if val is not None and val.type in (
                "arrow_function", "function", "function_expression",
            ):
                add(node, _node_name(node), "function")
        for child in node.children:
            visit(child, class_name)

    visit(tree.root_node, None)
    return syms


def extract_symbols(text: str, language: str) -> list[SourceSymbol]:
    if language == "python":
        return _extract_python_symbols(text)
    grammar = _TS_GRAMMAR.get(language)
    if grammar:
        return _extract_ts_symbols(text, grammar)
    return []


# ─── インデックス本体 ─────────────────────────────────────────────────


@dataclass
class SourceIndex:
    """コードベース 1 件の決定論インデックス。

    本文は持たず、署名（files[].symbols）だけを保持する。``read`` は都度ディスクから読む。
    """

    root: Path
    files: list[SourceFile] = field(default_factory=list)

    # ── 集計 ──
    def language_breakdown(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for f in self.files:
            out[f.language] = out.get(f.language, 0) + 1
        return out

    def symbol_count(self) -> int:
        return sum(len(f.symbols) for f in self.files)

    def total_bytes(self) -> int:
        return sum(f.bytes for f in self.files)

    # ── 検索（source_search ツールの裏）──
    def search(
        self, query: str, *, lang: str | None = None, limit: int = 30
    ) -> list[dict]:
        """query の識別子トークンで関連ファイルをランキングして返す（本文は返さない）。"""
        terms = _tokenize(query)
        results: list[tuple[int, dict]] = []
        for f in self.files:
            if lang and f.language != _normalize_lang(lang):
                continue
            score, matched = _score_file(f, terms)
            if score <= 0:
                continue
            results.append(
                (
                    score,
                    {
                        "path": f.path,
                        "language": f.language,
                        "score": score,
                        "matched_symbols": [
                            {"name": s.name, "kind": s.kind,
                             "start_line": s.start_line, "end_line": s.end_line}
                            for s in matched
                        ],
                    },
                )
            )
        results.sort(key=lambda kv: kv[0], reverse=True)
        return [r for _, r in results[:limit]]

    # ── 読み取り（source_read ツールの裏）──
    def read(
        self, rel_path: str, *, symbol: str | None = None, max_chars: int = 6000
    ) -> str:
        """rel_path（symbol 指定時は関数単位スライス）の本文を上限つきで返す。"""
        target = _resolve_within(self.root, rel_path)
        if target is None:
            return f"エラー: コードベース外のパスは読めません: {rel_path!r}"
        if not target.is_file():
            return f"エラー: ファイルが見つかりません: {rel_path}"
        try:
            text = target.read_text(encoding="utf-8", errors="replace")
        except Exception as e:  # noqa: BLE001
            return f"エラー: 読み取りに失敗しました: {e}"

        lines = text.splitlines()
        header = rel_path
        if symbol:
            sym = self._find_symbol(rel_path, symbol)
            if sym is None:
                return (
                    f"エラー: {rel_path} にシンボル {symbol!r} が見つかりません。"
                    "source_search で正しいシンボル名を確認してください。"
                )
            start = max(1, sym.start_line)
            end = min(len(lines), sym.end_line)
            body = "\n".join(lines[start - 1 : end])
            header = f"{rel_path}:{start}-{end} ({sym.kind} {sym.name})"
        else:
            body = "\n".join(lines)
            header = f"{rel_path}:1-{len(lines)}"
        body = _truncate_middle(body, max_chars)
        return f"// {header}\n{body}"

    def _find_symbol(self, rel_path: str, symbol: str) -> SourceSymbol | None:
        for f in self.files:
            if f.path != rel_path:
                continue
            for s in f.symbols:
                if s.name == symbol:
                    return s
            # メソッドは "Class.method" / "method" どちらの指定も許容
            for s in f.symbols:
                if s.name.split(".")[-1] == symbol:
                    return s
        return None

    # ── 永続化 ──
    def to_dict(self) -> dict:
        return {
            "version": 1,
            "files": [f.model_dump() for f in self.files],
        }

    @classmethod
    def from_dict(cls, root: Path, data: dict) -> "SourceIndex":
        files = [SourceFile(**f) for f in data.get("files", [])]
        return cls(root=root, files=files)


# ─── 検索ヘルパ ───────────────────────────────────────────────────────

_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")


def _tokenize(query: str) -> list[str]:
    """検索クエリから識別子トークン（3 文字以上）を小文字で抽出する。"""
    seen: dict[str, None] = {}
    for m in _TOKEN_RE.findall(query or ""):
        seen.setdefault(m.lower(), None)
    return list(seen.keys())


def _normalize_lang(lang: str) -> str:
    aliases = {"py": "python", "ts": "typescript", "js": "javascript"}
    return aliases.get(lang.lower(), lang.lower())


def _score_file(f: SourceFile, terms: list[str]) -> tuple[int, list[SourceSymbol]]:
    if not terms:
        return 0, []
    score = 0
    path_low = f.path.lower()
    matched: list[SourceSymbol] = []
    for term in terms:
        if term in path_low:
            score += 2
    for s in f.symbols:
        name_low = s.name.lower()
        tail = name_low.split(".")[-1]
        hit = False
        for term in terms:
            if term == tail or term == name_low:
                score += 5
                hit = True
            elif term in name_low:
                score += 3
                hit = True
        if hit:
            matched.append(s)
    return score, matched[:20]


def _truncate_middle(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    keep = max_chars // 2
    omitted = len(text) - max_chars
    return text[:keep] + f"\n...（{omitted} 文字省略）...\n" + text[-keep:]


def _resolve_within(root: Path, rel_path: str) -> Path | None:
    """rel_path を root 配下の絶対パスに解決。root の外を指すなら None（zip-slip 防止）。"""
    root = root.resolve()
    try:
        target = (root / rel_path).resolve()
    except Exception:  # noqa: BLE001
        return None
    if target == root or root in target.parents:
        return target
    return None


# ─── 構築・永続化 ─────────────────────────────────────────────────────

_INDEX_FILENAME = ".index.json"


def build_source_index(root: Path) -> SourceIndex:
    """root 配下を走査し、対象言語のファイルをインデックス化する。"""
    root = Path(root).resolve()
    files: list[SourceFile] = []
    for path in _walk(root):
        language = language_for(path)
        if language is None:
            continue
        try:
            size = path.stat().st_size
        except OSError:
            continue
        if size > MAX_FILE_BYTES:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            continue
        symbols = extract_symbols(text, language)
        files.append(
            SourceFile(
                path=path.relative_to(root).as_posix(),
                language=language,
                bytes=size,
                lines=text.count("\n") + (1 if text and not text.endswith("\n") else 0),
                symbols=symbols,
            )
        )
    files.sort(key=lambda f: f.path)
    return SourceIndex(root=root, files=files)


def _walk(root: Path):
    """除外ディレクトリ/ファイルを飛ばしつつ全ファイルを yield する。"""
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            entries = list(current.iterdir())
        except OSError:
            continue
        for entry in entries:
            if entry.is_dir():
                if not is_excluded_dir(entry.name):
                    stack.append(entry)
            elif entry.is_file():
                if not is_excluded_file(entry):
                    yield entry


def save_index(index: SourceIndex, *, root: Path | None = None) -> Path:
    """インデックスを ``<root>/.index.json`` に保存し、そのパスを返す。"""
    base = Path(root or index.root)
    out = base / _INDEX_FILENAME
    out.write_text(
        json.dumps(index.to_dict(), ensure_ascii=False), encoding="utf-8"
    )
    return out


def load_index(root: Path) -> SourceIndex | None:
    """``<root>/.index.json`` があればロード。無ければ None。"""
    base = Path(root)
    cache = base / _INDEX_FILENAME
    if not cache.is_file():
        return None
    try:
        data = json.loads(cache.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return SourceIndex.from_dict(base.resolve(), data)


def get_or_build_index(root: Path) -> SourceIndex:
    """キャッシュがあればロード、無ければ構築して保存。"""
    cached = load_index(root)
    if cached is not None:
        return cached
    index = build_source_index(root)
    try:
        save_index(index)
    except OSError:
        pass
    return index
