#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from typing import NotRequired, Sequence, TypedDict


GOOGLE_SLIDES_MIMETYPE = "application/vnd.google-apps.presentation"
PPTX_MIMETYPE = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
SCOPES: tuple[str, ...] = ("https://www.googleapis.com/auth/drive",)


class DriveFile(TypedDict):
    id: str
    name: str
    mimeType: str
    webViewLink: NotRequired[str]


@dataclass(frozen=True)
class AuthConfig:
    token_path: Path
    oauth_client_path: Path | None
    service_account_path: Path | None
    no_browser: bool


def _import_google_deps() -> tuple[object, object, object, object, object, object, object]:
    try:
        from google.auth.transport.requests import Request  # type: ignore[import-not-found]
        from google.oauth2.credentials import Credentials  # type: ignore[import-not-found]
        from google.oauth2.service_account import Credentials as ServiceAccountCredentials  # type: ignore[import-not-found]
        from google_auth_oauthlib.flow import InstalledAppFlow  # type: ignore[import-not-found]
        from googleapiclient.discovery import build  # type: ignore[import-not-found]
        from googleapiclient.errors import HttpError  # type: ignore[import-not-found]
        from googleapiclient.http import MediaFileUpload  # type: ignore[import-not-found]
    except ImportError as err:  # pragma: no cover
        msg = (
            "Missing Google API dependencies.\n\n"
            "Install:\n"
            "  python3 -m pip install --user google-api-python-client google-auth-oauthlib google-auth-httplib2\n"
        )
        raise SystemExit(msg) from err

    return Request, Credentials, ServiceAccountCredentials, InstalledAppFlow, build, HttpError, MediaFileUpload


def _read_json_file(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as err:
        raise FileNotFoundError(f"missing JSON file: {path}") from err
    except json.JSONDecodeError as err:
        raise ValueError(f"invalid JSON in {path}: {err}") from err


def _load_credentials(auth: AuthConfig, *, scopes: Sequence[str]):
    Request, Credentials, ServiceAccountCredentials, InstalledAppFlow, _build, _HttpError, _MediaFileUpload = (
        _import_google_deps()
    )

    if auth.service_account_path is not None:
        if not auth.service_account_path.exists():
            raise FileNotFoundError(f"missing --service-account: {auth.service_account_path}")
        creds = ServiceAccountCredentials.from_service_account_info(  # type: ignore[attr-defined]
            _read_json_file(auth.service_account_path),
            scopes=list(scopes),
        )
        return creds

    if auth.oauth_client_path is None:
        raise ValueError("missing auth: pass --oauth-client or --service-account")
    if not auth.oauth_client_path.exists():
        raise FileNotFoundError(f"missing --oauth-client: {auth.oauth_client_path}")

    creds = None
    if auth.token_path.exists():
        creds = Credentials.from_authorized_user_info(  # type: ignore[attr-defined]
            _read_json_file(auth.token_path),
            scopes=list(scopes),
        )

    if creds is not None and getattr(creds, "valid", False):
        return creds

    if creds is not None and getattr(creds, "expired", False) and getattr(creds, "refresh_token", None):
        creds.refresh(Request())  # type: ignore[call-arg]
    else:
        flow = InstalledAppFlow.from_client_secrets_file(str(auth.oauth_client_path), scopes=list(scopes))
        if auth.no_browser:
            run_console = getattr(flow, "run_console", None)
            if callable(run_console):
                creds = run_console()
            else:
                creds = flow.run_local_server(open_browser=False, port=0)
        else:
            creds = flow.run_local_server(open_browser=True, port=0)

    auth.token_path.parent.mkdir(parents=True, exist_ok=True)
    auth.token_path.write_text(creds.to_json(), encoding="utf-8")  # type: ignore[union-attr]
    return creds


def _upload_pptx_as_google_slides(
    *,
    pptx_path: Path,
    title: str,
    folder_id: str | None,
    supports_all_drives: bool,
    auth: AuthConfig,
) -> DriveFile:
    if not pptx_path.exists():
        raise FileNotFoundError(f"missing --pptx: {pptx_path}")
    if pptx_path.suffix.lower() != ".pptx":
        raise ValueError(f"--pptx must be a .pptx file: {pptx_path}")

    Request, Credentials, ServiceAccountCredentials, InstalledAppFlow, build, HttpError, MediaFileUpload = (
        _import_google_deps()
    )

    creds = _load_credentials(auth, scopes=SCOPES)
    service = build("drive", "v3", credentials=creds)

    file_metadata: dict[str, object] = {"name": title, "mimeType": GOOGLE_SLIDES_MIMETYPE}
    if folder_id:
        file_metadata["parents"] = [folder_id]

    media = MediaFileUpload(str(pptx_path), mimetype=PPTX_MIMETYPE, resumable=True)

    try:
        created = (
            service.files()
            .create(
                body=file_metadata,
                media_body=media,
                fields="id,name,mimeType,webViewLink",
                supportsAllDrives=supports_all_drives,
            )
            .execute()
        )
    except HttpError as err:  # pragma: no cover
        raise RuntimeError(f"Google Drive upload failed: {err}") from err

    return created


def _default_token_path() -> Path:
    return Path.home() / ".config" / "bluesky-slide2" / "google_drive_token.json"


def main(argv: Sequence[str] | None = None) -> int:
    here = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(
        description="Upload a PPTX to Google Drive and convert it to a Google Slides file.",
    )
    parser.add_argument(
        "--pptx",
        type=Path,
        default=here / "deck_versions/Prof_Meeting_Bluesky_RQs_Data_FINAL_more_examples_v17.pptx",
        help="Path to the local .pptx file.",
    )
    parser.add_argument(
        "--title",
        type=str,
        default=None,
        help="Google Slides filename (defaults to PPTX stem).",
    )
    parser.add_argument(
        "--folder-id",
        type=str,
        default=None,
        help="Optional Google Drive folder id to upload into (works for Shared Drives if --supports-all-drives).",
    )
    auth_group = parser.add_mutually_exclusive_group(required=False)
    auth_group.add_argument(
        "--oauth-client",
        type=Path,
        default=None,
        help="OAuth client secrets JSON (Downloaded from Google Cloud Console).",
    )
    auth_group.add_argument(
        "--service-account",
        type=Path,
        default=None,
        help="Service account JSON (requires Drive access; for Shared Drives the account must be a member).",
    )
    parser.add_argument(
        "--token",
        type=Path,
        default=_default_token_path(),
        help="Token cache path for OAuth (default: ~/.config/bluesky-slide2/google_drive_token.json).",
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="Don't open a browser during OAuth. Uses console auth if available, otherwise local server without auto-open.",
    )
    parser.add_argument(
        "--supports-all-drives",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Pass supportsAllDrives to the Drive API (default: enabled; needed for Shared Drives).",
    )
    parser.add_argument(
        "--open",
        action="store_true",
        help="Open the created Google Slides link in your browser.",
    )
    args = parser.parse_args(argv)

    pptx_path: Path = args.pptx.resolve()
    title = args.title or pptx_path.stem

    created = _upload_pptx_as_google_slides(
        pptx_path=pptx_path,
        title=title,
        folder_id=args.folder_id,
        supports_all_drives=bool(args.supports_all_drives),
        auth=AuthConfig(
            token_path=args.token.expanduser().resolve(),
            oauth_client_path=args.oauth_client.expanduser().resolve() if args.oauth_client else None,
            service_account_path=args.service_account.expanduser().resolve() if args.service_account else None,
            no_browser=bool(args.no_browser),
        ),
    )

    link = created.get("webViewLink")
    print(f"OK: created Google Slides: {created['name']} (id={created['id']})")
    if link:
        print(link)
        if args.open:
            webbrowser.open_new_tab(link)
    else:
        print("NOTE: No webViewLink returned (you can open it from Drive using the id above).")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
