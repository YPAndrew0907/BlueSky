#!/usr/bin/env python3
"""
Download 60 venue-filtered papers (2021–2025) and zip them.

Usage:
  python download_and_zip.py --manifest papers_manifest.csv --out-dir ./pdfs --zip ./papers_60.zip
"""

from __future__ import annotations

import argparse
import csv
import os
import re
import sys
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlparse

import requests

UA = "paper-pack-downloader/1.0 (+https://example.com; non-commercial academic use)"

@dataclass
class Paper:
    paper_id: str
    category: str
    venue: str
    year: str
    title: str
    doi: str
    paper_url: str
    pdf_url: str

def slugify(s: str, max_len: int = 140) -> str:
    s = s.strip()
    # collapse whitespace
    s = re.sub(r"\s+", " ", s)
    # remove unsafe filesystem chars
    s = re.sub(r"[\\/:*?\"<>|]+", "", s)
    s = s.replace("\u2019", "'").replace("\u201c", '"').replace("\u201d", '"')
    if len(s) > max_len:
        s = s[:max_len].rstrip()
    return s

def read_manifest(path: Path) -> List[Paper]:
    papers: List[Paper] = []
    with path.open("r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            papers.append(Paper(
                paper_id=row.get("paper_id","").strip(),
                category=row.get("category","").strip(),
                venue=row.get("venue","").strip(),
                year=row.get("year","").strip(),
                title=row.get("title","").strip(),
                doi=row.get("doi","").strip(),
                paper_url=row.get("paper_url","").strip(),
                pdf_url=row.get("pdf_url","").strip(),
            ))
    return papers

def http_get(url: str, *, timeout: int = 45) -> requests.Response:
    headers = {"User-Agent": UA}
    return requests.get(url, headers=headers, timeout=timeout, allow_redirects=True)

def resolve_pdf_url(p: Paper) -> str:
    # If provided, trust it.
    if p.pdf_url:
        return p.pdf_url

    pu = p.paper_url

    # ACL Anthology: https://aclanthology.org/<ID>/ -> https://aclanthology.org/<ID>.pdf
    m = re.match(r"^https?://aclanthology\.org/([^/]+)/?$", pu)
    if m:
        return f"https://aclanthology.org/{m.group(1)}.pdf"

    # ACM DL DOI page -> try the direct DOI PDF endpoint
    if "dl.acm.org/doi/" in pu and p.doi:
        return f"https://dl.acm.org/doi/pdf/{p.doi}"

    # USENIX already has direct PDFs in manifest typically.
    if "usenix.org" in pu and p.doi and p.doi.startswith("10.5555/"):
        # fallback: try to find a system/files PDF on the page
        try:
            r = http_get(pu)
            if r.status_code == 200:
                pdfs = re.findall(r'href="([^"]+\.pdf)"', r.text, flags=re.I)
                for href in pdfs:
                    if "system/files" in href:
                        return urljoin(pu, href)
        except Exception:
            pass

    # AAAI OJS (ICWSM): scrape for /article/(download|view)/<id>/<fileid>
    if "ojs.aaai.org/index.php/ICWSM/article/" in pu:
        r = http_get(pu)
        if r.status_code != 200:
            raise RuntimeError(f"OJS page not reachable: {pu} (status {r.status_code})")
        # The PDF link sometimes lacks ".pdf"; it’s still a PDF response.
        # Prefer "download" then "view"
        patterns = [
            r'href="([^"]+/article/download/\d+/\d+)"',
            r'href="([^"]+/article/view/\d+/\d+)"',
        ]
        for pat in patterns:
            hits = re.findall(pat, r.text)
            if hits:
                return urljoin(pu, hits[0])
        # fallback: any link containing "download" and the article id
        # (best-effort)
        hits = re.findall(r'href="([^"]+)"', r.text)
        for href in hits:
            if "/article/download/" in href:
                return urljoin(pu, href)
        raise RuntimeError("Could not locate PDF link on OJS page")

    # NDSS: scrape for first .pdf link
    if "ndss-symposium.org" in pu:
        r = http_get(pu)
        if r.status_code != 200:
            raise RuntimeError(f"NDSS page not reachable: {pu} (status {r.status_code})")
        pdfs = re.findall(r'href="([^"]+\.pdf)"', r.text, flags=re.I)
        if pdfs:
            # Prefer wp-content/uploads
            for href in pdfs:
                if "wp-content/uploads" in href:
                    return urljoin(pu, href)
            return urljoin(pu, pdfs[0])
        raise RuntimeError("Could not locate PDF link on NDSS page")

    # If nothing worked
    raise RuntimeError("No PDF URL and no resolver matched")

def download_pdf(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    headers = {"User-Agent": UA}
    with requests.get(url, headers=headers, stream=True, timeout=60, allow_redirects=True) as r:
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code}")
        ctype = (r.headers.get("content-type") or "").lower()
        # Some PDF endpoints return application/pdf; OJS sometimes returns application/octet-stream.
        if ("pdf" not in ctype) and ("octet-stream" not in ctype) and ("binary" not in ctype) and (".pdf" not in url.lower()):
            # Still allow if payload looks like PDF
            first = r.raw.read(5, decode_content=True)
            r.raw.seek(0)
            if first != b"%PDF-":
                raise RuntimeError(f"Unexpected content-type: {ctype}")
        with dest.open("wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 64):
                if chunk:
                    f.write(chunk)

def zip_folder(folder: Path, zip_path: Path) -> None:
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as z:
        for path in folder.rglob("*"):
            if path.is_file():
                arcname = path.relative_to(folder)
                z.write(path, arcname.as_posix())

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", required=True, help="papers_manifest.csv")
    ap.add_argument("--out-dir", required=True, help="Output directory for PDFs")
    ap.add_argument("--zip", required=True, help="Output zip file path")
    ap.add_argument("--sleep", type=float, default=0.4, help="Seconds to sleep between downloads (be polite)")
    args = ap.parse_args()

    manifest = Path(args.manifest).expanduser().resolve()
    out_dir = Path(args.out_dir).expanduser().resolve()
    zip_path = Path(args.zip).expanduser().resolve()

    papers = read_manifest(manifest)

    failures: List[Dict[str, str]] = []

    for i, p in enumerate(papers, start=1):
        cat_dir = out_dir / slugify(p.category, max_len=80)
        # safe filename
        short_title = slugify(p.title or p.paper_id, max_len=120)
        fname = f"[{p.year}] [{p.venue}] - {short_title}.pdf"
        dest = cat_dir / fname

        if dest.exists() and dest.stat().st_size > 50_000:
            print(f"({i}/{len(papers)}) SKIP exists: {dest.name}")
            continue

        try:
            pdf_url = resolve_pdf_url(p)
            print(f"({i}/{len(papers)}) GET {p.paper_id}: {pdf_url}")
            download_pdf(pdf_url, dest)
            print(f"    OK  -> {dest}")
        except Exception as e:
            print(f"    FAIL {p.paper_id}: {e}", file=sys.stderr)
            failures.append({
                "paper_id": p.paper_id,
                "doi": p.doi,
                "paper_url": p.paper_url,
                "pdf_url": p.pdf_url,
                "error": str(e),
            })
        time.sleep(max(0.0, float(args.sleep)))

    # write failures report
    if failures:
        fail_path = Path.cwd() / "failed_downloads.csv"
        with fail_path.open("w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(failures[0].keys()))
            w.writeheader()
            w.writerows(failures)
        print(f"Wrote failures report: {fail_path}")

    # zip
    print(f"Zipping {out_dir} -> {zip_path}")
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    zip_folder(out_dir, zip_path)
    print("Done.")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
