from __future__ import annotations

from pathlib import Path


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "collector_health_monitor_windows.ps1"


def test_health_monitor_tracks_mainline_public_and_backfill_jobs() -> None:
    text = SCRIPT_PATH.read_text(encoding="utf-8")

    for token in (
        'micro_snapshot_study = "bsky_collector_v2 micro-snapshot-study"',
        'snapshot_panel = "bsky_collector_v2 snapshot-panel"',
        'wide_sweep = "bsky_collector_v2 wide-sweep"',
        'hydrate_authors = "bsky_collector_v2 hydrate-authors"',
        'hydrate_feed_generators = "bsky_collector_v2 hydrate-feed-generators"',
        'index_feed_generators = "bsky_collector_v2 index-feed-generators"',
        'refresh_discovery = "bsky_collector_v2 refresh-discovery"',
        'build_panel = "bsky_collector_v2 build-panel"',
        'backfill_interactions = "bsky_collector_v2 backfill-interactions"',
        'backfill_rq1_factors = "bsky_collector_v2 backfill-rq1-factors"',
        'collect_public_omnibus = "bsky_collector_v2 collect-public-omnibus"',
    ):
        assert token in text


def test_health_monitor_snapshot_exposes_job_breakdown_and_recent_starts() -> None:
    text = SCRIPT_PATH.read_text(encoding="utf-8")

    assert "function Read-EpochFileUtc" in text
    assert "function Get-ActivePythonProcesses" in text
    assert "function Get-ActiveJobBreakdown" in text
    assert "active_job_breakdown = $activeJobBreakdown" in text
    assert "micro_snapshot_last_start_utc = $microSnapshotLastStartUtc" in text
    assert "public_omnivore_last_start_utc = $publicOmnivoreLastStartUtc" in text
    assert 'jobs=[{5}]' in text
