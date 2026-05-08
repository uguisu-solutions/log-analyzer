"""Langfuse tracing helpers shared by every configuration."""
from __future__ import annotations

import os
from functools import lru_cache

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
