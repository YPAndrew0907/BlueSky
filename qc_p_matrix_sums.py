#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path


SVG_NS = "http://www.w3.org/2000/svg"
NS = {"svg": SVG_NS}


def _val_to_cents(val: str) -> int:
    s = val.strip()
    if not re.match(r"^\d+\.\d{2}$", s):
        raise ValueError(f"Bad value format: {val!r}")
    whole, frac = s.split(".")
    return int(whole) * 100 + int(frac)


def check_p_matrix_svg_sums(*, pptx_path: Path, media_prefix: str = "S1_P_") -> list[str]:
    pat = re.compile(rf"^ppt/media/{re.escape(media_prefix)}(\d\d)_(\d\d)\.svg$")
    vals_cents: dict[tuple[int, int], int] = {}

    with zipfile.ZipFile(pptx_path) as z:
        for name in z.namelist():
            m = pat.match(name)
            if not m:
                continue
            r = int(m.group(1))
            c = int(m.group(2))
            root = ET.fromstring(z.read(name))
            texts = root.findall(".//svg:text", NS)
            if len(texts) != 1 or texts[0].text is None:
                raise ValueError(f"Expected exactly 1 <text> in {name}, got {len(texts)}")
            vals_cents[(r, c)] = _val_to_cents(texts[0].text)

    if not vals_cents:
        raise ValueError(f"No matrix SVGs found for prefix {media_prefix!r}")

    n_rows = max(r for r, _ in vals_cents) + 1
    n_cols = max(c for _, c in vals_cents) + 1
    issues: list[str] = []

    for r in range(n_rows):
        for c in range(n_cols):
            if (r, c) not in vals_cents:
                issues.append(f"missing cell: r={r} c={c}")
                if len(issues) > 40:
                    return issues

    for r in range(n_rows):
        s = sum(vals_cents[(r, c)] for c in range(n_cols))
        if s != 100:
            issues.append(f"row {r} sums to {s/100:.2f} (expected 1.00)")
    for c in range(n_cols):
        s = sum(vals_cents[(r, c)] for r in range(n_rows))
        if s != 100:
            issues.append(f"col {c} sums to {s/100:.2f} (expected 1.00)")

    base = sorted(vals_cents[(0, c)] for c in range(n_cols))
    for r in range(n_rows):
        cur = sorted(vals_cents[(r, c)] for c in range(n_cols))
        if cur != base:
            issues.append(f"row {r} is not a permutation of row 0")

    return issues


def main() -> None:
    parser = argparse.ArgumentParser(description="QC: verify displayed P_{i,j} cell values sum to 1.00 per row/col.")
    parser.add_argument("pptx", type=Path, nargs="?", default=Path("_build/ifx_poster_animated_01.pptx"))
    parser.add_argument("--prefix", default="S1_P_", help="Media prefix, e.g. S1_P_ (default).")
    args = parser.parse_args()

    pptx_path = args.pptx.resolve()
    if not pptx_path.exists():
        raise SystemExit(f"Missing pptx: {pptx_path}")

    issues = check_p_matrix_svg_sums(pptx_path=pptx_path, media_prefix=str(args.prefix))
    if issues:
        print("FAIL: P matrix sums check failed")
        for line in issues[:80]:
            print(line)
        if len(issues) > 80:
            print(f"... ({len(issues)} total)")
        raise SystemExit(1)

    print("OK: P matrix sums to 1.00 (rows+cols)")


if __name__ == "__main__":
    main()

