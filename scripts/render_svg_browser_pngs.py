#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
from pathlib import Path
from xml.etree import ElementTree as ET

from playwright.sync_api import sync_playwright


CHROME_PATH = Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe")


def _parse_svg_size(svg_text: str) -> tuple[int, int]:
    root = ET.fromstring(svg_text)
    width = root.get("width")
    height = root.get("height")
    if width and height:
        return int(float(width)), int(float(height))

    view_box = root.get("viewBox")
    if not view_box:
        raise ValueError("SVG is missing width/height and viewBox")
    parts = view_box.replace(",", " ").split()
    if len(parts) != 4:
        raise ValueError(f"Unsupported viewBox: {view_box}")
    return int(float(parts[2])), int(float(parts[3]))


def _patch_root_svg_tag(svg_text: str, width: int, height: int) -> str:
    svg_text = re.sub(r"^\s*<\?xml[^>]*>\s*", "", svg_text, count=1)
    svg_text = re.sub(r"^\s*<!DOCTYPE[^>]*>\s*", "", svg_text, count=1, flags=re.IGNORECASE)

    match = re.search(r"<svg\b([^>]*)>", svg_text, flags=re.IGNORECASE | re.DOTALL)
    if not match:
        raise ValueError("Could not find root <svg> tag")

    attrs = match.group(1)
    attrs = re.sub(
        r"""\s(?:id|width|height|preserveAspectRatio)\s*=\s*(".*?"|'.*?'|[^\s>]+)""",
        "",
        attrs,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if "viewBox" not in attrs and "viewbox" not in attrs.lower():
        attrs += f' viewBox="0 0 {width} {height}"'

    root_tag = (
        f'<svg{attrs} id="slide-root" width="{width}" height="{height}" '
        'preserveAspectRatio="xMinYMin meet">'
    )
    return svg_text[: match.start()] + root_tag + svg_text[match.end() :]


def _inline_svg_document(svg_text: str, width: int, height: int) -> str:
    # Force the top-level svg to occupy the full viewport so element screenshots
    # do not inherit browser image-document margins.
    svg_markup = _patch_root_svg_tag(svg_text, width, height)
    return f"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <style>
      html, body {{
        margin: 0;
        width: {width}px;
        height: {height}px;
        overflow: hidden;
        background: #08121E;
      }}

      body {{
        display: block;
      }}

      #slide-root {{
        display: block;
        width: {width}px;
        height: {height}px;
      }}
    </style>
  </head>
  <body>{svg_markup}</body>
</html>
"""


def render_svg_dir(*, src_dir: Path, out_dir: Path) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    rendered: list[Path] = []

    svg_paths = sorted(src_dir.glob("slide_*.svg"))
    if not svg_paths:
        raise FileNotFoundError(f"No slide SVGs found in {src_dir}")

    executable = str(CHROME_PATH) if CHROME_PATH.exists() else None
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=executable, headless=True)
        page = browser.new_page(device_scale_factor=1)
        try:
            for svg_path in svg_paths:
                svg_text = svg_path.read_text(encoding="utf-8")
                width, height = _parse_svg_size(svg_text)
                page.set_viewport_size({"width": width, "height": height})
                page.set_content(_inline_svg_document(svg_text, width, height), wait_until="load")
                page.locator("#slide-root").screenshot(path=str(out_dir / f"{svg_path.stem}.png"))
                rendered.append(out_dir / f"{svg_path.stem}.png")
        finally:
            page.close()
            browser.close()
    return rendered


def render_build_root(build_root: Path) -> list[tuple[Path, Path]]:
    rendered_dirs: list[tuple[Path, Path]] = []
    previews_dir = build_root / "previews"
    for composite_dir in sorted(previews_dir.glob("*_composite")):
        out_dir = composite_dir.with_name(composite_dir.name.replace("_composite", "_browser_png"))
        render_svg_dir(src_dir=composite_dir, out_dir=out_dir)
        rendered_dirs.append((composite_dir, out_dir))
    return rendered_dirs


def main() -> None:
    parser = argparse.ArgumentParser(description="Render composite SVG slide previews into browser PNGs without page whitespace.")
    parser.add_argument(
        "--build-root",
        type=Path,
        help="Build date folder containing a previews directory, e.g. _build/2026-03-07",
    )
    parser.add_argument(
        "--src-dir",
        type=Path,
        help="Composite SVG directory, e.g. _build/2026-03-07/previews/foo_composite",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        help="Output PNG directory. Required with --src-dir.",
    )
    args = parser.parse_args()

    if args.build_root:
        rendered = render_build_root(args.build_root.resolve())
        for src_dir, out_dir in rendered:
            print(f"OK: rendered {src_dir} -> {out_dir}")
        return

    if args.src_dir and args.out_dir:
        render_svg_dir(src_dir=args.src_dir.resolve(), out_dir=args.out_dir.resolve())
        print(f"OK: rendered {args.src_dir} -> {args.out_dir}")
        return

    raise SystemExit("Pass either --build-root or both --src-dir and --out-dir")


if __name__ == "__main__":
    main()
