from __future__ import annotations

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

from folios.validate import REPO_ROOT

CREDENTIALS_DIR = REPO_ROOT / ".credentials"
CLIENT_SECRET_PATH = CREDENTIALS_DIR / "client_secret.json"
TOKEN_PATH = CREDENTIALS_DIR / "token.json"

# drive.file only grants access to files the app itself creates — folios
# can never read the user's other Drive files. See PRIVACY.md.
SCOPES = [
    "https://www.googleapis.com/auth/forms.body",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.file",
]


class MissingClientSecretError(Exception):
    def __init__(self) -> None:
        super().__init__(
            f"{CLIENT_SECRET_PATH} not found. Create an OAuth 2.0 Desktop app "
            f"client in Google Cloud Console and download it there first "
            f"(build-plan step 13 / §7 — this is one of the things you do by hand)."
        )


def load_credentials() -> Credentials | None:
    """Existing credentials from .credentials/token.json, or None if
    `folios auth` has never been run. May be expired — get_credentials()
    is what silently refreshes; this is the raw read."""
    if not TOKEN_PATH.exists():
        return None
    return Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)


def _save_credentials(creds: Credentials) -> None:
    CREDENTIALS_DIR.mkdir(parents=True, exist_ok=True)
    TOKEN_PATH.write_text(creds.to_json())


def authenticate() -> Credentials:
    """Runs the OAuth installed-app flow, opening a browser once. This is
    what `folios auth` calls directly; everything else should call
    get_credentials() instead, which only falls back to this when there
    is truly no usable token yet."""
    if not CLIENT_SECRET_PATH.exists():
        raise MissingClientSecretError
    flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_SECRET_PATH), SCOPES)
    creds = flow.run_local_server(port=0)
    _save_credentials(creds)
    return creds


def get_credentials() -> Credentials:
    """The single entry point every Google API call (forms/sheets/drive,
    in later steps) goes through. Refreshes silently when possible; only
    opens a browser if there is no usable token at all — matching
    build-plan step 13's "Done when": `folios auth` opens a browser once,
    and every subsequent Google call succeeds without re-prompting."""
    creds = load_credentials()

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        _save_credentials(creds)
        return creds

    return authenticate()
