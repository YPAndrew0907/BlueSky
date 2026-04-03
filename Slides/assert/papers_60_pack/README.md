# Bluesky / Open-Feed Ecosystem — 60-paper PDF pack (auto-downloader)

This zip **does not contain the PDFs yet** (this environment can’t fetch external PDFs directly), but it *does* contain:

- `papers_manifest.csv` — 60 papers with **venue/year/DOI + a PDF link or a landing-page link**.
- `download_and_zip.py` — a one-command script that **downloads the PDFs**, organizes them by category, and then **creates a single zip**.

## Quick start

1) Unzip this folder somewhere.

2) Install Python deps:
```bash
python -m pip install --upgrade pip
python -m pip install requests
```

3) Download + organize + zip:
```bash
python download_and_zip.py --manifest papers_manifest.csv --out-dir ./pdfs --zip ./papers_60.zip
```

You’ll get:
- `./pdfs/<Category>/[YEAR] [VENUE] - <ShortTitle>.pdf`
- `./papers_60.zip`
- `./failed_downloads.csv` (if anything fails)

## Notes

- ACL Anthology papers should download cleanly (direct PDF links).
- ICWSM (AAAI OJS) papers are downloaded by scraping the “Download PDF” link from the article page.
- ACM DL links may require institutional access; the script still tries (and will record failures).
- NDSS pages are scraped to find a `.pdf` link.

If you want, tell me where you want this to live on your machine and I’ll tailor the default paths.
