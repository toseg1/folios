from unittest.mock import MagicMock

import pytest

from folios.google import auth as google_auth


@pytest.fixture(autouse=True)
def isolated_credentials_dir(tmp_path, monkeypatch):
    """Every test gets its own .credentials/ — never touches a real one."""
    monkeypatch.setattr(google_auth, "CREDENTIALS_DIR", tmp_path)
    monkeypatch.setattr(google_auth, "CLIENT_SECRET_PATH", tmp_path / "client_secret.json")
    monkeypatch.setattr(google_auth, "TOKEN_PATH", tmp_path / "token.json")
    return tmp_path


def test_load_credentials_returns_none_when_no_token_file():
    assert google_auth.load_credentials() is None


def test_load_credentials_reads_existing_token(monkeypatch):
    google_auth.TOKEN_PATH.write_text("{}")
    fake_creds = MagicMock()
    from_file = MagicMock(return_value=fake_creds)
    monkeypatch.setattr(google_auth.Credentials, "from_authorized_user_file", from_file)

    result = google_auth.load_credentials()

    assert result is fake_creds
    from_file.assert_called_once_with(str(google_auth.TOKEN_PATH), google_auth.SCOPES)


def test_get_credentials_returns_existing_valid_token_without_authenticating(monkeypatch):
    valid_creds = MagicMock(valid=True)
    monkeypatch.setattr(google_auth, "load_credentials", lambda: valid_creds)
    monkeypatch.setattr(
        google_auth, "authenticate", MagicMock(side_effect=AssertionError("must not be called"))
    )

    assert google_auth.get_credentials() is valid_creds


def test_get_credentials_refreshes_an_expired_token_silently(monkeypatch):
    expired_creds = MagicMock(valid=False, expired=True, refresh_token="r")
    expired_creds.to_json.return_value = '{"token": "refreshed"}'
    monkeypatch.setattr(google_auth, "load_credentials", lambda: expired_creds)
    monkeypatch.setattr(
        google_auth, "authenticate", MagicMock(side_effect=AssertionError("must not be called"))
    )
    monkeypatch.setattr(google_auth, "Request", MagicMock())

    result = google_auth.get_credentials()

    assert result is expired_creds
    expired_creds.refresh.assert_called_once()
    assert google_auth.TOKEN_PATH.exists()  # _save_credentials ran


def test_get_credentials_authenticates_when_there_is_no_usable_token(monkeypatch):
    monkeypatch.setattr(google_auth, "load_credentials", lambda: None)
    fake_creds = MagicMock()
    authenticate = MagicMock(return_value=fake_creds)
    monkeypatch.setattr(google_auth, "authenticate", authenticate)

    result = google_auth.get_credentials()

    assert result is fake_creds
    authenticate.assert_called_once()


def test_authenticate_raises_when_client_secret_is_missing():
    with pytest.raises(google_auth.MissingClientSecretError) as exc_info:
        google_auth.authenticate()
    assert "client_secret.json" in str(exc_info.value)


def test_authenticate_runs_the_installed_app_flow_and_saves_the_token(monkeypatch):
    google_auth.CLIENT_SECRET_PATH.write_text("{}")

    fake_creds = MagicMock()
    fake_creds.to_json.return_value = '{"token": "abc"}'
    fake_flow = MagicMock()
    fake_flow.run_local_server.return_value = fake_creds
    from_secrets = MagicMock(return_value=fake_flow)
    monkeypatch.setattr(
        google_auth.InstalledAppFlow, "from_client_secrets_file", from_secrets
    )

    result = google_auth.authenticate()

    assert result is fake_creds
    from_secrets.assert_called_once_with(str(google_auth.CLIENT_SECRET_PATH), google_auth.SCOPES)
    fake_flow.run_local_server.assert_called_once_with(port=0)
    assert google_auth.TOKEN_PATH.read_text() == '{"token": "abc"}'


def test_scopes_use_drive_file_not_full_drive_access():
    # The whole point of the scope choice (see PRIVACY.md): never the
    # broad `drive` scope, which would grant access to every file.
    assert "https://www.googleapis.com/auth/drive.file" in google_auth.SCOPES
    assert "https://www.googleapis.com/auth/drive" not in google_auth.SCOPES
