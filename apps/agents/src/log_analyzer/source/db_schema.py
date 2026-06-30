"""DB スキーマ抽出（Phase 1）。

コードベースから DB 構造を抽出する。源は 2 系統:

- **SQL DDL**（``.sql`` / migrations）: ``CREATE TABLE`` / ``CREATE INDEX`` /
  ``ALTER TABLE ... ADD`` を sqlparse で文分割し、本モジュールの構文パーサで
  テーブル・列・PK/FK/index を取り出す。
- **ORM モデル**: SQLAlchemy / Django（``.py`` を ast で）、Prisma（``schema.prisma``）。

同名テーブルは列をマージし、抽出元（``ddl`` / ``orm/sqlalchemy`` 等）を併記する。
失敗は 1 ファイル単位で握りつぶし、全体は止めない。
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import sqlparse

from log_analyzer.schema import DbColumn, DbSchema, DbTable
from log_analyzer.source.indexer import _walk

# ─── マージ用の内部表現 ───────────────────────────────────────────────


class _TableAcc:
    def __init__(self, name: str) -> None:
        self.name = name
        self.columns: dict[str, DbColumn] = {}
        self.indexes: list[list[str]] = []
        self.sources: set[str] = set()

    def add_column(self, col: DbColumn) -> None:
        existing = self.columns.get(col.name)
        if existing is None:
            self.columns[col.name] = col
            return
        # マージ: 型は先勝ち（非空優先）、フラグは OR、fk/default は非空優先
        if not existing.type and col.type:
            existing.type = col.type
        existing.primary_key = existing.primary_key or col.primary_key
        existing.nullable = existing.nullable and col.nullable
        if not existing.default and col.default:
            existing.default = col.default
        if not existing.foreign_key and col.foreign_key:
            existing.foreign_key = col.foreign_key

    def to_table(self) -> DbTable:
        return DbTable(
            name=self.name,
            columns=list(self.columns.values()),
            indexes=self.indexes,
            sources=sorted(self.sources),
        )


def _merge(acc: dict[str, _TableAcc], name: str) -> _TableAcc:
    key = name.lower()
    if key not in acc:
        acc[key] = _TableAcc(name)
    return acc[key]


# ─── 公開 API ─────────────────────────────────────────────────────────


def summarize_db_schema(schema: DbSchema, *, max_tables: int = 40, max_cols: int = 8) -> str:
    """log_text 注入用の **要約**（テーブル名＋主要列）。詳細は db_schema(table) ツールで。

    input トークン肥大を避けるため、列は max_cols まで・テーブルは max_tables まで。
    """
    if not schema.tables:
        return ""
    lines = [
        "## DB スキーマ（要約）",
        "詳細な列定義は db_schema(table) ツールで取得できます。",
    ]
    for t in schema.tables[:max_tables]:
        col_strs: list[str] = []
        for c in t.columns[:max_cols]:
            if c.primary_key:
                col_strs.append(f"{c.name} PK")
            elif c.foreign_key:
                col_strs.append(f"{c.name} FK→{c.foreign_key}")
            else:
                col_strs.append(c.name)
        more = "" if len(t.columns) <= max_cols else f", …(+{len(t.columns) - max_cols})"
        src = "+".join(t.sources) if t.sources else "?"
        lines.append(f"- {t.name}({', '.join(col_strs)}{more})  [{src}]")
    if len(schema.tables) > max_tables:
        lines.append(
            f"（他 {len(schema.tables) - max_tables} テーブル省略。db_schema(table) で取得可）"
        )
    return "\n".join(lines) + "\n"


def format_db_schema_detail(schema: DbSchema, table: str | None = None) -> str:
    """db_schema ツールが返す詳細表現（列の型・NOT NULL・PK/FK・default・index）。"""
    if not schema.tables:
        return "DB スキーマは検出されていません（DDL / ORM が見つかりません）。"
    tables = schema.tables
    if table:
        tl = table.lower()
        tables = [t for t in schema.tables if t.name.lower() == tl]
        if not tables:
            avail = ", ".join(t.name for t in schema.tables) or "(なし)"
            return f"テーブル {table!r} は見つかりません。利用可能なテーブル: {avail}"
    lines: list[str] = []
    for t in tables:
        src = "+".join(t.sources) if t.sources else "?"
        lines.append(f"### table: {t.name}  [{src}]")
        for c in t.columns:
            flags: list[str] = []
            if c.primary_key:
                flags.append("PK")
            if not c.nullable:
                flags.append("NOT NULL")
            if c.foreign_key:
                flags.append(f"FK→{c.foreign_key}")
            if c.default:
                flags.append(f"default={c.default}")
            suffix = (" " + " ".join(flags)) if flags else ""
            lines.append(f"  - {c.name} {c.type}{suffix}".rstrip())
        for idx in t.indexes:
            lines.append(f"  index: ({', '.join(idx)})")
    return "\n".join(lines)


def extract_db_schema(root: Path) -> DbSchema:
    """root 配下から DDL ＋ ORM の DB スキーマを抽出・マージして返す。"""
    root = Path(root)
    acc: dict[str, _TableAcc] = {}
    for path in _walk(root):
        suffix = path.suffix.lower()
        try:
            if suffix == ".sql":
                _ingest_sql(path.read_text(encoding="utf-8", errors="replace"), acc)
            elif path.name == "schema.prisma" or suffix == ".prisma":
                _ingest_prisma(path.read_text(encoding="utf-8", errors="replace"), acc)
            elif suffix == ".py":
                _ingest_python_orm(path.read_text(encoding="utf-8", errors="replace"), acc)
        except Exception:  # noqa: BLE001 — 1 ファイル失敗で全体を止めない
            continue
    tables = [acc[k].to_table() for k in sorted(acc)]
    return DbSchema(tables=tables)


# ─── SQL DDL ──────────────────────────────────────────────────────────

_IDENT = r'[`"\[]?(\w+)[`"\]]?'


def _ingest_sql(sql: str, acc: dict[str, _TableAcc]) -> None:
    cleaned = sqlparse.format(sql, strip_comments=True)
    for raw in sqlparse.split(cleaned):
        stmt = raw.strip().rstrip(";").strip()
        if not stmt:
            continue
        head = stmt[:12].lower()
        if head.startswith("create tabl"):
            _parse_create_table(stmt, acc)
        elif head.startswith("create inde") or stmt[:18].lower().startswith("create unique inde"):
            _parse_create_index(stmt, acc)
        elif head.startswith("alter table"):
            _parse_alter_table(stmt, acc)


def _strip_ident(token: str) -> str:
    token = token.strip().strip(",")
    # schema.table → table、囲み文字を除去
    token = token.split(".")[-1]
    return token.strip('`"[]')


def _extract_balanced(text: str) -> str | None:
    """最初の '(' から対応する ')' までの中身を返す（ネスト対応）。"""
    start = text.find("(")
    if start < 0:
        return None
    depth = 0
    for i in range(start, len(text)):
        ch = text[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return text[start + 1 : i]
    return None


def _split_top_level(body: str) -> list[str]:
    """トップレベルのカンマで分割（括弧内のカンマは無視）。"""
    items: list[str] = []
    depth = 0
    cur: list[str] = []
    for ch in body:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            items.append("".join(cur).strip())
            cur = []
        else:
            cur.append(ch)
    if cur:
        last = "".join(cur).strip()
        if last:
            items.append(last)
    return items


_COL_RE = re.compile(
    r'^[`"\[]?(?P<name>\w+)[`"\]]?\s+(?P<type>[A-Za-z][\w]*(?:\s*\([^)]*\))?)(?P<rest>.*)$',
    re.S,
)
_DEFAULT_RE = re.compile(r"default\s+('(?:[^']*)'|\S+)", re.I)
_REFERENCES_RE = re.compile(r"references\s+" + _IDENT + r"\s*\(\s*" + _IDENT + r"\s*\)", re.I)


def _parse_create_table(stmt: str, acc: dict[str, _TableAcc]) -> None:
    m = re.match(
        r"create\s+table\s+(?:if\s+not\s+exists\s+)?(?P<name>[`\"\[\]\w.]+)",
        stmt, re.I,
    )
    if not m:
        return
    table_name = _strip_ident(m.group("name"))
    body = _extract_balanced(stmt)
    if body is None:
        return
    table = _merge(acc, table_name)
    table.sources.add("ddl")

    for item in _split_top_level(body):
        low = item.lower()
        first = low.split("(", 1)[0].strip()
        if first.startswith("primary key"):
            for col in _cols_in_parens(item):
                c = _ensure_col(table, col)
                c.primary_key = True
                c.nullable = False
            continue
        if first.startswith("foreign key") or (first.startswith("constraint") and "foreign key" in low):
            _apply_inline_fk(table, item)
            continue
        if first.startswith("constraint") and "primary key" in low:
            for col in _cols_in_parens(item):
                c = _ensure_col(table, col)
                c.primary_key = True
                c.nullable = False
            continue
        if first.startswith(("unique", "key", "index", "check", "constraint")):
            cols = _cols_in_parens(item)
            if cols and first.startswith(("unique", "key", "index")):
                table.indexes.append(cols)
            continue
        col = _parse_column_def(item)
        if col is not None:
            table.add_column(col)


def _ensure_col(table: _TableAcc, name: str) -> DbColumn:
    if name not in table.columns:
        table.columns[name] = DbColumn(name=name)
    return table.columns[name]


def _cols_in_parens(item: str) -> list[str]:
    inner = _extract_balanced(item)
    if inner is None:
        return []
    return [_strip_ident(c) for c in inner.split(",") if c.strip()]


def _apply_inline_fk(table: _TableAcc, item: str) -> None:
    cols = _cols_in_parens(item)
    ref = _REFERENCES_RE.search(item)
    if cols and ref:
        target = f"{_strip_ident(ref.group(1))}.{_strip_ident(ref.group(2))}"
        _ensure_col(table, cols[0]).foreign_key = target


def _parse_column_def(item: str) -> DbColumn | None:
    m = _COL_RE.match(item.strip())
    if not m:
        return None
    name = m.group("name")
    if name.lower() in {"primary", "foreign", "constraint", "unique", "key", "index", "check"}:
        return None
    rest = m.group("rest") or ""
    low = rest.lower()
    is_pk = "primary key" in low
    col = DbColumn(
        name=name,
        type=re.sub(r"\s+", "", m.group("type")),
        nullable=("not null" not in low) and not is_pk,  # PK は暗黙 NOT NULL
        primary_key=is_pk,
    )
    dm = _DEFAULT_RE.search(rest)
    if dm:
        col.default = dm.group(1)
    rm = _REFERENCES_RE.search(rest)
    if rm:
        col.foreign_key = f"{_strip_ident(rm.group(1))}.{_strip_ident(rm.group(2))}"
    return col


def _parse_create_index(stmt: str, acc: dict[str, _TableAcc]) -> None:
    m = re.search(r"\bon\s+(?P<name>[`\"\[\]\w.]+)", stmt, re.I)
    if not m:
        return
    table = _merge(acc, _strip_ident(m.group("name")))
    cols = [_strip_ident(c) for c in (_extract_balanced(stmt) or "").split(",") if c.strip()]
    if cols:
        table.indexes.append(cols)


def _parse_alter_table(stmt: str, acc: dict[str, _TableAcc]) -> None:
    m = re.match(r"alter\s+table\s+(?P<name>[`\"\[\]\w.]+)", stmt, re.I)
    if not m:
        return
    table = _merge(acc, _strip_ident(m.group("name")))
    table.sources.add("ddl")
    low = stmt.lower()
    if "foreign key" in low:
        _apply_inline_fk(table, stmt)
    if "primary key" in low:
        for col in _cols_in_parens(stmt):
            c = _ensure_col(table, col)
            c.primary_key = True
            c.nullable = False


# ─── ORM: SQLAlchemy / Django（Python ast）─────────────────────────


def _ingest_python_orm(text: str, acc: dict[str, _TableAcc]) -> None:
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        if _is_django_model(node):
            _parse_django_model(node, acc)
        elif _has_tablename(node):
            _parse_sqlalchemy_model(node, acc)


def _call_func_name(call: ast.Call) -> str:
    f = call.func
    if isinstance(f, ast.Name):
        return f.id
    if isinstance(f, ast.Attribute):
        return f.attr
    return ""


def _str_value(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _kw(call: ast.Call, name: str):
    for kw in call.keywords:
        if kw.arg == name:
            return kw.value
    return None


def _kw_is_true(call: ast.Call, name: str) -> bool:
    v = _kw(call, name)
    return isinstance(v, ast.Constant) and v.value is True


# -- SQLAlchemy --

def _has_tablename(cls: ast.ClassDef) -> bool:
    for stmt in cls.body:
        if isinstance(stmt, ast.Assign):
            for t in stmt.targets:
                if isinstance(t, ast.Name) and t.id == "__tablename__":
                    return True
    return False


def _parse_sqlalchemy_model(cls: ast.ClassDef, acc: dict[str, _TableAcc]) -> None:
    table_name = cls.name.lower()
    for stmt in cls.body:
        if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1:
            tgt = stmt.targets[0]
            if isinstance(tgt, ast.Name) and tgt.id == "__tablename__":
                s = _str_value(stmt.value)
                if s:
                    table_name = s
    table = _merge(acc, table_name)
    table.sources.add("orm/sqlalchemy")

    for stmt in cls.body:
        if not isinstance(stmt, (ast.Assign, ast.AnnAssign)):
            continue
        targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
        value = stmt.value
        if not isinstance(value, ast.Call):
            continue
        if _call_func_name(value) not in ("Column", "mapped_column"):
            continue
        name = next((t.id for t in targets if isinstance(t, ast.Name)), None)
        if not name:
            continue
        col = DbColumn(name=name)
        # 型 = 最初の位置引数（ForeignKey でないもの）
        for arg in value.args:
            if isinstance(arg, ast.Call) and _call_func_name(arg) == "ForeignKey":
                fk = _str_value(arg.args[0]) if arg.args else None
                if fk:
                    col.foreign_key = fk
            elif not col.type:
                col.type = _type_name(arg)
        col.primary_key = _kw_is_true(value, "primary_key")
        nv = _kw(value, "nullable")
        if isinstance(nv, ast.Constant) and isinstance(nv.value, bool):
            col.nullable = nv.value
        elif col.primary_key:
            col.nullable = False
        table.add_column(col)


def _type_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Call):
        return _call_func_name(node)
    return ""


# -- Django --

def _is_django_model(cls: ast.ClassDef) -> bool:
    for base in cls.bases:
        if isinstance(base, ast.Attribute) and base.attr == "Model":
            return True
        if isinstance(base, ast.Name) and base.id == "Model":
            return True
    return False


def _parse_django_model(cls: ast.ClassDef, acc: dict[str, _TableAcc]) -> None:
    table_name = cls.name.lower()
    # Meta.db_table があれば優先
    for stmt in cls.body:
        if isinstance(stmt, ast.ClassDef) and stmt.name == "Meta":
            for ms in stmt.body:
                if isinstance(ms, ast.Assign):
                    for t in ms.targets:
                        if isinstance(t, ast.Name) and t.id == "db_table":
                            s = _str_value(ms.value)
                            if s:
                                table_name = s
    table = _merge(acc, table_name)
    table.sources.add("orm/django")

    for stmt in cls.body:
        if not isinstance(stmt, ast.Assign) or len(stmt.targets) != 1:
            continue
        tgt = stmt.targets[0]
        if not isinstance(tgt, ast.Name):
            continue
        value = stmt.value
        if not isinstance(value, ast.Call):
            continue
        fname = _call_func_name(value)
        # ``*Field`` に加え、Field で終わらない ForeignKey も拾う
        if not fname.endswith("Field") and fname != "ForeignKey":
            continue
        col = DbColumn(name=tgt.id, type=fname)
        col.primary_key = _kw_is_true(value, "primary_key")
        # Django 既定は NOT NULL。null=True のときだけ nullable。
        nv = _kw(value, "null")
        col.nullable = isinstance(nv, ast.Constant) and nv.value is True
        if fname in ("ForeignKey", "OneToOneField", "ManyToManyField"):
            ref = _str_value(value.args[0]) if value.args else None
            if ref is None and value.args:
                ref = _type_name(value.args[0])
            if ref:
                col.foreign_key = f"{ref.lower()}.id"
        table.add_column(col)


# ─── ORM: Prisma ──────────────────────────────────────────────────────

_PRISMA_MODEL_RE = re.compile(r"model\s+(\w+)\s*\{(.*?)\}", re.S)


def _ingest_prisma(text: str, acc: dict[str, _TableAcc]) -> None:
    for m in _PRISMA_MODEL_RE.finditer(text):
        name = m.group(1)
        table = _merge(acc, name)
        table.sources.add("orm/prisma")
        for line in m.group(2).splitlines():
            line = line.strip()
            if not line or line.startswith("//") or line.startswith("@@"):
                continue
            parts = line.split()
            if len(parts) < 2:
                continue
            field_name, ftype = parts[0], parts[1]
            attrs = " ".join(parts[2:])
            nullable = ftype.endswith("?")
            base_type = ftype.rstrip("?[]")
            col = DbColumn(
                name=field_name,
                type=base_type,
                nullable=nullable,
                primary_key="@id" in attrs,
            )
            rel = re.search(r"@relation\([^)]*references:\s*\[(\w+)\]", attrs)
            if rel:
                col.foreign_key = f"{base_type.lower()}.{rel.group(1)}"
            dm = re.search(r"@default\(([^)]*)\)", attrs)
            if dm:
                col.default = dm.group(1)
            table.add_column(col)
