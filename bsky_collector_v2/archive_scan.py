from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


SURFACE_HOURLY = "hourly"
SURFACE_WIDE = "wide"
VALID_SURFACES = {SURFACE_HOURLY, SURFACE_WIDE}


@dataclass(frozen=True)
class RunPartition:
    branch: str
    surface: str
    date_utc: str
    hour_utc: str | None
    base_dir: Path
    parts_dir: Path

    @property
    def snapshot_key(self) -> str:
        if self.hour_utc is None:
            return f"{self.branch}:{self.surface}:{self.date_utc}"
        return f"{self.branch}:{self.surface}:{self.date_utc}T{self.hour_utc}"


def date_in_range(date_utc: str, start_date: str | None, end_date: str | None) -> bool:
    if start_date and date_utc < start_date:
        return False
    if end_date and date_utc > end_date:
        return False
    return True


def iter_branch_roots(data_root: Path, include_labelerexp: bool) -> list[tuple[str, Path]]:
    roots: list[tuple[str, Path]] = [("main", data_root)]
    if include_labelerexp:
        labeler_root = data_root / "labelerexp"
        if labeler_root.exists():
            roots.append(("labelerexp", labeler_root))
    return roots


def iter_partitions(
    *,
    root: Path,
    branch: str,
    surfaces: list[str],
    start_date: str | None,
    end_date: str | None,
) -> list[RunPartition]:
    partitions: list[RunPartition] = []
    for surface in surfaces:
        if surface not in VALID_SURFACES:
            raise ValueError(f"unsupported surface: {surface}")
        surface_root = root / surface
        if not surface_root.exists():
            continue
        if surface == SURFACE_HOURLY:
            for date_dir in sorted(path for path in surface_root.iterdir() if path.is_dir()):
                if not date_in_range(date_dir.name, start_date, end_date):
                    continue
                for hour_dir in sorted(path for path in date_dir.iterdir() if path.is_dir()):
                    parts_dir = hour_dir / "parts"
                    if not parts_dir.exists():
                        continue
                    partitions.append(
                        RunPartition(
                            branch=branch,
                            surface=surface,
                            date_utc=date_dir.name,
                            hour_utc=hour_dir.name,
                            base_dir=hour_dir,
                            parts_dir=parts_dir,
                        )
                    )
        else:
            for date_dir in sorted(path for path in surface_root.iterdir() if path.is_dir()):
                if not date_in_range(date_dir.name, start_date, end_date):
                    continue
                parts_dir = date_dir / "parts"
                if not parts_dir.exists():
                    continue
                partitions.append(
                    RunPartition(
                        branch=branch,
                        surface=surface,
                        date_utc=date_dir.name,
                        hour_utc=None,
                        base_dir=date_dir,
                        parts_dir=parts_dir,
                    )
                )
    return partitions
