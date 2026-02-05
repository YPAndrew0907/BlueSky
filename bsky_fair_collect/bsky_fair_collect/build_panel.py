from __future__ import annotations

import logging
import random
from dataclasses import dataclass

from bsky_fair_collect.config import AppConfig, AuthMode
from bsky_fair_collect.errors import record_error
from bsky_fair_collect.parse_utils import parse_at_uri
from bsky_fair_collect.state import StateDB
from bsky_fair_collect.utils import utc_now_iso

logger = logging.getLogger("bsky_fair_collect.stage.build_panel")


@dataclass(frozen=True)
class PanelRow:
    feed_uri: str
    feed_group: str
    selection_reason: str
    provider_bucket: str | None
    service_did: str | None
    creator_did: str | None
    display_name: str | None
    inclusion_count: int | None
    popularity_rank: int | None


def stage_build_feed_panel(cfg: AppConfig, state: StateDB) -> None:
    logger.info(
        "stage=start name=build_feed_panel targets discovery=%s popular=%s less_known=%s",
        cfg.run.n_discovery,
        cfg.run.n_popular,
        cfg.run.n_less_known,
    )

    if state.get_meta("snapshots_started") == "1":
        logger.info("build_feed_panel skip reason=snapshots_started")
        return

    run_id = state.get_meta("run_id") or ""
    seed = int(run_id[:8], 16) if run_id else 0
    rng = random.Random(seed)

    discovery = _select_discovery(state, n=cfg.run.n_discovery)
    popular = _select_popular(state, n=cfg.run.n_popular, exclude=set(discovery))

    missing_discovery = max(0, cfg.run.n_discovery - len(discovery))
    missing_popular = max(0, cfg.run.n_popular - len(popular))
    desired_less_known = cfg.run.n_less_known + missing_discovery + missing_popular
    if missing_discovery or missing_popular:
        logger.warning(
            "build_feed_panel filling deficits into less_known missing_discovery=%s missing_popular=%s desired_less_known=%s",
            missing_discovery,
            missing_popular,
            desired_less_known,
        )

    less_known = _select_less_known(
        state,
        n=desired_less_known,
        exclude=set(discovery) | set(popular),
        rng=rng,
    )

    rows: list[PanelRow] = []
    rows.extend(_panel_rows_from_feeds(state, discovery, feed_group="discovery_surfaced"))
    rows.extend(_panel_rows_from_popular(state, popular))
    rows.extend(_panel_rows_from_feeds(state, less_known, feed_group="less_known"))

    state.conn.execute("DELETE FROM feed_panel")
    state.conn.executemany(
        """
        INSERT INTO feed_panel(
          feed_uri, feed_group, selection_reason, provider_bucket, service_did, creator_did, display_name,
          inclusion_count, popularity_rank
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                r.feed_uri,
                r.feed_group,
                r.selection_reason,
                r.provider_bucket,
                r.service_did,
                r.creator_did,
                r.display_name,
                r.inclusion_count,
                r.popularity_rank,
            )
            for r in rows
        ],
    )
    state.conn.commit()

    logger.info(
        "stage=done name=build_feed_panel total=%s discovery=%s popular=%s less_known=%s seed=%s",
        _count_rows(state, "feed_panel"),
        len(discovery),
        len(popular),
        len(less_known),
        seed,
    )

    # Store baselines used by validation so resumable runs don't become stricter after the panel
    # is fixed and snapshots are already in progress.
    eligible_provider_buckets = int(
        state.conn.execute(
            """
            SELECT COUNT(DISTINCT provider_bucket) AS n
            FROM feed_generators
            WHERE provider_bucket IS NOT NULL
              AND feed_uri NOT IN (SELECT feed_uri FROM feed_panel WHERE feed_group IN ('discovery_surfaced', 'popular'))
            """
        ).fetchone()["n"]
    )
    less_known_provider_buckets = int(
        state.conn.execute(
            "SELECT COUNT(DISTINCT provider_bucket) AS n FROM feed_panel WHERE feed_group='less_known' AND provider_bucket IS NOT NULL"
        ).fetchone()["n"]
    )
    state.set_meta("panel_built_at_utc", utc_now_iso())
    state.set_meta("panel_eligible_provider_buckets", str(eligible_provider_buckets))
    state.set_meta("panel_less_known_provider_buckets", str(less_known_provider_buckets))


def repair_feed_panel_after_snapshot_failures(cfg: AppConfig, state: StateDB) -> int:
    """
    Replace feeds that appear permanently unreachable in snapshots.

    Heuristic: replace feeds in the current panel that failed with a 4xx (except 429) for any
    required viewer mode. Replacements are drawn from the same group candidate pools.
    """

    viewer_modes = _required_viewer_modes(cfg.auth_mode)
    placeholders = ",".join(["?"] * len(viewer_modes))

    failed_rows = list(
        state.conn.execute(
            f"""
            SELECT feed_uri, feed_group, viewer_mode, http_status
            FROM feed_snapshot_status
            WHERE feed_uri IN (SELECT feed_uri FROM feed_panel)
              AND viewer_mode IN ({placeholders})
              AND success = 0
              AND http_status IS NOT NULL
              AND http_status >= 400 AND http_status < 500
              AND http_status != 429
            """,
            tuple(viewer_modes),
        )
    )
    if not failed_rows:
        return 0

    to_replace: dict[str, set[str]] = {}
    for r in failed_rows:
        uri = str(r["feed_uri"])
        group = str(r["feed_group"])
        to_replace.setdefault(group, set()).add(uri)

    all_panel_uris = {str(r["feed_uri"]) for r in state.conn.execute("SELECT feed_uri FROM feed_panel")}
    permanently_failed_uris = {
        str(r["feed_uri"])
        for r in state.conn.execute(
            f"""
            SELECT DISTINCT feed_uri
            FROM feed_snapshot_status
            WHERE viewer_mode IN ({placeholders})
              AND success = 0
              AND http_status IS NOT NULL
              AND http_status >= 400 AND http_status < 500
              AND http_status != 429
            """,
            tuple(viewer_modes),
        )
    }
    exclude = all_panel_uris | permanently_failed_uris

    run_id = state.get_meta("run_id") or ""
    seed = int(run_id[:8], 16) if run_id else 0
    rng = random.Random(seed + 1)

    total_replaced = 0
    for group, uris in sorted(to_replace.items()):
        need = len(uris)
        if need <= 0:
            continue

        # Remove the failed feeds from the panel (keep snapshot_status for auditing).
        state.conn.executemany("DELETE FROM feed_panel WHERE feed_uri = ?", [(u,) for u in sorted(uris)])

        replacements: list[str]
        if group == "discovery_surfaced":
            replacements = _pick_ranked_discovery(state, n=need, exclude=exclude)
        elif group == "popular":
            replacements = _pick_ranked_popular(state, n=need, exclude=exclude)
        elif group == "less_known":
            replacements = _select_less_known(state, n=need, exclude=exclude, rng=rng)
        else:
            # Unknown group: do not attempt to replace; re-add the original feeds.
            state.conn.executemany(
                """
                INSERT OR IGNORE INTO feed_panel(feed_uri, feed_group, selection_reason, provider_bucket, service_did, creator_did, display_name, inclusion_count, popularity_rank)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        row.feed_uri,
                        row.feed_group,
                        row.selection_reason,
                        row.provider_bucket,
                        row.service_did,
                        row.creator_did,
                        row.display_name,
                        row.inclusion_count,
                        row.popularity_rank,
                    )
                    for row in _panel_rows_from_feeds(state, sorted(uris), feed_group=group)
                ],
            )
            record_error(
                state,
                stage="feed_panel.repair",
                key=group,
                error_type="unknown_feed_group",
                error_message=f"skipped replacements for unknown feed_group={group} (restored originals)",
            )
            continue

        exclude |= set(replacements)

        panel_rows: list[PanelRow] = []
        for uri in replacements:
            panel_rows.append(_panel_row_for_uri(state, uri, feed_group=group, selection_reason="replacement_after_snapshot_failure"))

        state.conn.executemany(
            """
            INSERT OR IGNORE INTO feed_panel(
              feed_uri, feed_group, selection_reason, provider_bucket, service_did, creator_did, display_name,
              inclusion_count, popularity_rank
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    r.feed_uri,
                    r.feed_group,
                    r.selection_reason,
                    r.provider_bucket,
                    r.service_did,
                    r.creator_did,
                    r.display_name,
                    r.inclusion_count,
                    r.popularity_rank,
                )
                for r in panel_rows
            ],
        )

        if len(replacements) < need:
            record_error(
                state,
                stage="feed_panel.repair",
                key=group,
                error_type="insufficient_replacements",
                error_message=f"needed={need} got={len(replacements)}",
            )
            logger.warning("feed_panel repair group=%s needed=%s got=%s", group, need, len(replacements))

        record_error(
            state,
            stage="feed_panel.repair",
            key=group,
            error_type="replaced_after_snapshot_failure",
            error_message=f"removed={need} added={len(panel_rows)} viewer_modes={','.join(viewer_modes)}",
        )

        total_replaced += len(panel_rows)

    state.conn.commit()
    logger.warning("feed_panel repaired replacements=%s", total_replaced)
    return total_replaced


def _required_viewer_modes(auth_mode: AuthMode) -> tuple[str, ...]:
    if auth_mode == AuthMode.UNAUTH:
        return ("unauth",)
    if auth_mode == AuthMode.AUTH:
        return ("auth",)
    if auth_mode == AuthMode.BOTH:
        return ("unauth", "auth")
    raise ValueError(f"unhandled auth_mode: {auth_mode}")


def _pick_ranked_discovery(state: StateDB, *, n: int, exclude: set[str]) -> list[str]:
    rows = list(
        state.conn.execute(
            """
            SELECT feed_uri, COUNT(*) AS slot_count
            FROM starterpack_feeds
            GROUP BY feed_uri
            """
        )
    )
    items = [(str(r["feed_uri"]), int(r["slot_count"])) for r in rows]
    items.sort(key=lambda t: (-t[1], t[0]))
    ranked = [uri for uri, _ in items]
    return _pick_from_ranked(ranked, n=n, exclude=exclude)


def _pick_ranked_popular(state: StateDB, *, n: int, exclude: set[str]) -> list[str]:
    ranked = [str(r["feed_uri"]) for r in state.conn.execute("SELECT feed_uri FROM popular_feeds ORDER BY popularity_rank")]
    return _pick_from_ranked(ranked, n=n, exclude=exclude)


def _pick_from_ranked(ranked: list[str], *, n: int, exclude: set[str]) -> list[str]:
    out: list[str] = []
    for uri in ranked:
        if uri in exclude:
            continue
        out.append(uri)
        if len(out) >= n:
            break
    return out


def _panel_row_for_uri(state: StateDB, uri: str, *, feed_group: str, selection_reason: str) -> PanelRow:
    try:
        creator_from_uri = parse_at_uri(uri).did
    except ValueError:
        creator_from_uri = None

    fg = state.conn.execute(
        """
        SELECT creator_did, service_did, provider_bucket, display_name
        FROM feed_generators
        WHERE feed_uri = ?
        """,
        (uri,),
    ).fetchone()

    inclusion_count = None
    popularity_rank = None
    if feed_group == "discovery_surfaced":
        ic = state.conn.execute(
            "SELECT COUNT(DISTINCT starterpack_uri) AS n FROM starterpack_feeds WHERE feed_uri = ?",
            (uri,),
        ).fetchone()
        inclusion_count = int(ic["n"]) if ic is not None else None
    if feed_group == "popular":
        pr = state.conn.execute(
            "SELECT popularity_rank FROM popular_feeds WHERE feed_uri = ?",
            (uri,),
        ).fetchone()
        popularity_rank = int(pr["popularity_rank"]) if pr is not None else None

    return PanelRow(
        feed_uri=uri,
        feed_group=feed_group,
        selection_reason=selection_reason,
        provider_bucket=str(fg["provider_bucket"]) if fg is not None and fg["provider_bucket"] is not None else "unknown",
        service_did=str(fg["service_did"]) if fg is not None and fg["service_did"] is not None else None,
        creator_did=str(fg["creator_did"]) if fg is not None and fg["creator_did"] is not None else creator_from_uri,
        display_name=str(fg["display_name"]) if fg is not None and fg["display_name"] is not None else None,
        inclusion_count=inclusion_count,
        popularity_rank=popularity_rank,
    )


def _count_rows(state: StateDB, table: str) -> int:
    row = state.conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()
    return int(row["n"]) if row is not None else 0


def _select_discovery(state: StateDB, *, n: int) -> list[str]:
    # Select top feeds by slot_count (total appearances across starter packs).
    rows = list(
        state.conn.execute(
            """
            SELECT feed_uri, COUNT(*) AS slot_count
            FROM starterpack_feeds
            GROUP BY feed_uri
            """
        )
    )
    items = [(str(r["feed_uri"]), int(r["slot_count"])) for r in rows]
    items.sort(key=lambda t: (-t[1], t[0]))
    return [uri for uri, _ in items[:n]]


def _select_popular(state: StateDB, *, n: int, exclude: set[str]) -> list[str]:
    out: list[str] = []
    for row in state.conn.execute(
        "SELECT feed_uri FROM popular_feeds ORDER BY popularity_rank",
    ):
        uri = str(row["feed_uri"])
        if uri in exclude:
            continue
        out.append(uri)
        if len(out) >= n:
            break
    return out


def _select_less_known(state: StateDB, *, n: int, exclude: set[str], rng: random.Random) -> list[str]:
    provider_to_feeds: dict[str, list[str]] = {}
    for row in state.conn.execute(
        """
        SELECT feed_uri, provider_bucket
        FROM feed_generators
        ORDER BY feed_uri
        """
    ):
        uri = str(row["feed_uri"])
        if uri in exclude:
            continue
        bucket = str(row["provider_bucket"])
        provider_to_feeds.setdefault(bucket, []).append(uri)

    providers = list(provider_to_feeds.keys())
    rng.shuffle(providers)
    for bucket in providers:
        rng.shuffle(provider_to_feeds[bucket])

    selected: list[str] = []
    round_idx = 0
    while len(selected) < n:
        picked_this_round = 0
        for bucket in providers:
            feeds = provider_to_feeds[bucket]
            if round_idx >= len(feeds):
                continue
            selected.append(feeds[round_idx])
            picked_this_round += 1
            if len(selected) >= n:
                break
        if picked_this_round == 0:
            break
        round_idx += 1

    return selected


def _panel_rows_from_feeds(state: StateDB, feed_uris: list[str], *, feed_group: str) -> list[PanelRow]:
    rows: list[PanelRow] = []
    for uri in feed_uris:
        try:
            creator_from_uri = parse_at_uri(uri).did
        except ValueError:
            creator_from_uri = None

        fg = state.conn.execute(
            """
            SELECT creator_did, service_did, provider_bucket, display_name
            FROM feed_generators
            WHERE feed_uri = ?
            """,
            (uri,),
        ).fetchone()

        inclusion_count = None
        if feed_group == "discovery_surfaced":
            ic = state.conn.execute(
                "SELECT COUNT(DISTINCT starterpack_uri) AS n FROM starterpack_feeds WHERE feed_uri = ?",
                (uri,),
            ).fetchone()
            inclusion_count = int(ic["n"]) if ic is not None else None

        selection_reason = (
            "starterpack_top_by_slot_count" if feed_group == "discovery_surfaced" else "provider_balanced_long_tail"
        )

        rows.append(
            PanelRow(
                feed_uri=uri,
                feed_group=feed_group,
                selection_reason=selection_reason,
                provider_bucket=str(fg["provider_bucket"]) if fg is not None and fg["provider_bucket"] is not None else "unknown",
                service_did=str(fg["service_did"]) if fg is not None and fg["service_did"] is not None else None,
                creator_did=str(fg["creator_did"]) if fg is not None and fg["creator_did"] is not None else creator_from_uri,
                display_name=str(fg["display_name"]) if fg is not None and fg["display_name"] is not None else None,
                inclusion_count=inclusion_count,
                popularity_rank=None,
            )
        )
    return rows


def _panel_rows_from_popular(state: StateDB, feed_uris: list[str]) -> list[PanelRow]:
    rows: list[PanelRow] = []
    for uri in feed_uris:
        try:
            creator_from_uri = parse_at_uri(uri).did
        except ValueError:
            creator_from_uri = None

        fg = state.conn.execute(
            """
            SELECT creator_did, service_did, provider_bucket, display_name
            FROM feed_generators
            WHERE feed_uri = ?
            """,
            (uri,),
        ).fetchone()
        pr = state.conn.execute(
            "SELECT popularity_rank FROM popular_feeds WHERE feed_uri = ?",
            (uri,),
        ).fetchone()
        popularity_rank = int(pr["popularity_rank"]) if pr is not None else None

        rows.append(
            PanelRow(
                feed_uri=uri,
                feed_group="popular",
                selection_reason="top_popular_feed_generators",
                provider_bucket=str(fg["provider_bucket"]) if fg is not None and fg["provider_bucket"] is not None else "unknown",
                service_did=str(fg["service_did"]) if fg is not None and fg["service_did"] is not None else None,
                creator_did=str(fg["creator_did"]) if fg is not None and fg["creator_did"] is not None else creator_from_uri,
                display_name=str(fg["display_name"]) if fg is not None and fg["display_name"] is not None else None,
                inclusion_count=None,
                popularity_rank=popularity_rank,
            )
        )
    return rows
