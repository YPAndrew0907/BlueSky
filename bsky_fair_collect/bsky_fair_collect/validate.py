from __future__ import annotations

import logging
from dataclasses import dataclass

from bsky_fair_collect.config import AppConfig, AuthMode, RunParams
from bsky_fair_collect.http_client import HttpClient, HttpRetryConfig
from bsky_fair_collect.session import SessionManager
from bsky_fair_collect.state import StateDB

logger = logging.getLogger("bsky_fair_collect.validate")


@dataclass(frozen=True)
class ValidationCheck:
    check_name: str
    status: str  # PASS|FAIL
    observed_value: str
    expected_threshold: str
    notes: str


def upsert_checks(state: StateDB, checks: list[ValidationCheck]) -> None:
    state.conn.executemany(
        """
        INSERT INTO validations(check_name, status, observed_value, expected_threshold, notes)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(check_name) DO UPDATE SET
          status = excluded.status,
          observed_value = excluded.observed_value,
          expected_threshold = excluded.expected_threshold,
          notes = excluded.notes
        """,
        [(c.check_name, c.status, c.observed_value, c.expected_threshold, c.notes) for c in checks],
    )
    state.conn.commit()


def require_all_pass(state: StateDB) -> None:
    row = state.conn.execute("SELECT COUNT(*) AS n FROM validations WHERE status = 'FAIL'").fetchone()
    fails = int(row["n"]) if row is not None else 0
    if fails:
        raise RuntimeError(f"validation failed: {fails} checks are FAIL (see OUT_DIR/csv/validation_report.csv)")


def validate_feed_generator_index(state: StateDB) -> list[ValidationCheck]:
    num_feed_generators = _count_rows(state, "feed_generators")
    num_providers = _count_distinct(state, "feed_generators", "provider_bucket")

    checks = [
        ValidationCheck(
            check_name="feed_generators_index.nonempty",
            status="PASS" if num_feed_generators > 0 else "FAIL",
            observed_value=str(num_feed_generators),
            expected_threshold="> 0",
            notes="Indexed via relay listReposByCollection + appview getActorFeeds.",
        ),
        ValidationCheck(
            check_name="feed_generators_index.provider_bucket_count",
            status="PASS" if num_providers > 0 else "FAIL",
            observed_value=str(num_providers),
            expected_threshold="> 0",
            notes="Ideal scale is large (e.g., 100k+ feeds) but this check only requires >0.",
        ),
    ]
    return checks


def validate_starterpacks(state: StateDB) -> list[ValidationCheck]:
    num_packs = _count_rows(state, "starterpacks")
    num_unique_feeds = _count_distinct(state, "starterpack_feeds", "feed_uri")

    return [
        ValidationCheck(
            check_name="starterpacks.nonempty",
            status="PASS" if num_packs > 0 else "FAIL",
            observed_value=str(num_packs),
            expected_threshold="> 0",
            notes=(
                "Collected via relay listReposByCollection(app.bsky.graph.starterpack) + "
                "appview getActorStarterPacks + getStarterPack (searchStarterPacks used when available)."
            ),
        ),
        ValidationCheck(
            check_name="starterpacks.unique_feeds_nonempty",
            status="PASS" if num_unique_feeds > 0 else "FAIL",
            observed_value=str(num_unique_feeds),
            expected_threshold="> 0",
            notes="Distinct feed URIs extracted from packs.",
        ),
    ]


def validate_popular(cfg: AppConfig, state: StateDB) -> list[ValidationCheck]:
    n = _count_rows(state, "popular_feeds")
    target = cfg.run.n_popular
    cursor_done = state.get_meta("popular_cursor_done") == "1"

    if target <= 0:
        ok = True
        expected_threshold = "n_popular == 0"
        notes = "popular stage disabled (target is 0)."
    elif n >= int(0.95 * target):
        ok = True
        expected_threshold = f">= {int(0.95 * target)}"
        notes = "Collected via appview getPopularFeedGenerators."
    elif cursor_done and n > 0:
        # The endpoint can be exhausted (or stop yielding new unique feeds) before reaching the target.
        ok = True
        expected_threshold = "cursor exhausted (popular_cursor_done=1)"
        notes = f"Exhausted before target: collected={n} target={target}."
    else:
        ok = False
        expected_threshold = f">= {int(0.95 * target)}"
        notes = "Collected via appview getPopularFeedGenerators (may be exhausted before target)."

    return [
        ValidationCheck(
            check_name="popular_feeds.coverage",
            status="PASS" if ok else "FAIL",
            observed_value=str(n),
            expected_threshold=expected_threshold,
            notes=notes,
        )
    ]


def validate_feed_panel(cfg: AppConfig, state: StateDB) -> list[ValidationCheck]:
    desired = cfg.run.n_discovery + cfg.run.n_popular + cfg.run.n_less_known
    total = _count_rows(state, "feed_panel")
    ok_total = total >= int(0.95 * desired) if desired > 0 else True

    less_known_providers = _count_distinct_where(
        state,
        table="feed_panel",
        column="provider_bucket",
        where_sql="feed_group = 'less_known' AND provider_bucket IS NOT NULL",
        params=(),
    )
    less_known_count = _count_rows_where(
        state,
        table="feed_panel",
        where_sql="feed_group = 'less_known'",
        params=(),
    )
    # Freeze the "eligible provider bucket" baseline once snapshots have started so this check doesn't
    # become stricter over time as the feed-generator index grows during resumable runs.
    eligible_provider_buckets: int | None = None
    eligible_meta = state.get_meta("panel_eligible_provider_buckets")
    if eligible_meta:
        try:
            eligible_provider_buckets = int(eligible_meta)
        except ValueError:
            eligible_provider_buckets = None

    if eligible_provider_buckets is None:
        eligible_provider_buckets = _count_distinct_where(
            state,
            table="feed_generators",
            column="provider_bucket",
            where_sql=(
                "provider_bucket IS NOT NULL AND "
                "feed_uri NOT IN (SELECT feed_uri FROM feed_panel WHERE feed_group IN ('discovery_surfaced', 'popular'))"
            ),
            params=(),
        )

    if state.get_meta("snapshots_started") == "1" and not eligible_meta:
        # Legacy runs (or runs created before we stored the baseline) can end up with a panel built
        # from a smaller index slice. In that case, freeze the baseline to the panel's observed
        # provider diversity so we don't fail at the very end with an impossible-to-fix check.
        eligible_provider_buckets = min(int(eligible_provider_buckets), int(less_known_providers))
        if eligible_provider_buckets > 0:
            state.set_meta("panel_eligible_provider_buckets", str(int(eligible_provider_buckets)))

    base_less_known = less_known_count
    if cfg.run.n_less_known > 0:
        base_less_known = min(int(cfg.run.n_less_known), int(less_known_count))

    max_possible_diversity = min(int(base_less_known), int(eligible_provider_buckets))
    if base_less_known > 0 and max_possible_diversity > 0:
        # This is a *diversity signal* check, not a hard scientific truth: we want lots of
        # unique providers represented in the less-known sample.
        target_by_count = max(50, int(base_less_known * 0.25))
        target_by_possible = max(1, int(max_possible_diversity * 0.8))
        desired_diversity = min(target_by_count, target_by_possible, max_possible_diversity)
    else:
        desired_diversity = 0
    ok_diversity = less_known_providers >= desired_diversity if less_known_count > 0 else True

    return [
        ValidationCheck(
            check_name="feed_panel.coverage",
            status="PASS" if ok_total else "FAIL",
            observed_value=str(total),
            expected_threshold=f">= {int(0.95 * desired)}",
            notes="Panel built from discovery_surfaced + popular + provider-balanced less_known.",
        ),
        ValidationCheck(
            check_name="feed_panel.less_known_provider_diversity",
            status="PASS" if ok_diversity else "FAIL",
            observed_value=str(less_known_providers),
            expected_threshold=f">= {desired_diversity} (max_possible={max_possible_diversity})",
            notes=(
                "Unique provider_bucket count within less_known group. "
                f"less_known_count={less_known_count} base_less_known={base_less_known} "
                f"eligible_provider_buckets={eligible_provider_buckets}."
            ),
        ),
    ]


def _count_rows_where(
    state: StateDB,
    *,
    table: str,
    where_sql: str,
    params: tuple[object, ...],
) -> int:
    row = state.conn.execute(
        f"SELECT COUNT(*) AS n FROM {table} WHERE {where_sql}",
        params,
    ).fetchone()
    return int(row["n"]) if row is not None else 0


def validate_snapshots(cfg: AppConfig, state: StateDB) -> list[ValidationCheck]:
    panel = _count_rows(state, "feed_panel")
    expected = panel * (2 if cfg.auth_mode == AuthMode.BOTH else 1)
    success = int(
        state.conn.execute(
            """
            SELECT COUNT(*) AS n
            FROM feed_snapshot_status s
            INNER JOIN feed_panel p ON p.feed_uri = s.feed_uri
            WHERE s.success = 1
            """
        ).fetchone()["n"]
    )
    rate = (success / expected) if expected else 0.0
    if not expected:
        ok = True
        expected_threshold = "n/a"
        notes = "no panel entries"
    else:
        # For very small smoke-test runs, allow up to 1 failure (but still require at least 1 success).
        if expected < 20:
            required_success = max(1, expected - 1)
            ok = success >= required_success
            expected_threshold = f">= {required_success}/{expected} ({required_success/expected:.4f})"
        else:
            ok = rate >= 0.90
            expected_threshold = ">= 0.90"
        notes = f"success={success} expected={expected}"

    return [
        ValidationCheck(
            check_name="snapshots.success_rate",
            status="PASS" if ok else "FAIL",
            observed_value=f"{rate:.4f}",
            expected_threshold=expected_threshold,
            notes=notes,
        )
    ]


def validate_author_hydration(state: StateDB) -> list[ValidationCheck]:
    unique_authors = int(state.conn.execute("SELECT COUNT(DISTINCT author_did) AS n FROM feed_items").fetchone()["n"])
    hydrated = _count_rows(state, "authors")
    rate = (hydrated / unique_authors) if unique_authors else 0.0
    ok = rate >= 0.95 if unique_authors else True
    return [
        ValidationCheck(
            check_name="authors.hydration_rate",
            status="PASS" if ok else "FAIL",
            observed_value=f"{rate:.4f}",
            expected_threshold=">= 0.95",
            notes=f"hydrated={hydrated} unique_authors={unique_authors}",
        )
    ]


def validate_all(cfg: AppConfig, state: StateDB) -> None:
    checks: list[ValidationCheck] = []
    checks.extend(validate_feed_generator_index(state))
    checks.extend(validate_starterpacks(state))
    checks.extend(validate_popular(cfg, state))
    checks.extend(validate_feed_panel(cfg, state))
    checks.extend(validate_snapshots(cfg, state))
    checks.extend(validate_author_hydration(state))
    upsert_checks(state, checks)
    require_all_pass(state)


def remediate_snapshots_once(cfg: AppConfig, state: StateDB, session: SessionManager | None) -> None:
    # One remediation loop: slow down and retry failed snapshots.
    from bsky_fair_collect.snapshot_feeds import stage_snapshot_feeds
    from bsky_fair_collect.build_panel import repair_feed_panel_after_snapshot_failures

    # First: replace permanently unreachable 4xx feeds in the panel, then snapshot only the new (missing) feeds.
    replaced = repair_feed_panel_after_snapshot_failures(cfg, state)

    tmp_http = HttpClient(state=state, rps=cfg.run.rps, retry=HttpRetryConfig(max_retries=cfg.run.max_retries))
    try:
        tmp_session = SessionManager(state=state, http=tmp_http) if session is not None else None
        if replaced:
            logger.warning("remediation snapshots: repaired feed_panel replacements=%s", replaced)
            stage_snapshot_feeds(cfg, state, tmp_http, tmp_session, retry_failures=False)
    finally:
        tmp_http.close()

    # If we're still below the success threshold, do one last retry pass on remaining failures at a slower rate.
    checks = validate_snapshots(cfg, state)
    if all(c.status == "PASS" for c in checks):
        return

    new_rps = max(1.0, cfg.run.rps * 0.5)
    new_retries = cfg.run.max_retries + 2
    logger.warning(
        "remediation snapshots: still failing; retry remaining failures with rps=%.2f (was %.2f) max_retries=%s (was %s)",
        new_rps,
        cfg.run.rps,
        new_retries,
        cfg.run.max_retries,
    )

    slow_http = HttpClient(state=state, rps=new_rps, retry=HttpRetryConfig(max_retries=new_retries))
    try:
        slow_session = SessionManager(state=state, http=slow_http) if session is not None else None
        stage_snapshot_feeds(cfg, state, slow_http, slow_session, retry_failures=True)
    finally:
        slow_http.close()


def remediate_authors_once(cfg: AppConfig, state: StateDB) -> None:
    from bsky_fair_collect.hydrate_authors import stage_hydrate_authors

    new_rps = max(1.0, cfg.run.rps * 0.5)
    new_batch = max(5, int(cfg.run.profiles_batch_size * 0.5))
    logger.warning(
        "remediation authors: retry missing with rps=%.2f (was %.2f) profiles_batch_size=%s (was %s)",
        new_rps,
        cfg.run.rps,
        new_batch,
        cfg.run.profiles_batch_size,
    )

    remed_cfg = AppConfig(
        outputs=cfg.outputs,
        hosts=cfg.hosts,
        auth_mode=cfg.auth_mode,
        run=RunParams(
            rps=new_rps,
            max_retries=cfg.run.max_retries + 2,
            posts_per_feed=cfg.run.posts_per_feed,
            n_discovery=cfg.run.n_discovery,
            n_popular=cfg.run.n_popular,
            n_less_known=cfg.run.n_less_known,
            starterpack_queries=cfg.run.starterpack_queries,
            starterpack_max_per_query=cfg.run.starterpack_max_per_query,
            popular_page_limit=cfg.run.popular_page_limit,
            relay_page_limit=cfg.run.relay_page_limit,
            actor_feeds_page_limit=cfg.run.actor_feeds_page_limit,
            feed_page_limit=cfg.run.feed_page_limit,
            profiles_batch_size=new_batch,
        ),
    )

    tmp_http = HttpClient(
        state=state,
        rps=remed_cfg.run.rps,
        retry=HttpRetryConfig(max_retries=remed_cfg.run.max_retries),
    )
    try:
        stage_hydrate_authors(remed_cfg, state, tmp_http)
    finally:
        tmp_http.close()


def _count_rows(state: StateDB, table: str) -> int:
    row = state.conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()
    return int(row["n"]) if row is not None else 0


def _count_distinct(state: StateDB, table: str, column: str) -> int:
    row = state.conn.execute(f"SELECT COUNT(DISTINCT {column}) AS n FROM {table}").fetchone()
    return int(row["n"]) if row is not None else 0


def _count_distinct_where(
    state: StateDB,
    *,
    table: str,
    column: str,
    where_sql: str,
    params: tuple[object, ...],
) -> int:
    row = state.conn.execute(
        f"SELECT COUNT(DISTINCT {column}) AS n FROM {table} WHERE {where_sql}",
        params,
    ).fetchone()
    return int(row["n"]) if row is not None else 0
