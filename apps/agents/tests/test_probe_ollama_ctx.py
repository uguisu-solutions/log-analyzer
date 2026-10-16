"""scripts/probe_ollama_ctx.py（Ollama の num_ctx の実測）のテスト。

docs/plan/slm_support.md §7-1 / §7-3：Phase 5 の最初に、わざと長い入力を送って
「どこまで num_ctx を上げられるか」「入らなかったときに切り捨てを検知できるか」を確かめる。
テストでは LLM 呼び出しと /api/ps を偽物に差し替え、HTTP は使わない。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(_SCRIPTS))

import probe_ollama_ctx as po  # noqa: E402


def test_plan_lines_fills_num_ctx_by_ratio():
    """1 行あたりのトークン数から、num_ctx の何割を埋めるかを行数に直す（超過分も送る）。"""
    assert po.plan_lines(32768, fills=[0.5, 0.9, 1.2], tokens_per_line=2.0) == [8192, 14745, 19660]


class _FakeComplete:
    """llm_client.complete の代わり。呼ばれた時点の OLLAMA_NUM_CTX と引数を記録する。"""

    def __init__(self, *, input_tokens=0, truncated=False, error=None):
        self.input_tokens, self.truncated, self.error = input_tokens, truncated, error
        self.calls: list[dict] = []

    def __call__(self, **kwargs):
        self.calls.append({**kwargs, "num_ctx_env": os.environ.get("OLLAMA_NUM_CTX")})
        if self.error:
            raise self.error
        return SimpleNamespace(
            usage=SimpleNamespace(input_tokens=self.input_tokens),
            input_truncated=self.truncated,
        )


def test_probe_once_sends_through_app_path_with_given_num_ctx(monkeypatch):
    """アプリと同じ llm_client.complete で送る（切り捨て検知もアプリの判定そのものを使う）。

    num_ctx は呼び出しの間だけ差し替え、終わったら元に戻す。
    """
    monkeypatch.setenv("OLLAMA_NUM_CTX", "32768")
    fake = _FakeComplete(input_tokens=65536, truncated=True)

    p = po.probe_once("ollama:gemma4:26b-a4b-it-q4_K_M", num_ctx=65536, lines=100, complete=fake)

    (call,) = fake.calls
    assert call["model"] == "ollama:gemma4:26b-a4b-it-q4_K_M"
    assert call["num_ctx_env"] == "65536"
    assert call["messages"][0]["content"].count("\n") + 1 == 100
    assert call["max_tokens"] <= 16  # 出力は要らない。入力の処理だけを見る
    assert os.environ["OLLAMA_NUM_CTX"] == "32768"
    assert (p.num_ctx, p.lines, p.prompt_tokens, p.truncated, p.error) == (65536, 100, 65536, True, "")


def test_probe_once_records_error_instead_of_raising():
    """VRAM に載らない num_ctx は Ollama がエラーを返す。それも計測結果なので記録して続ける。"""
    fake = _FakeComplete(error=RuntimeError("Ollama がエラーを返した（HTTP 500）: out of memory"))

    p = po.probe_once("ollama:x", num_ctx=262144, lines=10, complete=fake)

    assert p.error == "Ollama がエラーを返した（HTTP 500）: out of memory"
    assert (p.prompt_tokens, p.truncated) == (0, False)


@pytest.mark.parametrize(
    ("prompt_tokens", "truncated", "error", "expected"),
    [
        (1000, False, "", "全部入った"),
        (4096, True, "", "切り捨てを検知"),
        # 送ったはずの量（1000 行 × 2 トークン）より大きく少ないのに印が無い → アプリが見逃す
        (1500, False, "", "検知漏れ"),
        (0, False, "out of memory", "失敗"),
    ],
)
def test_verdict(prompt_tokens, truncated, error, expected):
    p = po.Probe(num_ctx=4096, lines=500 if expected == "全部入った" else 1000,
                 prompt_tokens=prompt_tokens, truncated=truncated, elapsed_sec=1.0, error=error)
    assert po.verdict(p, tokens_per_line=2.0) == expected


def test_calibrate_uses_difference_of_two_sizes_to_exclude_system_overhead():
    """system プロンプト等の固定分（ここでは 50）を除くため、行数の違う 2 回の差から求める。"""
    def fake(**kwargs):
        n = kwargs["messages"][0]["content"].count("\n") + 1
        return SimpleNamespace(usage=SimpleNamespace(input_tokens=50 + 30 * n), input_truncated=False)

    assert po.calibrate("ollama:x", complete=fake) == 30.0


def test_run_probes_each_num_ctx_and_fill_and_reads_effective_context_length():
    """num_ctx × 埋める割合をすべて送り、num_ctx ごとに /api/ps の実効値と VRAM を読む。"""
    fake = _FakeComplete(input_tokens=10)
    ps_calls: list[str] = []

    def ps(model):
        ps_calls.append(os.environ.get("OLLAMA_NUM_CTX", ""))
        return {"context_length": 32768, "size_vram": 20 * 1024**3}

    rows = po.run("ollama:x", ctxs=[32768, 65536], fills=[0.5, 1.2], tokens_per_line=2.0,
                  complete=fake, ps=ps)

    assert [(r.probe.num_ctx, r.probe.lines) for r in rows] == [
        (32768, 8192), (32768, 19660), (65536, 16384), (65536, 39321),
    ]
    assert len(ps_calls) == 2
    assert {(r.context_length, r.vram_gb) for r in rows} == {(32768, 20.0)}


def _row(num_ctx, *, fill, verdict_="全部入った", context_length=None):
    lines = int(num_ctx * fill / 2)
    truncated = verdict_ == "切り捨てを検知"
    tokens = {"全部入った": lines * 2, "切り捨てを検知": num_ctx, "検知漏れ": num_ctx // 2, "失敗": 0}
    p = po.Probe(num_ctx, lines, tokens[verdict_], truncated, 1.0, "oom" if verdict_ == "失敗" else "")
    return po.Row(p, fill, verdict_, context_length or num_ctx, 20.0)


def test_recommend_picks_largest_num_ctx_that_fits_and_is_applied():
    rows = [
        _row(32768, fill=0.9), _row(32768, fill=1.2, verdict_="切り捨てを検知"),
        _row(65536, fill=0.9), _row(65536, fill=1.2, verdict_="切り捨てを検知"),
        _row(131072, fill=0.9, verdict_="失敗"),
    ]
    assert po.recommend(rows) == 65536


def test_recommend_rejects_num_ctx_not_applied_or_with_silent_truncation():
    """実効値が指定と違う（黙って縮められた）もの、溢れを見逃したものは推奨しない。"""
    rows = [
        _row(32768, fill=0.9), _row(32768, fill=1.2, verdict_="切り捨てを検知"),
        _row(65536, fill=0.9, context_length=32768),
        _row(131072, fill=0.9), _row(131072, fill=1.2, verdict_="検知漏れ"),
    ]
    assert po.recommend(rows) == 32768
    assert po.recommend([_row(32768, fill=0.9, verdict_="失敗")]) is None


def test_recommend_allows_error_only_on_overfilled_input():
    """溢れる入力をエラーで拒否するのは正しい振る舞い（黙って切るより良い）。除外理由にしない。"""
    rows = [_row(65536, fill=0.9), _row(65536, fill=1.2, verdict_="失敗")]
    assert po.recommend(rows) == 65536


def test_ollama_ps_reads_loaded_model_entry(monkeypatch):
    """/api/ps から、指定モデルの実効コンテキスト長と VRAM 使用量を読む（無ければ 0）。"""
    from log_analyzer import llm_client

    monkeypatch.setenv("OLLAMA_BASE_URL", "http://localhost:11435")
    seen: list[str] = []

    def fake_get(url, timeout):
        seen.append(url)
        return {"models": [
            {"name": "other:1b", "context_length": 4096, "size_vram": 1},
            {"name": "gemma4:31b-it-q4_K_M", "context_length": 65536, "size_vram": 30},
        ]}

    monkeypatch.setattr(llm_client, "_ollama_get", fake_get)

    assert po.ollama_ps("ollama:gemma4:31b-it-q4_K_M") == {"context_length": 65536, "size_vram": 30}
    assert po.ollama_ps("ollama:missing") == {}
    assert seen[0] == "http://localhost:11435/api/ps"


def test_main_prints_table_and_recommendation(capsys):
    """表（num_ctx・割合・行数・入力トークン・判定・実効値・VRAM）と推奨値を出す。推奨値があれば 0 で終わる。"""
    loaded = {"num_ctx": 0}  # 実物の /api/ps は、直前に読み込んだときの num_ctx を返す

    def fake(**kwargs):
        n = kwargs["messages"][0]["content"].count("\n") + 1
        cap = loaded["num_ctx"] = int(os.environ["OLLAMA_NUM_CTX"])
        tokens = min(2 * n, cap)
        return SimpleNamespace(usage=SimpleNamespace(input_tokens=tokens), input_truncated=tokens >= cap)

    def ps(model):
        return {"context_length": loaded["num_ctx"], "size_vram": 0}

    code = po.main(["--model", "gemma4:26b-a4b-it-q4_K_M", "--ctx", "32768,65536", "--fill", "0.5,1.2"],
                   complete=fake, ps=ps)

    out = capsys.readouterr().out
    assert code == 0
    assert "1 行あたり 2.0 トークン" in out
    assert "切り捨てを検知" in out and "全部入った" in out
    assert "推奨 OLLAMA_NUM_CTX = 65536" in out
