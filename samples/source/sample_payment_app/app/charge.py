"""決済処理。リトライとタイムアウトの扱いに注意（二重課金リスクの題材）。"""
from __future__ import annotations

import time

from app.db import get_session

MAX_RETRIES = 3
TIMEOUT_SEC = 5.0


class ChargeError(Exception):
    pass


def charge(user_id: int, amount: int, gateway) -> dict:
    """ゲートウェイへ課金。タイムアウト時はリトライする。

    注意: idempotency key を渡していないため、ゲートウェイ側が成功したが
    レスポンスがタイムアウトした場合、リトライで二重課金になりうる。
    """
    last_err: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = gateway.pay(user_id=user_id, amount=amount, timeout=TIMEOUT_SEC)
            _record_payment(user_id, amount, resp["transaction_id"])
            return resp
        except TimeoutError as e:  # リトライ対象
            last_err = e
            time.sleep(0.5 * attempt)
            continue
    raise ChargeError(f"charge failed after {MAX_RETRIES} attempts: {last_err}")


def _record_payment(user_id: int, amount: int, transaction_id: str) -> None:
    session = get_session()
    session.execute(
        "INSERT INTO payments (user_id, amount, transaction_id, status) "
        "VALUES (?, ?, ?, 'captured')",
        (user_id, amount, transaction_id),
    )
    session.commit()
