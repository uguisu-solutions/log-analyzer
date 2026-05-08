"""ルールベースの前処理フィルタ（構成2 第1段）。

ERROR/WARN 行は逐次保持し、KEEPALIVE 等の頻出 INFO は件数のみに集約することで
入力サイズを Sonnet 推論前に大幅圧縮する。集約しきれなかった INFO 行は
``other_info_count`` でカウントされ、`info_loss_flags` の根拠になる。
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass

SEVERITY_PATTERN = re.compile(r"\[(ERROR|WARN|FATAL|CRITICAL)\]")

# 正常運用で頻出するパターンは件数集計のみに丸める
NORMAL_PATTERNS: dict[str, re.Pattern[str]] = {
    "keepalive": re.compile(r"keepalive"),
    "interface_up": re.compile(r"interface .+ link up"),
    "connection_established": re.compile(r"connection established"),
    "policy_applied": re.compile(r"policy v\d+ -> v\d+ applied"),
    "auto_page_sent": re.compile(r"auto-page sent"),
}


@dataclass
class FilterResult:
    anomaly_lines: list[str]
    normal_counts: dict[str, int]
    other_info_count: int
    original_lines: int
    original_bytes: int
    filtered_bytes: int

    @property
    def compression_ratio(self) -> float:
        if self.original_bytes == 0:
            return 0.0
        return self.filtered_bytes / self.original_bytes


def filter_log(text: str) -> FilterResult:
    lines = text.splitlines()
    anomaly: list[str] = []
    counts: Counter[str] = Counter()
    other_info = 0

    for line in lines:
        if SEVERITY_PATTERN.search(line):
            anomaly.append(line)
            continue
        matched = False
        for name, pattern in NORMAL_PATTERNS.items():
            if pattern.search(line):
                counts[name] += 1
                matched = True
                break
        if not matched and line.strip():
            other_info += 1

    filtered_text = "\n".join(anomaly)
    return FilterResult(
        anomaly_lines=anomaly,
        normal_counts=dict(counts),
        other_info_count=other_info,
        original_lines=len(lines),
        original_bytes=len(text.encode("utf-8")),
        filtered_bytes=len(filtered_text.encode("utf-8")),
    )
