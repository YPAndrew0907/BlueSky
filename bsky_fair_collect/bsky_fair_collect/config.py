from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path


class AuthMode(str, Enum):
    UNAUTH = "unauth"
    AUTH = "auth"
    BOTH = "both"


class ViewerMode(str, Enum):
    UNAUTH = "unauth"
    AUTH = "auth"


@dataclass(frozen=True)
class Hosts:
    appview_host: str = "https://public.api.bsky.app"
    relay_host: str = "https://relay1.us-east.bsky.network"


@dataclass(frozen=True)
class RunParams:
    rps: float = 5.0
    max_retries: int = 7
    posts_per_feed: int = 200
    n_discovery: int = 2000
    n_popular: int = 2000
    n_less_known: int = 2000

    starterpack_queries: tuple[str, ...] = (
        "news",
        "sports",
        "tech",
        "science",
        "art",
        "music",
        "movies",
        "gaming",
        "books",
        "politics",
        "finance",
        "crypto",
        "photography",
        "design",
        "writing",
        "history",
        "nature",
        "travel",
        "food",
        "cats",
        "dogs",
        "anime",
        "kpop",
        *tuple("abcdefghijklmnopqrstuvwxyz"),
    )

    starterpack_max_per_query: int = 500
    starterpack_query_limit: int | None = None
    starterpack_actor_limit: int | None = None
    popular_page_limit: int = 100
    relay_page_limit: int = 1000
    actor_feeds_page_limit: int = 100
    feed_page_limit: int = 100
    profiles_batch_size: int = 25

    # Optional dev/smoke-test limits (None = no limit).
    index_max_actors: int | None = None


@dataclass(frozen=True)
class OutputPaths:
    out_dir: Path
    csv_dir: Path
    raw_dir: Path
    logs_dir: Path
    state_dir: Path

    @staticmethod
    def for_out_dir(out_dir: Path) -> "OutputPaths":
        return OutputPaths(
            out_dir=out_dir,
            csv_dir=out_dir / "csv",
            raw_dir=out_dir / "raw",
            logs_dir=out_dir / "logs",
            state_dir=out_dir / "state",
        )


@dataclass(frozen=True)
class AppConfig:
    outputs: OutputPaths
    hosts: Hosts
    auth_mode: AuthMode
    run: RunParams
