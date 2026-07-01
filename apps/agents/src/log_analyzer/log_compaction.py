"""生ログの反復行を畳み込む前処理（入力トークン削減）。

実ログ（syslog / CSV ダンプ等）は、タイムスタンプや id・IP だけが違う
**ほぼ同一の行が大量に反復**する。これを丸ごと log_text に貼ると、その
log_text がパイプライン全体で 9 回ほど再送され、入力トークン（=コスト）の
大半を占める（実測: A ケースの生ログ約 7MB ≒ 236 万トークン）。

本モジュールは行を「テンプレート化」（可変トークンをマスク）してグルーピングし、
各テンプレートにつき先頭数件だけ原文で残し、残りは末尾の集約サマリに件数で
畳み込む。フォーマット非依存で、syslog でも CSV でも効く。

設計方針:
- **順序を保つ**: 残す行は元の並び順のまま（バースト→収束のような時系列の手掛かりを維持）。
- **ロスは明示**: 畳み込んだ件数をサマリに出し、LLM に「これは完全ログではない」と伝える。
- **可逆・安全**: 環境変数で無効化でき、小さいログ（既定 200 行未満）は素通し。
- **信号は残す**: 各テンプレートの先頭 N 件は原文なので、実 id/IP の例は必ず残る。

``filters.py`` は構成2 の ``[ERROR]`` 前提フィルタで別物。本モジュールは
フォーマットを仮定しない汎用の反復圧縮を担う。
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass


# ─── 予算（環境変数で調整可能。source_tools に倣う）────────────────


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _enabled() -> bool:
    # 既定 ON。"0" / "false" / "no" で無効化。
    raw = (os.environ.get("LOG_COMPACT_ENABLED") or "1").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def _max_examples() -> int:
    # 1 テンプレートあたり原文で残す先頭件数。
    return max(1, _env_int("LOG_COMPACT_MAX_EXAMPLES", 3))


def _min_lines() -> int:
    # この行数未満のログは畳み込まず素通し（小さいログを触らない）。
    return _env_int("LOG_COMPACT_MIN_LINES", 200)


def _summary_preview_chars() -> int:
    return _env_int("LOG_COMPACT_PREVIEW_CHARS", 160)


# ─── テンプレート化（可変トークンをマスク）──────────────────────
#
# 適用順が重要。より具体的なパターン（ISO 日時 / UUID / MAC / IP）を先に消し、
# 最後に汎用の数値を消す。順序を誤ると IP が数値マスクで壊れる。

_MASKS: list[tuple[re.Pattern[str], str]] = [
    # ISO8601 日時（T または空白区切り、ミリ秒・TZ 任意）
    (re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?"), "<TS>"),
    # syslog 形式の日付（例: Jul  2 10:00:00）
    (re.compile(r"[A-Z][a-z]{2}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2}"), "<TS>"),
    # 時刻のみ（例: 10:00:00.123）
    (re.compile(r"\b\d{2}:\d{2}:\d{2}(?:\.\d+)?\b"), "<TS>"),
    # UUID
    (re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"), "<UUID>"),
    # MAC アドレス（: または - 区切り）
    (re.compile(r"\b(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}\b"), "<MAC>"),
    # IPv4（ポート任意）
    (re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?\b"), "<IP>"),
    # 0x 付き16進 / 長い16進列
    (re.compile(r"\b0x[0-9a-fA-F]+\b"), "<HEX>"),
    (re.compile(r"\b[0-9a-fA-F]{16,}\b"), "<HEX>"),
    # 汎用の数値（最後に適用）
    (re.compile(r"\b\d+\b"), "<N>"),
]


def _templatize(line: str) -> str:
    """行から可変トークンをマスクしてテンプレート文字列を返す。"""
    t = line
    for pattern, repl in _MASKS:
        t = pattern.sub(repl, t)
    return t


@dataclass
class CompactionResult:
    text: str
    original_lines: int
    kept_lines: int
    dropped_lines: int
    template_count: int
    original_bytes: int
    compacted_bytes: int

    @property
    def compression_ratio(self) -> float:
        if self.original_bytes == 0:
            return 1.0
        return self.compacted_bytes / self.original_bytes


def compact_log(
    text: str,
    *,
    max_examples: int | None = None,
    min_lines: int | None = None,
) -> CompactionResult:
    """反復行を畳み込んだログを返す。

    各テンプレートにつき先頭 ``max_examples`` 件は原文のまま元の順序で残し、
    それを超えた行は末尾の「省略サマリ」に件数で集約する。行数が ``min_lines``
    未満なら何もしない（原文をそのまま返す）。
    """
    max_ex = _max_examples() if max_examples is None else max(1, max_examples)
    min_ln = _min_lines() if min_lines is None else min_lines

    lines = text.splitlines()
    original_bytes = len(text.encode("utf-8"))
    n = len(lines)

    if n < min_ln:
        return CompactionResult(
            text=text,
            original_lines=n,
            kept_lines=n,
            dropped_lines=0,
            template_count=0,
            original_bytes=original_bytes,
            compacted_bytes=original_bytes,
        )

    shown: dict[str, int] = {}     # template -> これまで原文で出した件数
    dropped: dict[str, int] = {}   # template -> 畳み込んだ件数
    first_template_order: list[str] = []
    kept: list[str] = []

    for line in lines:
        if not line.strip():
            # 空行はテンプレート化せずそのまま（過剰な畳み込みを避ける）。ただし
            # 連続空行は 1 行に潰す。
            if kept and kept[-1] == "":
                continue
            kept.append("")
            continue
        tmpl = _templatize(line)
        if tmpl not in shown:
            shown[tmpl] = 0
            dropped[tmpl] = 0
            first_template_order.append(tmpl)
        if shown[tmpl] < max_ex:
            kept.append(line)
            shown[tmpl] += 1
        else:
            dropped[tmpl] += 1

    total_dropped = sum(dropped.values())
    kept_body = "\n".join(kept).rstrip()

    if total_dropped == 0:
        # 反復が無く畳み込めなかった → 原文を返す（サマリも付けない）。
        out = kept_body
        return CompactionResult(
            text=out,
            original_lines=n,
            kept_lines=len(kept),
            dropped_lines=0,
            template_count=len(first_template_order),
            original_bytes=original_bytes,
            compacted_bytes=len(out.encode("utf-8")),
        )

    # 省略サマリ（畳み込んだテンプレートのみ、件数降順）
    preview_cap = _summary_preview_chars()
    summarized = [t for t in first_template_order if dropped[t] > 0]
    summarized.sort(key=lambda t: dropped[t], reverse=True)
    summary_lines = [
        "",
        f"── 省略サマリ（同種行を集約｜元 {n} 行 → 表示 {len(kept)} 行、"
        f"{total_dropped} 行を畳み込み）──",
        "以下は反復のため省略した行パターンと件数。先頭数件は上に原文で表示済み。",
    ]
    for t in summarized:
        preview = t if len(t) <= preview_cap else t[:preview_cap] + "…"
        summary_lines.append(f"[×{dropped[t]}] {preview}")

    out = kept_body + "\n" + "\n".join(summary_lines) + "\n"
    return CompactionResult(
        text=out,
        original_lines=n,
        kept_lines=len(kept),
        dropped_lines=total_dropped,
        template_count=len(first_template_order),
        original_bytes=original_bytes,
        compacted_bytes=len(out.encode("utf-8")),
    )


def compact_log_text(text: str) -> str:
    """環境変数ゲート付きの薄いラッパ。無効時・非圧縮時は原文を返す。

    log_text 構築側から呼ぶ用。統計が要らない箇所はこちらを使う。
    """
    if not _enabled():
        return text
    return compact_log(text).text
