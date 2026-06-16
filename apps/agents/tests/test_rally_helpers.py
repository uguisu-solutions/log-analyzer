"""rally/_helpers の JSON 抽出テスト。

BQ ツール利用後にモデルが「前置き散文 + ```json フェンス」で返すケース
(integrator フォールバックの原因) を確実に拾えることを検証する。
"""
from __future__ import annotations

from log_analyzer.rally._helpers import extract_json, safe_extract_json


def test_pure_json():
    assert extract_json('{"next": "integrator", "confidence": 0.5}') == {
        "next": "integrator", "confidence": 0.5
    }


def test_leading_fence_json():
    text = '```json\n{"next": "fw"}\n```'
    assert extract_json(text) == {"next": "fw"}


def test_preamble_then_fenced_json():
    # 実際に再現した壊れパターン: 散文 → ```json フェンス
    text = (
        "深刻な攻撃の兆候が見られます。Kerberos の列挙が疑われます。\n\n"
        '```json\n{"findings": [{"category": "Sec", "summary": "AS-REP"}], '
        '"next": "fw"}\n```'
    )
    out = extract_json(text)
    assert out["next"] == "fw"
    assert out["findings"][0]["category"] == "Sec"


def test_preamble_then_bare_object():
    # フェンス無しで散文の後ろに素の {...}
    text = '分析結果は以下のとおりです。 {"next": "routing", "confidence": 0.7} 以上。'
    assert extract_json(text) == {"next": "routing", "confidence": 0.7}


def test_brace_inside_string_not_miscounted():
    text = 'メモ: {"summary": "値に } や { を含む", "next": "dns"} 補足'
    out = extract_json(text)
    assert out["next"] == "dns"
    assert "}" in out["summary"]


def test_safe_extract_falls_back_on_garbage():
    out, err = safe_extract_json("ここには JSON がありません", fallback={"next": "integrator"})
    assert out == {"next": "integrator"}
    assert err is not None


def test_safe_extract_truncated_json_falls_back():
    # max_tokens 切断で閉じ括弧が無い → フォールバック
    out, err = safe_extract_json('{"findings": [{"summary": "途中で', fallback={"next": "x"})
    assert out == {"next": "x"}
    assert err is not None
