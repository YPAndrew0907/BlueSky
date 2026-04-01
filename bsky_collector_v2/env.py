from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import dotenv_values


@dataclass(frozen=True)
class AuthEnv:
    identifier: str
    app_password: str
    pds_host: str = "https://bsky.social"


def load_auth_env(path: Path) -> AuthEnv:
    values = dotenv_values(str(path))
    identifier = _require(values, "BSKY_IDENTIFIER", "BSKY_HANDLE", "BLUESKY_IDENTIFIER", "BLUESKY_HANDLE")
    app_password = _require(values, "BSKY_APP_PASSWORD", "BLUESKY_APP_PASSWORD")
    pds_host = str(
        values.get("BSKY_PDS_HOST")
        or values.get("BLUESKY_PDS_HOST")
        or values.get("BLUESKY_PDS")
        or os.environ.get("BSKY_PDS_HOST")
        or os.environ.get("BLUESKY_PDS_HOST")
        or os.environ.get("BLUESKY_PDS")
        or "https://bsky.social"
    )
    return AuthEnv(identifier=identifier, app_password=app_password, pds_host=pds_host)


def _require(values: dict[str, str | None], *keys: str) -> str:
    for k in keys:
        v = values.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
        v = os.environ.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    raise ValueError(f"missing required auth env var(s): {', '.join(keys)}")
