from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta


def now_utc() -> datetime:
    return datetime.now(tz=UTC)


def format_utc(dt: datetime) -> str:
    if dt.tzinfo is None:
        raise ValueError("format_utc requires timezone-aware datetime")
    return dt.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def utc_date_str(dt: datetime) -> str:
    return dt.astimezone(UTC).date().isoformat()


def utc_hour_str(dt: datetime) -> str:
    # Hour directory name must be zero-padded.
    return f"{dt.astimezone(UTC).hour:02d}"


def floor_to_hour_utc(dt: datetime) -> datetime:
    dt_utc = dt.astimezone(UTC)
    return dt_utc.replace(minute=0, second=0, microsecond=0)


def floor_to_window_utc(dt: datetime, *, window_minutes: int) -> datetime:
    if window_minutes <= 0 or 60 % int(window_minutes) != 0:
        raise ValueError(f"window_minutes must evenly divide 60, got {window_minutes}")
    dt_utc = dt.astimezone(UTC)
    floored_minute = (dt_utc.minute // int(window_minutes)) * int(window_minutes)
    return dt_utc.replace(minute=floored_minute, second=0, microsecond=0)


def ceil_to_window_utc(dt: datetime, *, window_minutes: int) -> datetime:
    floored = floor_to_window_utc(dt, window_minutes=window_minutes)
    if dt.astimezone(UTC) == floored:
        return floored
    return floored + timedelta(minutes=int(window_minutes))


@dataclass(frozen=True)
class SnapshotHour:
    hour_utc: datetime

    @property
    def date_str(self) -> str:
        return utc_date_str(self.hour_utc)

    @property
    def hour_str(self) -> str:
        return utc_hour_str(self.hour_utc)

    @property
    def hour_iso_z(self) -> str:
        return format_utc(self.hour_utc)


@dataclass(frozen=True)
class MicroWindow:
    start_utc: datetime | None = None
    window_minutes: int = 5
    study_id: str | None = None
    sample_family: str | None = None
    scheduled_window_start_utc: datetime | None = None
    scheduled_window_end_utc: datetime | None = None
    window_index: int = 0
    window_minute: int | None = None

    def __post_init__(self) -> None:
        start = self.start_utc or self.scheduled_window_start_utc
        if start is None:
            raise ValueError("MicroWindow requires start_utc or scheduled_window_start_utc")
        start_utc = start.astimezone(UTC)
        object.__setattr__(self, "start_utc", start_utc)
        object.__setattr__(self, "scheduled_window_start_utc", self.scheduled_window_start_utc or start_utc)
        object.__setattr__(
            self,
            "scheduled_window_end_utc",
            self.scheduled_window_end_utc or (start_utc + timedelta(minutes=int(self.window_minutes))),
        )
        object.__setattr__(self, "window_minute", int(self.window_minute if self.window_minute is not None else start_utc.minute))

    @property
    def end_utc(self) -> datetime:
        return self.start_utc.astimezone(UTC) + timedelta(minutes=int(self.window_minutes))

    @property
    def date_str(self) -> str:
        return utc_date_str(self.start_utc)

    @property
    def hour_str(self) -> str:
        return utc_hour_str(self.start_utc)

    @property
    def minute_str(self) -> str:
        return f"{self.start_utc.astimezone(UTC).minute:02d}"

    @property
    def minute_int(self) -> int:
        return int(self.window_minute if self.window_minute is not None else self.start_utc.astimezone(UTC).minute)

    @property
    def start_iso_z(self) -> str:
        return format_utc(self.start_utc)

    @property
    def end_iso_z(self) -> str:
        return format_utc(self.end_utc)
