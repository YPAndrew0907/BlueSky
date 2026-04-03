# Convert PPTX → Google Slides

## Fast (no code) method

1. Go to Google Drive.
2. Upload the `.pptx`.
3. Right-click the uploaded file → **Open with** → **Google Slides** (Drive will convert it).

## Scripted method (repeatable)

This folder includes `pptx_to_google_slides.py`, which uploads a local `.pptx` to Drive and converts it into a native Google Slides file.

### 1) Set up a venv + deps

```bash
cd /Users/yipengandrewwang/BlueSky/Slide2
python3 -m venv .venv
.venv/bin/python -m pip install -U pip
.venv/bin/python -m pip install google-api-python-client google-auth-oauthlib google-auth-httplib2
```

### 2) Create OAuth credentials (Desktop)

In Google Cloud Console:
- Enable **Google Drive API**
- Create an **OAuth Client ID** of type **Desktop app**
- Download the JSON and save it somewhere private, e.g. `~/.config/bluesky-slide2/oauth_client.json`

### 3) Convert

```bash
/Users/yipengandrewwang/BlueSky/Slide2/.venv/bin/python /Users/yipengandrewwang/BlueSky/Slide2/pptx_to_google_slides.py \
  --pptx /Users/yipengandrewwang/BlueSky/Slide2/deck_versions/Prof_Meeting_Bluesky_RQs_Data_FINAL_more_examples_v17.pptx \
  --oauth-client ~/.config/bluesky-slide2/oauth_client.json \
  --open
```

Notes:
- OAuth tokens are cached at `~/.config/bluesky-slide2/google_drive_token.json` by default.
- Use `--folder-id <id>` to upload into a specific Drive folder.

