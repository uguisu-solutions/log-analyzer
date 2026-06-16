"""ingest_logs_to_bq.py の文字コード処理テスト。

ad021_case2 を壊したのと同じ「Shift-JIS を errors=replace で取り込んで U+FFFD に
潰す」事故を二度と起こさないことを検証する。BQ 接続は不要 (_read_text 単体)。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

# scripts/ を import path に追加
_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(_SCRIPTS))

import ingest_logs_to_bq as ing  # noqa: E402

# Windows の AD ログにありがちな日本語を含む 1 行
_JP = "Kerberos 認証チケット (TGT) が要求されました。アカウント名: Administrator"


def test_read_text_auto_detects_cp932(tmp_path: Path):
    p = tmp_path / "sjis.log"
    p.write_bytes(_JP.encode("cp932"))
    out = ing._read_text(p, "auto")
    assert out == _JP
    assert "�" not in out  # 文字化け (U+FFFD) が無い


def test_read_text_auto_detects_utf8(tmp_path: Path):
    p = tmp_path / "utf8.log"
    p.write_bytes(_JP.encode("utf-8"))
    out = ing._read_text(p, "auto")
    assert out == _JP
    assert "�" not in out


def test_read_text_explicit_cp932(tmp_path: Path):
    p = tmp_path / "sjis.log"
    p.write_bytes(_JP.encode("cp932"))
    assert ing._read_text(p, "cp932") == _JP


def test_read_text_strict_utf8_on_cp932_raises(tmp_path: Path):
    """utf-8 を明示した上で Shift-JIS を渡したら、黙って潰さず例外にする。"""
    p = tmp_path / "sjis.log"
    p.write_bytes(_JP.encode("cp932"))
    with pytest.raises(UnicodeDecodeError):
        ing._read_text(p, "utf-8")


def test_rows_from_file_preserves_japanese(tmp_path: Path):
    from datetime import datetime, timezone

    p = tmp_path / "ad.log"
    p.write_bytes(("2025-11-12T00:00:00Z " + _JP).encode("cp932"))
    rows = ing._rows_from_file(p, "ADServer", datetime.now(timezone.utc), "auto")
    assert len(rows) == 1
    assert "�" not in rows[0]["message"]
    assert "Administrator" in rows[0]["message"]
    assert "認証チケット" in rows[0]["message"]
