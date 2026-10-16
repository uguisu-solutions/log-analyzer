r"""Ollama の num_ctx の実測（docs/plan/slm_support.md §7-1 / §7-3、Phase 5 の最初に行う）。

わざと長い入力を送り、次の 2 点を確かめる。
- num_ctx をどこまで上げられるか（VRAM に載るか・実効値が指定どおりか）
- 入力が num_ctx を超えたとき、アプリの切り捨て検知（``input_truncated``）が立つか

送信はアプリと同じ ``llm_client.complete`` を通す（ペイロードも切り捨ての判定もアプリそのもの）。
"""
from __future__ import annotations

import math
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

# 出力は要らない（入力をどこまで処理したかだけを見る）。生成に時間を使わないよう短くする
_MAX_TOKENS = 8
_SYSTEM = "次のログを読み、「OK」とだけ答えてください。"

Complete = Callable[..., Any]


@dataclass(frozen=True)
class Probe:
    """1 回の計測。``error`` は呼び出しが失敗したとき（VRAM 不足など）の理由。"""

    num_ctx: int
    lines: int
    prompt_tokens: int
    truncated: bool
    elapsed_sec: float
    error: str = ""


def plan_lines(num_ctx: int, *, fills: list[float], tokens_per_line: float) -> list[int]:
    """num_ctx の ``fill`` 割を埋める行数。1.0 を超える割合は、わざと溢れさせる入力になる。"""
    return [math.floor(num_ctx * f / tokens_per_line) for f in fills]


# 較正に使う行数。どちらも 32768 に十分収まる量にする
_CALIBRATE_LINES = (100, 300)
_CALIBRATE_NUM_CTX = 32768


def calibrate(model: str, *, complete: Complete) -> float:
    """1 行あたりのトークン数。system プロンプト等の固定分を除くため、2 回の差から求める。"""
    small, large = (
        probe_once(model, num_ctx=_CALIBRATE_NUM_CTX, lines=n, complete=complete)
        for n in _CALIBRATE_LINES
    )
    for p in (small, large):
        if p.error:
            raise RuntimeError(f"較正に失敗した（接続先・モデル名を確認）: {p.error}")
    return (large.prompt_tokens - small.prompt_tokens) / (large.lines - small.lines)


# 送ったはずの量の何割を下回ったら「切られた」とみなすか（行ごとのトークン数の揺れを見込む）
_LOSS_RATIO = 0.9


def verdict(p: Probe, *, tokens_per_line: float) -> str:
    """1 回の計測の判定。「検知漏れ」は、切られたのにアプリの印が立たなかったもの（§7-1 の誤った結論の元）。"""
    if p.error:
        return "失敗"
    if p.truncated:
        return "切り捨てを検知"
    if p.prompt_tokens < p.lines * tokens_per_line * _LOSS_RATIO:
        return "検知漏れ"
    return "全部入った"


def filler(lines: int) -> str:
    """合成のログ行（実ログは使わない）。行ごとに値を変え、同じ行の繰り返しにしない。"""
    return "\n".join(
        f"2026-10-16T00:{i // 60 % 60:02d}:{i % 60:02d}Z fw-01 DENY tcp "
        f"src=10.0.{i % 200}.{i % 250 + 1}:{40000 + i % 20000} dst=10.0.20.5:443 seq={i}"
        for i in range(lines)
    )


def probe_once(model: str, *, num_ctx: int, lines: int, complete: Complete) -> Probe:
    """``num_ctx`` を呼び出しの間だけ差し替えて 1 回送る。"""
    saved = os.environ.get("OLLAMA_NUM_CTX")
    os.environ["OLLAMA_NUM_CTX"] = str(num_ctx)
    started = time.monotonic()
    try:
        r = complete(
            model=model,
            system=_SYSTEM,
            messages=[{"role": "user", "content": filler(lines)}],
            max_tokens=_MAX_TOKENS,
        )
    except Exception as e:  # noqa: BLE001 — VRAM 不足なども計測結果として残し、次の条件へ進む
        return Probe(num_ctx, lines, 0, False, round(time.monotonic() - started, 1), str(e))
    finally:
        if saved is None:
            os.environ.pop("OLLAMA_NUM_CTX", None)
        else:
            os.environ["OLLAMA_NUM_CTX"] = saved
    return Probe(
        num_ctx=num_ctx,
        lines=lines,
        prompt_tokens=int(r.usage.input_tokens),
        truncated=bool(r.input_truncated),
        elapsed_sec=round(time.monotonic() - started, 1),
    )


Ps = Callable[[str], dict]


@dataclass(frozen=True)
class Row:
    """表の 1 行。``context_length`` / ``vram_gb`` は送った直後の /api/ps の値（載っていなければ 0）。"""

    probe: Probe
    fill: float
    verdict: str
    context_length: int
    vram_gb: float


def run(
    model: str,
    *,
    ctxs: list[int],
    fills: list[float],
    tokens_per_line: float,
    complete: Complete,
    ps: Ps,
) -> list[Row]:
    """num_ctx ごとに、埋める割合を順に送る。実効値は num_ctx ごとに 1 回読む。"""
    rows: list[Row] = []
    for num_ctx in ctxs:
        lines = plan_lines(num_ctx, fills=fills, tokens_per_line=tokens_per_line)
        probes = [probe_once(model, num_ctx=num_ctx, lines=n, complete=complete) for n in lines]
        loaded = ps(model)
        context_length = int(loaded.get("context_length") or 0)
        vram_gb = round(int(loaded.get("size_vram") or 0) / 1024**3, 1)
        rows += [
            Row(p, f, verdict(p, tokens_per_line=tokens_per_line), context_length, vram_gb)
            for p, f in zip(probes, fills, strict=True)
        ]
    return rows


def recommend(rows: list[Row]) -> int | None:
    """使ってよい最大の num_ctx。失敗・実効値の不一致・検知漏れが 1 つでもある num_ctx は除く。

    1.0 を超えて埋めた計測の「失敗」は除外理由にしない（溢れる入力を拒否するのは正しい振る舞い）。
    """
    ok: list[int] = []
    for num_ctx in sorted({r.probe.num_ctx for r in rows}):
        group = [r for r in rows if r.probe.num_ctx == num_ctx]
        if any(r.verdict == "検知漏れ" for r in group):
            continue
        if any(r.verdict == "失敗" and r.fill <= 1.0 for r in group):
            continue
        if any(r.context_length != num_ctx for r in group):
            continue
        ok.append(num_ctx)
    return max(ok) if ok else None


def ollama_ps(model: str) -> dict:
    """/api/ps のうち指定モデルの行（``context_length`` / ``size_vram``）。読み込まれていなければ空。"""
    from log_analyzer import llm_client

    tag = model.removeprefix(llm_client.OLLAMA_PREFIX)
    data = llm_client._ollama_get(f"{llm_client.ollama_base_url()}/api/ps", timeout=10)
    for m in data.get("models") or []:
        if m.get("name") == tag:
            return {"context_length": m.get("context_length"), "size_vram": m.get("size_vram")}
    return {}


def _print_table(rows: list[Row]) -> None:
    print(f"{'num_ctx':>8} {'割合':>5} {'行数':>7} {'入力tok':>8} {'秒':>6} {'実効ctx':>8} {'VRAM GB':>8}  判定")
    for r in rows:
        p = r.probe
        print(f"{p.num_ctx:>8} {r.fill:>5.2f} {p.lines:>7} {p.prompt_tokens:>8} {p.elapsed_sec:>6} "
              f"{r.context_length:>8} {r.vram_gb:>8}  {r.verdict}{'（' + p.error + '）' if p.error else ''}")


def main(argv: list[str] | None = None, *, complete: Complete | None = None, ps: Ps | None = None) -> int:
    import argparse

    from log_analyzer import llm_client

    ap = argparse.ArgumentParser(description="Ollama の num_ctx を実測する（Phase 5 の最初に 1 回）")
    ap.add_argument("--model", required=True, help="Ollama のタグ（ollama: は付けても付けなくてもよい）")
    ap.add_argument("--ctx", default="32768,65536,98304,131072", help="試す num_ctx（カンマ区切り）")
    ap.add_argument("--fill", default="0.5,0.9,1.2", help="num_ctx の何割を埋めるか（1.0 超は溢れさせる）")
    args = ap.parse_args(argv)

    model = args.model if args.model.startswith(llm_client.OLLAMA_PREFIX) else llm_client.OLLAMA_PREFIX + args.model
    ctxs = [int(c) for c in args.ctx.split(",")]
    fills = [float(f) for f in args.fill.split(",")]
    complete = complete or llm_client.complete
    ps = ps or ollama_ps

    print(f"接続先: {llm_client.ollama_base_url()}  モデル: {model}")
    tokens_per_line = calibrate(model, complete=complete)
    print(f"1 行あたり {tokens_per_line:.1f} トークン")
    rows = run(model, ctxs=ctxs, fills=fills, tokens_per_line=tokens_per_line, complete=complete, ps=ps)
    _print_table(rows)

    if any(r.verdict == "検知漏れ" for r in rows):
        print("警告: 溢れた入力でアプリの切り捨て検知が立たなかった（llm_client の判定を見直す必要がある）")
    best = recommend(rows)
    if best is None:
        print("推奨できる num_ctx が無い（すべて失敗・実効値の不一致・検知漏れのいずれか）")
        return 1
    print(f"推奨 OLLAMA_NUM_CTX = {best}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
