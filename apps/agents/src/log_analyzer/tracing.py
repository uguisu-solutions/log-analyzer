"""Langfuse tracing helpers shared by every configuration."""
from __future__ import annotations

import os
from functools import lru_cache
from typing import Any

from langfuse import Langfuse


@lru_cache(maxsize=1)
def get_client() -> Langfuse:
    return Langfuse(
        public_key=os.environ["LANGFUSE_PUBLIC_KEY"],
        secret_key=os.environ["LANGFUSE_SECRET_KEY"],
        host=os.environ.get("LANGFUSE_HOST", "http://localhost:3000"),
    )


def flush() -> None:
    get_client().flush()


# 1M トークンあたりの USD 単価 (input, output)。Langfuse OSS は新しめ/独自の
# モデル ID を既定の価格表に持たないため、コスト表示用にこちらで明示計算する。
# Claude 系は claude-api リファレンス準拠 (2026-06 時点)。
# gpt-5.5 は監査エージェント用 (確認事項 B-1 で Generation 化したため単価を登録。
# 2026-08 時点の OpenAI 公開価格 $5 / $30 per MTok)。
# 未収載モデルはトークンのみ記録しコストは省略する。
_MODEL_PRICES_PER_MTOK: dict[str, tuple[float, float]] = {
    "claude-fable-5": (10.0, 50.0),
    "claude-opus-4-8": (5.0, 25.0),
    "claude-opus-4-7": (5.0, 25.0),
    "claude-opus-4-6": (5.0, 25.0),
    "claude-opus-4-5": (5.0, 25.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-sonnet-4-5": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
    "gpt-5.5": (5.0, 30.0),
}


def _model_price(model: str | None) -> tuple[float, float] | None:
    if not model:
        return None
    m = str(model).strip().lower()
    if m in _MODEL_PRICES_PER_MTOK:
        return _MODEL_PRICES_PER_MTOK[m]
    # 日付サフィックス等に備えた接頭一致 (例 claude-haiku-4-5-20251001)
    for key, price in _MODEL_PRICES_PER_MTOK.items():
        if m.startswith(key):
            return price
    return None


# prompt caching の課金倍率 (base input 価格に対する係数)。
# 5 分 TTL の cache 書き込み = 1.25x, cache 読み出し = 0.1x (claude-api 準拠)。
_CACHE_WRITE_MULT = 1.25
_CACHE_READ_MULT = 0.1


def usage_components(usage: Any) -> dict[str, int]:
    """Anthropic の ``response.usage`` を 4 分解した dict にする。

    prompt caching 有効時、``input_tokens`` は**非キャッシュ分のみ**で、
    キャッシュ書き込み/読み出しは別フィールドに入る。caching 無効時は両者 0。
    """
    return {
        "input": int(getattr(usage, "input_tokens", 0) or 0),
        "cache_creation": int(getattr(usage, "cache_creation_input_tokens", 0) or 0),
        "cache_read": int(getattr(usage, "cache_read_input_tokens", 0) or 0),
        "output": int(getattr(usage, "output_tokens", 0) or 0),
    }


def cost_usd(
    model: str | None,
    tokens_in: Any,
    tokens_out: Any,
    *,
    cache_creation: Any = 0,
    cache_read: Any = 0,
) -> float | None:
    """1 回の LLM 呼び出しの推定コスト (USD) を返す。未収載モデルは ``None``。

    確認事項 D-2 で ``metrics.cost_usd`` を実計算するために切り出した。
    Langfuse 送信用の :func:`usage_for` と**同じ計算**を共有する
    (prompt caching の倍率: 書込 ×1.25 / 読出 ×0.1)。

    ``tokens_in`` は入力処理トークンの総量 (非キャッシュ + 書込 + 読出)。
    ``None`` を返した場合、呼び出し側は 0 とみなさず「単価未登録」として
    扱うこと (0 を「無料」と誤読させないため)。
    """
    price = _model_price(model)
    if price is None:
        return None
    p_in, p_out = price
    ti, to = int(tokens_in or 0), int(tokens_out or 0)
    cc, cr = int(cache_creation or 0), int(cache_read or 0)
    uncached = max(0, ti - cc - cr)
    billable_in = uncached + cc * _CACHE_WRITE_MULT + cr * _CACHE_READ_MULT
    return billable_in / 1_000_000 * p_in + to / 1_000_000 * p_out


def usage_for(
    model: str | None,
    tokens_in: Any,
    tokens_out: Any,
    *,
    cache_creation: Any = 0,
    cache_read: Any = 0,
) -> dict[str, Any]:
    """Langfuse v2 の ``generation(usage=...)`` 用 ModelUsage 辞書を作る。

    `usage_details` (v3 形式) はローカル Langfuse OSS が Usage 列に集計しないため、
    v2 標準の `usage` (`input`/`output`/`unit`) を使う。

    ``tokens_in`` は **入力処理トークンの総量** (= 非キャッシュ + cache 書込 + cache 読出)
    を渡す。コストは prompt caching の倍率 (書込 ×1.25 / 読出 ×0.1) を反映して計算し、
    input_cost/output_cost/total_cost を付与する。caching 無し (cache_* = 0) なら
    従来どおり tokens_in 全量を ×1.0 で課金。
    """
    ti = int(tokens_in or 0)
    to = int(tokens_out or 0)
    cc = int(cache_creation or 0)
    cr = int(cache_read or 0)
    uncached = max(0, ti - cc - cr)  # 非キャッシュ入力 (×1.0 課金)
    usage: dict[str, Any] = {"input": ti, "output": to, "unit": "TOKENS"}
    price = _model_price(model)
    if price:
        p_in, p_out = price
        billable_in = uncached + cc * _CACHE_WRITE_MULT + cr * _CACHE_READ_MULT
        ic = billable_in / 1_000_000 * p_in
        oc = to / 1_000_000 * p_out
        usage["input_cost"] = ic
        usage["output_cost"] = oc
        usage["total_cost"] = ic + oc
    return usage
