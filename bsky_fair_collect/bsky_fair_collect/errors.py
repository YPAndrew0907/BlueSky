from __future__ import annotations

from dataclasses import dataclass

from bsky_fair_collect.state import StateDB
from bsky_fair_collect.utils import utc_now_iso


@dataclass(frozen=True)
class RecordedError:
    stage: str
    key: str
    error_type: str
    http_status: int | None
    error_message: str
    when_utc: str
    retry_count: int


def truncate_message(message: str, *, max_len: int = 500) -> str:
    if len(message) <= max_len:
        return message
    return message[:max_len] + "…"


def record_error(
    state: StateDB,
    *,
    stage: str,
    key: str,
    error_type: str,
    error_message: str,
    http_status: int | None = None,
    retry_count: int = 0,
) -> None:
    state.conn.execute(
        """
        INSERT INTO errors(stage, key, error_type, http_status, error_message, when_utc, retry_count)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            stage,
            key,
            error_type,
            http_status,
            truncate_message(error_message),
            utc_now_iso(),
            retry_count,
        ),
    )
    state.conn.commit()

