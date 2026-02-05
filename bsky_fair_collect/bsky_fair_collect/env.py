from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
from typing import Final

from dotenv import dotenv_values


ENV_LOCAL_PATH: Final[Path] = Path("/Users/yipengandrewwang/BlueSky/.env.local")


@dataclass(frozen=True)
class Credentials:
    handle: str | None
    app_password: str | None
    pds: str
    access_jwt: str | None
    refresh_jwt: str | None


def _pick_env(
    values: dict[str, str | None],
    preferred_key: str,
    *,
    aliases: tuple[str, ...] = (),
) -> str | None:
    preferred = values.get(preferred_key) or os.environ.get(preferred_key)
    if preferred:
        return preferred
    for key in aliases:
        v = values.get(key) or os.environ.get(key)
        if v:
            return v
    return None


def load_credentials(env_path: Path = ENV_LOCAL_PATH) -> Credentials | None:
    if not env_path.exists():
        return None

    values = dotenv_values(env_path)

    handle = _pick_env(values, "BSKY_HANDLE", aliases=("BLUESKY_HANDLE", "BLUESKY_IDENTIFIER"))
    app_password = _pick_env(values, "BSKY_APP_PASSWORD", aliases=("BLUESKY_APP_PASSWORD",))
    pds = _pick_env(values, "BSKY_PDS", aliases=("BLUESKY_PDS",)) or "https://bsky.social"
    access_jwt = _pick_env(values, "BSKY_ACCESS_JWT", aliases=("BLUESKY_ACCESS_JWT",))
    refresh_jwt = _pick_env(values, "BSKY_REFRESH_JWT", aliases=("BLUESKY_REFRESH_JWT",))

    # Allow token-only usage for strictly read-only auth contexts.
    if not handle and not access_jwt:
        return None

    return Credentials(
        handle=handle,
        app_password=app_password,
        pds=pds,
        access_jwt=access_jwt,
        refresh_jwt=refresh_jwt,
    )
