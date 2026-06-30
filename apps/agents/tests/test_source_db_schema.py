"""DB スキーマ抽出（db_schema.py）の単体テスト。"""
from __future__ import annotations

from pathlib import Path

from log_analyzer.source.db_schema import extract_db_schema


def _tables(schema):
    return {t.name.lower(): t for t in schema.tables}


def test_ddl_create_table(tmp_path: Path):
    (tmp_path / "schema.sql").write_text(
        """
        CREATE TABLE payments (
            id BIGINT PRIMARY KEY,
            user_id BIGINT NOT NULL REFERENCES users(id),
            status VARCHAR(16) NOT NULL DEFAULT 'pending'
        );
        CREATE INDEX idx_pay ON payments (user_id, status);
        """,
        encoding="utf-8",
    )
    schema = extract_db_schema(tmp_path)
    t = _tables(schema)["payments"]
    cols = {c.name: c for c in t.columns}
    assert "ddl" in t.sources
    assert cols["id"].primary_key is True
    assert cols["id"].nullable is False  # PK は暗黙 NOT NULL
    assert cols["user_id"].nullable is False
    assert cols["user_id"].foreign_key == "users.id"
    assert cols["status"].default == "'pending'"
    assert ["user_id", "status"] in t.indexes


def test_ddl_table_level_primary_key(tmp_path: Path):
    (tmp_path / "s.sql").write_text(
        """
        CREATE TABLE membership (
            org_id BIGINT,
            user_id BIGINT,
            PRIMARY KEY (org_id, user_id),
            FOREIGN KEY (user_id) REFERENCES users(id)
        );
        """,
        encoding="utf-8",
    )
    t = _tables(extract_db_schema(tmp_path))["membership"]
    cols = {c.name: c for c in t.columns}
    assert cols["org_id"].primary_key is True
    assert cols["user_id"].primary_key is True
    assert cols["user_id"].foreign_key == "users.id"


def test_sqlalchemy_model(tmp_path: Path):
    (tmp_path / "models.py").write_text(
        "from sqlalchemy import Column, Integer, String, ForeignKey\n"
        "class User(Base):\n"
        "    __tablename__ = 'users'\n"
        "    id = Column(Integer, primary_key=True)\n"
        "    email = Column(String(255), nullable=False)\n"
        "    org_id = Column(Integer, ForeignKey('orgs.id'))\n",
        encoding="utf-8",
    )
    t = _tables(extract_db_schema(tmp_path))["users"]
    cols = {c.name: c for c in t.columns}
    assert "orm/sqlalchemy" in t.sources
    assert cols["id"].primary_key is True
    assert cols["email"].nullable is False
    assert cols["org_id"].foreign_key == "orgs.id"


def test_django_model(tmp_path: Path):
    (tmp_path / "models.py").write_text(
        "from django.db import models\n"
        "class Article(models.Model):\n"
        "    title = models.CharField(max_length=200)\n"
        "    body = models.TextField(null=True)\n"
        "    author = models.ForeignKey('User', on_delete=models.CASCADE)\n"
        "    class Meta:\n"
        "        db_table = 'articles'\n",
        encoding="utf-8",
    )
    t = _tables(extract_db_schema(tmp_path))["articles"]
    cols = {c.name: c for c in t.columns}
    assert "orm/django" in t.sources
    assert cols["title"].type == "CharField"
    assert cols["title"].nullable is False  # null 未指定 → NOT NULL
    assert cols["body"].nullable is True
    assert cols["author"].foreign_key == "user.id"


def test_prisma_model(tmp_path: Path):
    (tmp_path / "schema.prisma").write_text(
        """
        model Post {
          id        Int     @id @default(autoincrement())
          title     String
          published Boolean @default(false)
          bio       String?
        }
        """,
        encoding="utf-8",
    )
    t = _tables(extract_db_schema(tmp_path))["post"]
    cols = {c.name: c for c in t.columns}
    assert "orm/prisma" in t.sources
    assert cols["id"].primary_key is True
    assert cols["title"].nullable is False
    assert cols["bio"].nullable is True


def test_ddl_and_orm_merge_sources(tmp_path: Path):
    (tmp_path / "schema.sql").write_text(
        "CREATE TABLE users (id BIGINT PRIMARY KEY, email VARCHAR(255));",
        encoding="utf-8",
    )
    (tmp_path / "models.py").write_text(
        "from sqlalchemy import Column, Integer, String\n"
        "class User(Base):\n"
        "    __tablename__ = 'users'\n"
        "    id = Column(Integer, primary_key=True)\n"
        "    phone = Column(String(32))\n",
        encoding="utf-8",
    )
    t = _tables(extract_db_schema(tmp_path))["users"]
    cols = {c.name for c in t.columns}
    assert {"id", "email", "phone"} <= cols  # DDL と ORM の列が統合される
    assert set(t.sources) == {"ddl", "orm/sqlalchemy"}
