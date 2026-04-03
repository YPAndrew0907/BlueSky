from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from bsky_collector_v2.time_utils import SnapshotHour


@dataclass(frozen=True)
class Layout:
    out_base: Path

    @property
    def studies_root(self) -> Path:
        return self.out_base / "studies"

    def study_dir(self, study_id: str) -> Path:
        return self.studies_root / str(study_id)

    def study_manifest_json(self, study_id: str) -> Path:
        return self.study_dir(study_id) / "study_manifest.json"

    def study_panel_dir(self, study_id: str) -> Path:
        return self.study_dir(study_id) / "panel"

    def study_panel_csv(self, study_id: str) -> Path:
        return self.study_panel_dir(study_id) / "frozen_panel.csv"

    def study_benchmark_json(self, study_id: str) -> Path:
        return self.study_dir(study_id) / "benchmark_result.json"

    @property
    def study_benchmarks_root(self) -> Path:
        return self.studies_root / "benchmarks"

    def benchmark_result_json(self, benchmark_id: str) -> Path:
        return self.study_benchmarks_root / f"{benchmark_id}.json"

    @property
    def effective_csv_root(self) -> Path:
        return self.out_base / "effective_csv"

    @property
    def effective_timeseries_root(self) -> Path:
        return self.effective_csv_root / "timeseries"

    @property
    def effective_micro5_root(self) -> Path:
        return self.effective_timeseries_root / "micro5"

    @property
    def effective_key_root(self) -> Path:
        return self.effective_csv_root / "key"

    @property
    def effective_key_sources_json(self) -> Path:
        return self.effective_key_root / "sources.json"

    @property
    def metadata_root(self) -> Path:
        return self.out_base / "metadata"

    def metadata_day(self, date_yyyy_mm_dd: str) -> Path:
        return self.metadata_root / date_yyyy_mm_dd

    def discovery_sources_dir(self, date_yyyy_mm_dd: str) -> Path:
        return self.metadata_day(date_yyyy_mm_dd) / "discovery_sources"

    def feed_generators_index_day_dir(self, date_yyyy_mm_dd: str) -> Path:
        return self.metadata_day(date_yyyy_mm_dd) / "feed_generators_index"

    def feed_generators_index_parts_dir(self, date_yyyy_mm_dd: str) -> Path:
        return self.feed_generators_index_day_dir(date_yyyy_mm_dd) / "parts"

    def feed_generators_index_logs_dir(self, date_yyyy_mm_dd: str) -> Path:
        return self.feed_generators_index_day_dir(date_yyyy_mm_dd) / "logs"

    def feed_generators_index_log(self, date_yyyy_mm_dd: str) -> Path:
        return self.feed_generators_index_logs_dir(date_yyyy_mm_dd) / "index.log"

    def feed_generators_index_manifest_json(self, date_yyyy_mm_dd: str) -> Path:
        return self.feed_generators_index_day_dir(date_yyyy_mm_dd) / "run_manifest.json"

    def feed_generators_index_progress_json(self, date_yyyy_mm_dd: str) -> Path:
        return self.feed_generators_index_day_dir(date_yyyy_mm_dd) / "progress.json"

    def feed_generators_index_http_stats_csv(self, date_yyyy_mm_dd: str) -> Path:
        return self.feed_generators_index_day_dir(date_yyyy_mm_dd) / "http_stats.csv"

    def feed_generators_index_request_provenance_csv(self, date_yyyy_mm_dd: str) -> Path:
        return self.feed_generators_index_day_dir(date_yyyy_mm_dd) / "request_provenance.csv"

    def feed_generators_index_quality_report_json(self, date_yyyy_mm_dd: str) -> Path:
        return self.feed_generators_index_day_dir(date_yyyy_mm_dd) / "quality_report.json"

    def feed_generators_index_auth_preference_snapshot_json(self, date_yyyy_mm_dd: str) -> Path:
        return self.feed_generators_index_day_dir(date_yyyy_mm_dd) / "auth_preference_snapshot.json"

    def feed_catalog_csv(self, date_yyyy_mm_dd: str) -> Path:
        return self.metadata_day(date_yyyy_mm_dd) / "feed_catalog.csv"

    def metadata_manifest_json(self, date_yyyy_mm_dd: str) -> Path:
        return self.metadata_day(date_yyyy_mm_dd) / "run_manifest.json"

    def metadata_discovery_status_json(self, date_yyyy_mm_dd: str) -> Path:
        return self.metadata_day(date_yyyy_mm_dd) / "discovery_status.json"

    def metadata_request_provenance_csv(self, date_yyyy_mm_dd: str) -> Path:
        return self.metadata_day(date_yyyy_mm_dd) / "request_provenance.csv"

    def metadata_quality_report_json(self, date_yyyy_mm_dd: str) -> Path:
        return self.metadata_day(date_yyyy_mm_dd) / "quality_report.json"

    def metadata_auth_preference_snapshot_json(self, date_yyyy_mm_dd: str) -> Path:
        return self.metadata_day(date_yyyy_mm_dd) / "auth_preference_snapshot.json"

    def starterpack_feeds_csv(self, date_yyyy_mm_dd: str) -> Path:
        return self.metadata_day(date_yyyy_mm_dd) / "starterpack_feeds.csv"

    def starterpack_accounts_csv(self, date_yyyy_mm_dd: str) -> Path:
        return self.metadata_day(date_yyyy_mm_dd) / "starterpack_accounts.csv"

    def suggested_feeds_csv(self, date_yyyy_mm_dd: str) -> Path:
        return self.metadata_day(date_yyyy_mm_dd) / "suggested_feeds.csv"

    def suggested_accounts_csv(self, date_yyyy_mm_dd: str) -> Path:
        return self.metadata_day(date_yyyy_mm_dd) / "suggested_accounts.csv"

    def suggested_follows_by_actor_csv(self, date_yyyy_mm_dd: str) -> Path:
        return self.metadata_day(date_yyyy_mm_dd) / "suggested_follows_by_actor.csv"

    @property
    def panel_root(self) -> Path:
        return self.out_base / "panel"

    @property
    def panel_active_csv(self) -> Path:
        return self.panel_root / "panel_v1.csv"

    @property
    def panel_versions_dir(self) -> Path:
        return self.panel_root / "panel_versions"

    def panel_version_csv(self, date_yyyy_mm_dd: str) -> Path:
        return self.panel_versions_dir / f"panel_v1_{date_yyyy_mm_dd}.csv"

    @property
    def micro5_root(self) -> Path:
        return self.out_base / "micro5"

    def micro5_study_root(self, study_id: str) -> Path:
        return self.micro5_root / str(study_id)

    def micro5_family_root(self, *, study_id: str, sample_family: str) -> Path:
        return self.micro5_study_root(study_id) / str(sample_family)

    def effective_micro5_study_root(self, *, study_id: str, sample_family: str) -> Path:
        return self.effective_micro5_root / str(study_id) / str(sample_family)

    def _micro5_components(
        self,
        *,
        window=None,  # noqa: ANN001
        date_yyyy_mm_dd: str | None = None,
        hour_str: str | None = None,
        minute_str: str | None = None,
    ) -> tuple[str, str, str]:
        if window is not None:
            if hasattr(window, "date_str") and hasattr(window, "hour_str") and hasattr(window, "minute_str"):
                return str(window.date_str), str(window.hour_str), str(window.minute_str)
            start_utc = getattr(window, "scheduled_window_start_utc", None) or getattr(window, "start_utc", None)
            if start_utc is not None:
                return (
                    start_utc.date().isoformat(),
                    f"{start_utc.hour:02d}",
                    f"{start_utc.minute:02d}",
                )
        if date_yyyy_mm_dd is None or hour_str is None or minute_str is None:
            raise ValueError("micro5 paths require either window=... or date_yyyy_mm_dd/hour_str/minute_str")
        return str(date_yyyy_mm_dd), str(hour_str), str(minute_str)

    def micro5_window_dir(
        self,
        *,
        study_id: str,
        sample_family: str,
        window=None,  # noqa: ANN001
        date_yyyy_mm_dd: str | None = None,
        hour_str: str | None = None,
        minute_str: str | None = None,
    ) -> Path:
        date_yyyy_mm_dd, hour_str, minute_str = self._micro5_components(
            window=window,
            date_yyyy_mm_dd=date_yyyy_mm_dd,
            hour_str=hour_str,
            minute_str=minute_str,
        )
        return self.micro5_family_root(study_id=study_id, sample_family=sample_family) / date_yyyy_mm_dd / hour_str / minute_str

    def effective_micro5_window_dir(
        self,
        *,
        study_id: str,
        sample_family: str,
        window=None,  # noqa: ANN001
        date_yyyy_mm_dd: str | None = None,
        hour_str: str | None = None,
        minute_str: str | None = None,
    ) -> Path:
        date_yyyy_mm_dd, hour_str, minute_str = self._micro5_components(
            window=window,
            date_yyyy_mm_dd=date_yyyy_mm_dd,
            hour_str=hour_str,
            minute_str=minute_str,
        )
        return self.effective_micro5_study_root(study_id=study_id, sample_family=sample_family) / date_yyyy_mm_dd / hour_str / minute_str

    def micro5_manifest_json(
        self,
        *,
        study_id: str,
        sample_family: str,
        window=None,  # noqa: ANN001
        date_yyyy_mm_dd: str | None = None,
        hour_str: str | None = None,
        minute_str: str | None = None,
    ) -> Path:
        return self.micro5_window_dir(
            study_id=study_id,
            sample_family=sample_family,
            window=window,
            date_yyyy_mm_dd=date_yyyy_mm_dd,
            hour_str=hour_str,
            minute_str=minute_str,
        ) / "run_manifest.json"

    def micro5_progress_json(
        self,
        *,
        study_id: str,
        sample_family: str,
        window=None,  # noqa: ANN001
        date_yyyy_mm_dd: str | None = None,
        hour_str: str | None = None,
        minute_str: str | None = None,
    ) -> Path:
        return self.micro5_window_dir(
            study_id=study_id,
            sample_family=sample_family,
            window=window,
            date_yyyy_mm_dd=date_yyyy_mm_dd,
            hour_str=hour_str,
            minute_str=minute_str,
        ) / "progress.json"

    def micro5_http_stats_csv(
        self,
        *,
        study_id: str,
        sample_family: str,
        window=None,  # noqa: ANN001
        date_yyyy_mm_dd: str | None = None,
        hour_str: str | None = None,
        minute_str: str | None = None,
    ) -> Path:
        return self.micro5_window_dir(
            study_id=study_id,
            sample_family=sample_family,
            window=window,
            date_yyyy_mm_dd=date_yyyy_mm_dd,
            hour_str=hour_str,
            minute_str=minute_str,
        ) / "http_stats.csv"

    def micro5_request_provenance_csv(
        self,
        *,
        study_id: str,
        sample_family: str,
        window=None,  # noqa: ANN001
        date_yyyy_mm_dd: str | None = None,
        hour_str: str | None = None,
        minute_str: str | None = None,
    ) -> Path:
        return self.micro5_window_dir(
            study_id=study_id,
            sample_family=sample_family,
            window=window,
            date_yyyy_mm_dd=date_yyyy_mm_dd,
            hour_str=hour_str,
            minute_str=minute_str,
        ) / "request_provenance.csv"

    def micro5_quality_report_json(
        self,
        *,
        study_id: str,
        sample_family: str,
        window=None,  # noqa: ANN001
        date_yyyy_mm_dd: str | None = None,
        hour_str: str | None = None,
        minute_str: str | None = None,
    ) -> Path:
        return self.micro5_window_dir(
            study_id=study_id,
            sample_family=sample_family,
            window=window,
            date_yyyy_mm_dd=date_yyyy_mm_dd,
            hour_str=hour_str,
            minute_str=minute_str,
        ) / "quality_report.json"

    def micro5_auth_preference_snapshot_json(
        self,
        *,
        study_id: str,
        sample_family: str,
        window=None,  # noqa: ANN001
        date_yyyy_mm_dd: str | None = None,
        hour_str: str | None = None,
        minute_str: str | None = None,
    ) -> Path:
        return self.micro5_window_dir(
            study_id=study_id,
            sample_family=sample_family,
            window=window,
            date_yyyy_mm_dd=date_yyyy_mm_dd,
            hour_str=hour_str,
            minute_str=minute_str,
        ) / "auth_preference_snapshot.json"

    def micro5_status_sqlite(
        self,
        *,
        study_id: str,
        sample_family: str,
        window=None,  # noqa: ANN001
        date_yyyy_mm_dd: str | None = None,
        hour_str: str | None = None,
        minute_str: str | None = None,
    ) -> Path:
        return self.micro5_window_dir(
            study_id=study_id,
            sample_family=sample_family,
            window=window,
            date_yyyy_mm_dd=date_yyyy_mm_dd,
            hour_str=hour_str,
            minute_str=minute_str,
        ) / "snapshot_status.sqlite"

    def micro5_parts_dir(
        self,
        *,
        study_id: str,
        sample_family: str,
        window=None,  # noqa: ANN001
        date_yyyy_mm_dd: str | None = None,
        hour_str: str | None = None,
        minute_str: str | None = None,
    ) -> Path:
        return self.micro5_window_dir(
            study_id=study_id,
            sample_family=sample_family,
            window=window,
            date_yyyy_mm_dd=date_yyyy_mm_dd,
            hour_str=hour_str,
            minute_str=minute_str,
        ) / "parts"

    def micro5_logs_dir(
        self,
        *,
        study_id: str,
        sample_family: str,
        window=None,  # noqa: ANN001
        date_yyyy_mm_dd: str | None = None,
        hour_str: str | None = None,
        minute_str: str | None = None,
    ) -> Path:
        return self.micro5_window_dir(
            study_id=study_id,
            sample_family=sample_family,
            window=window,
            date_yyyy_mm_dd=date_yyyy_mm_dd,
            hour_str=hour_str,
            minute_str=minute_str,
        ) / "logs"

    def micro5_snapshot_log(
        self,
        *,
        study_id: str,
        sample_family: str,
        window=None,  # noqa: ANN001
        date_yyyy_mm_dd: str | None = None,
        hour_str: str | None = None,
        minute_str: str | None = None,
    ) -> Path:
        return self.micro5_logs_dir(
            study_id=study_id,
            sample_family=sample_family,
            window=window,
            date_yyyy_mm_dd=date_yyyy_mm_dd,
            hour_str=hour_str,
            minute_str=minute_str,
        ) / "snapshot.log"

    @property
    def hourly_root(self) -> Path:
        return self.out_base / "hourly"

    def hourly_hour_dir(self, hour: SnapshotHour) -> Path:
        return self.hourly_root / hour.date_str / hour.hour_str

    def hourly_manifest_json(self, hour: SnapshotHour) -> Path:
        return self.hourly_hour_dir(hour) / "run_manifest.json"

    def hourly_progress_json(self, hour: SnapshotHour) -> Path:
        return self.hourly_hour_dir(hour) / "progress.json"

    def hourly_http_stats_csv(self, hour: SnapshotHour) -> Path:
        return self.hourly_hour_dir(hour) / "http_stats.csv"

    def hourly_request_provenance_csv(self, hour: SnapshotHour) -> Path:
        return self.hourly_hour_dir(hour) / "request_provenance.csv"

    def hourly_quality_report_json(self, hour: SnapshotHour) -> Path:
        return self.hourly_hour_dir(hour) / "quality_report.json"

    def hourly_auth_preference_snapshot_json(self, hour: SnapshotHour) -> Path:
        return self.hourly_hour_dir(hour) / "auth_preference_snapshot.json"

    def hourly_status_sqlite(self, hour: SnapshotHour) -> Path:
        return self.hourly_hour_dir(hour) / "snapshot_status.sqlite"

    def hourly_parts_dir(self, hour: SnapshotHour) -> Path:
        return self.hourly_hour_dir(hour) / "parts"

    def hourly_logs_dir(self, hour: SnapshotHour) -> Path:
        return self.hourly_hour_dir(hour) / "logs"

    def hourly_snapshot_log(self, hour: SnapshotHour) -> Path:
        return self.hourly_logs_dir(hour) / "snapshot.log"

    @property
    def wide_root(self) -> Path:
        return self.out_base / "wide"

    def wide_day_dir(self, date_yyyy_mm_dd: str) -> Path:
        return self.wide_root / date_yyyy_mm_dd

    def wide_manifest_json(self, date_yyyy_mm_dd: str) -> Path:
        return self.wide_day_dir(date_yyyy_mm_dd) / "run_manifest.json"

    def wide_progress_json(self, date_yyyy_mm_dd: str) -> Path:
        return self.wide_day_dir(date_yyyy_mm_dd) / "progress.json"

    def wide_http_stats_csv(self, date_yyyy_mm_dd: str) -> Path:
        return self.wide_day_dir(date_yyyy_mm_dd) / "http_stats.csv"

    def wide_request_provenance_csv(self, date_yyyy_mm_dd: str) -> Path:
        return self.wide_day_dir(date_yyyy_mm_dd) / "request_provenance.csv"

    def wide_quality_report_json(self, date_yyyy_mm_dd: str) -> Path:
        return self.wide_day_dir(date_yyyy_mm_dd) / "quality_report.json"

    def wide_parts_dir(self, date_yyyy_mm_dd: str) -> Path:
        return self.wide_day_dir(date_yyyy_mm_dd) / "parts"

    def wide_logs_dir(self, date_yyyy_mm_dd: str) -> Path:
        return self.wide_day_dir(date_yyyy_mm_dd) / "logs"

    def wide_log(self, date_yyyy_mm_dd: str) -> Path:
        return self.wide_logs_dir(date_yyyy_mm_dd) / "wide.log"

    @property
    def authors_root(self) -> Path:
        return self.out_base / "authors"

    def authors_day_dir(self, date_yyyy_mm_dd: str) -> Path:
        return self.authors_root / date_yyyy_mm_dd

    def authors_manifest_json(self, date_yyyy_mm_dd: str) -> Path:
        return self.authors_day_dir(date_yyyy_mm_dd) / "run_manifest.json"

    def authors_progress_json(self, date_yyyy_mm_dd: str) -> Path:
        return self.authors_day_dir(date_yyyy_mm_dd) / "progress.json"

    def authors_http_stats_csv(self, date_yyyy_mm_dd: str) -> Path:
        return self.authors_day_dir(date_yyyy_mm_dd) / "http_stats.csv"

    def authors_request_provenance_csv(self, date_yyyy_mm_dd: str) -> Path:
        return self.authors_day_dir(date_yyyy_mm_dd) / "request_provenance.csv"

    def authors_quality_report_json(self, date_yyyy_mm_dd: str) -> Path:
        return self.authors_day_dir(date_yyyy_mm_dd) / "quality_report.json"

    @property
    def feed_generators_root(self) -> Path:
        return self.out_base / "feed_generators"

    def feed_generators_day_dir(self, date_yyyy_mm_dd: str) -> Path:
        return self.feed_generators_root / date_yyyy_mm_dd

    def feed_generators_manifest_json(self, date_yyyy_mm_dd: str) -> Path:
        return self.feed_generators_day_dir(date_yyyy_mm_dd) / "run_manifest.json"

    def feed_generators_progress_json(self, date_yyyy_mm_dd: str) -> Path:
        return self.feed_generators_day_dir(date_yyyy_mm_dd) / "progress.json"

    def feed_generators_http_stats_csv(self, date_yyyy_mm_dd: str) -> Path:
        return self.feed_generators_day_dir(date_yyyy_mm_dd) / "http_stats.csv"

    def feed_generators_request_provenance_csv(self, date_yyyy_mm_dd: str) -> Path:
        return self.feed_generators_day_dir(date_yyyy_mm_dd) / "request_provenance.csv"

    def feed_generators_quality_report_json(self, date_yyyy_mm_dd: str) -> Path:
        return self.feed_generators_day_dir(date_yyyy_mm_dd) / "quality_report.json"

    @property
    def interactions_root(self) -> Path:
        return self.out_base / "interactions"

    def interactions_day_dir(self, date_yyyy_mm_dd: str) -> Path:
        return self.interactions_root / date_yyyy_mm_dd

    def interactions_manifest_json(self, date_yyyy_mm_dd: str) -> Path:
        return self.interactions_day_dir(date_yyyy_mm_dd) / "run_manifest.json"

    def interactions_progress_json(self, date_yyyy_mm_dd: str) -> Path:
        return self.interactions_day_dir(date_yyyy_mm_dd) / "progress.json"

    def interactions_http_stats_csv(self, date_yyyy_mm_dd: str) -> Path:
        return self.interactions_day_dir(date_yyyy_mm_dd) / "http_stats.csv"

    def interactions_request_provenance_csv(self, date_yyyy_mm_dd: str) -> Path:
        return self.interactions_day_dir(date_yyyy_mm_dd) / "request_provenance.csv"

    def interactions_quality_report_json(self, date_yyyy_mm_dd: str) -> Path:
        return self.interactions_day_dir(date_yyyy_mm_dd) / "quality_report.json"

    @property
    def control_root(self) -> Path:
        return self.out_base / "control"

    @property
    def control_db_path(self) -> Path:
        return self.control_root / "control_state.db"

    @property
    def feed_generators_index_checkpoint_json(self) -> Path:
        return self.control_root / "feed_generators_index_checkpoint.json"

    @property
    def logs_root(self) -> Path:
        return self.out_base / "logs"

    @property
    def global_collector_log(self) -> Path:
        return self.logs_root / "collector.log"

    @property
    def global_errors_log(self) -> Path:
        return self.logs_root / "errors.log"
