#!/usr/bin/env python3
from __future__ import annotations

import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Literal

from PIL import Image, ImageFilter


BskyShotId = Literal[
    "feeds",
    "feed_whats_hot",
    "feed_for_science",
    "starterpack",
    "search",
]


@dataclass(frozen=True)
class RectRel:
    """Rectangle in relative coordinates (0..1) within an image."""

    x0: float
    y0: float
    x1: float
    y1: float

    def to_px(self, *, width: int, height: int) -> tuple[int, int, int, int]:
        def _clamp(v: float) -> float:
            return max(0.0, min(1.0, v))

        x0 = int(round(_clamp(self.x0) * width))
        y0 = int(round(_clamp(self.y0) * height))
        x1 = int(round(_clamp(self.x1) * width))
        y1 = int(round(_clamp(self.y1) * height))
        if x1 < x0:
            x0, x1 = x1, x0
        if y1 < y0:
            y0, y1 = y1, y0
        return x0, y0, x1, y1


@dataclass(frozen=True)
class CaptureSpec:
    shot_id: BskyShotId
    url: str


@dataclass(frozen=True)
class DerivedSpec:
    asset_id: str
    source_shot_id: BskyShotId
    crop_rel: RectRel | None
    redact_rels: tuple[RectRel, ...]
    out_size_px: tuple[int, int] | None


DEFAULT_CHROME = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")


def _run_chrome_screenshot(
    *,
    chrome: Path,
    url: str,
    out_path: Path,
    window_px: tuple[int, int],
    virtual_time_budget_ms: int,
) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    args = [
        str(chrome),
        "--headless=new",
        "--disable-gpu",
        "--hide-scrollbars",
        "--no-first-run",
        "--no-default-browser-check",
        f"--window-size={window_px[0]},{window_px[1]}",
        f"--virtual-time-budget={virtual_time_budget_ms}",
        f"--screenshot={out_path}",
        url,
    ]
    proc = subprocess.run(args, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        stderr = (proc.stderr or "").strip()
        raise RuntimeError(f"chrome screenshot failed rc={proc.returncode}: {stderr[:300]}")
    if not out_path.exists() or out_path.stat().st_size <= 0:
        raise RuntimeError(f"chrome screenshot produced no file: {out_path}")


def capture_bsky_screenshots(
    *,
    out_dir: Path,
    specs: Iterable[CaptureSpec],
    chrome: Path = DEFAULT_CHROME,
    window_px: tuple[int, int] = (1920, 1080),
    virtual_time_budget_ms: int = 20000,
    retries: int = 3,
) -> dict[BskyShotId, Path]:
    if not chrome.exists():
        raise RuntimeError(f"Chrome not found at: {chrome}")

    out: dict[BskyShotId, Path] = {}
    for spec in specs:
        out_path = out_dir / f"bsky_raw_{spec.shot_id}.png"
        if out_path.exists() and out_path.stat().st_size > 0:
            out[spec.shot_id] = out_path
            continue

        last_err: Exception | None = None
        budget = virtual_time_budget_ms
        for attempt in range(1, retries + 1):
            try:
                _run_chrome_screenshot(
                    chrome=chrome,
                    url=spec.url,
                    out_path=out_path,
                    window_px=window_px,
                    virtual_time_budget_ms=budget,
                )
                out[spec.shot_id] = out_path
                last_err = None
                break
            except Exception as err:  # noqa: BLE001
                last_err = err
                budget = int(budget * 1.6)
                time.sleep(0.35 * attempt)
        if last_err is not None:
            raise RuntimeError(f"failed to capture {spec.shot_id} from {spec.url}: {last_err}") from last_err

    return out


def _pixelate_region(img: Image.Image, rect_px: tuple[int, int, int, int], *, block: int = 18) -> None:
    x0, y0, x1, y1 = rect_px
    if x1 <= x0 or y1 <= y0:
        return
    region = img.crop((x0, y0, x1, y1))
    w, h = region.size
    if w <= 2 or h <= 2:
        return
    bw = max(1, w // block)
    bh = max(1, h // block)
    region = region.resize((bw, bh), resample=Image.NEAREST).resize((w, h), resample=Image.NEAREST)
    img.paste(region, (x0, y0))


def _apply_soft_blur(img: Image.Image, rect_px: tuple[int, int, int, int], *, radius: float = 14.0) -> None:
    x0, y0, x1, y1 = rect_px
    if x1 <= x0 or y1 <= y0:
        return
    region = img.crop((x0, y0, x1, y1))
    region = region.filter(ImageFilter.GaussianBlur(radius=radius))
    img.paste(region, (x0, y0))


def derive_sanitized_images(
    *,
    raw_by_id: dict[BskyShotId, Path],
    out_dir: Path,
    derived: Iterable[DerivedSpec],
) -> dict[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    out: dict[str, Path] = {}
    for spec in derived:
        src = raw_by_id.get(spec.source_shot_id)
        if src is None:
            raise RuntimeError(f"Missing raw screenshot for source_shot_id={spec.source_shot_id}")

        out_path = out_dir / f"{spec.asset_id}.png"
        if out_path.exists() and out_path.stat().st_size > 0:
            out[spec.asset_id] = out_path
            continue

        img = Image.open(src).convert("RGB")
        w, h = img.size
        if spec.crop_rel is not None:
            x0, y0, x1, y1 = spec.crop_rel.to_px(width=w, height=h)
            img = img.crop((x0, y0, x1, y1))
            w, h = img.size

        # Privacy-first: blur/pixelate the risky regions.
        for rr in spec.redact_rels:
            rect = rr.to_px(width=w, height=h)
            _pixelate_region(img, rect, block=22)
            _apply_soft_blur(img, rect, radius=16.0)

        if spec.out_size_px is not None:
            img = img.resize(spec.out_size_px, resample=Image.LANCZOS)

        img.save(out_path)
        out[spec.asset_id] = out_path
    return out


HANDLE_RE = re.compile(r"@[A-Za-z0-9_.-]{2,}")


def redact_inline_handles(text: str) -> str:
    """Redact @handles in text we place on slides (not in screenshots)."""

    return HANDLE_RE.sub("@[redacted]", text)

