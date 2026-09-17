import shutil
import subprocess
from unittest.mock import MagicMock

import pytest
import requests

from folios import doctor
from folios import seed as seed_module
from folios.google import auth as google_auth
from tests.test_seed import EXAMPLE_CONFIG

# --- docker ---------------------------------------------------------------


def test_check_docker_missing_binary(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: None)
    result = doctor.check_docker()
    assert result.ok is False
    assert "docker.com" in result.message


def test_check_docker_not_running(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/docker")
    monkeypatch.setattr(
        subprocess,
        "run",
        MagicMock(side_effect=subprocess.CalledProcessError(1, "docker info")),
    )
    result = doctor.check_docker()
    assert result.ok is False
    assert "not running" in result.message


def test_check_docker_running(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/docker")
    monkeypatch.setattr(subprocess, "run", MagicMock(return_value=None))
    result = doctor.check_docker()
    assert result.ok is True


# --- ports ------------------------------------------------------------------


def test_check_port_free(monkeypatch):
    monkeypatch.setattr(doctor, "_port_is_listening", lambda port: False)
    result = doctor.check_port(5432, "Postgres", "folios-postgres")
    assert result.ok is True
    assert "free" in result.message


def test_check_port_ours(monkeypatch):
    monkeypatch.setattr(doctor, "_port_is_listening", lambda port: True)
    monkeypatch.setattr(doctor, "_running_container_names", lambda: {"folios-postgres"})
    result = doctor.check_port(5432, "Postgres", "folios-postgres")
    assert result.ok is True
    assert "up on port" in result.message


def test_check_port_occupied_by_something_else(monkeypatch):
    monkeypatch.setattr(doctor, "_port_is_listening", lambda port: True)
    monkeypatch.setattr(doctor, "_running_container_names", lambda: {"some-other-thing"})
    result = doctor.check_port(5432, "Postgres", "folios-postgres")
    assert result.ok is False
    assert "already in use" in result.message


# --- google credentials -----------------------------------------------------


@pytest.fixture(autouse=True)
def isolated_credentials_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(google_auth, "CREDENTIALS_DIR", tmp_path)
    monkeypatch.setattr(google_auth, "CLIENT_SECRET_PATH", tmp_path / "client_secret.json")
    monkeypatch.setattr(google_auth, "TOKEN_PATH", tmp_path / "token.json")
    return tmp_path


def test_check_google_credentials_missing_client_secret():
    result = doctor.check_google_credentials()
    assert result.ok is False
    assert "client_secret.json" in result.message


def test_check_google_credentials_no_token():
    google_auth.CLIENT_SECRET_PATH.write_text("{}")
    result = doctor.check_google_credentials()
    assert result.ok is False
    assert "folios auth" in result.message


def test_check_google_credentials_valid(monkeypatch):
    google_auth.CLIENT_SECRET_PATH.write_text("{}")
    monkeypatch.setattr(google_auth, "load_credentials", lambda: MagicMock(valid=True))
    result = doctor.check_google_credentials()
    assert result.ok is True


def test_check_google_credentials_expired_but_refreshable(monkeypatch):
    google_auth.CLIENT_SECRET_PATH.write_text("{}")
    monkeypatch.setattr(
        google_auth,
        "load_credentials",
        lambda: MagicMock(valid=False, expired=True, refresh_token="r"),
    )
    result = doctor.check_google_credentials()
    assert result.ok is True


def test_check_google_credentials_invalid(monkeypatch):
    google_auth.CLIENT_SECRET_PATH.write_text("{}")
    monkeypatch.setattr(
        google_auth,
        "load_credentials",
        lambda: MagicMock(valid=False, expired=False, refresh_token=None),
    )
    result = doctor.check_google_credentials()
    assert result.ok is False
    assert "folios auth" in result.message


# --- reachability -------------------------------------------------------


def test_check_yahoo_reachable(monkeypatch):
    monkeypatch.setattr(requests, "get", MagicMock(return_value=MagicMock()))
    result = doctor.check_yahoo()
    assert result.ok is True


def test_check_yahoo_unreachable(monkeypatch):
    monkeypatch.setattr(requests, "get", MagicMock(side_effect=requests.ConnectionError()))
    result = doctor.check_yahoo()
    assert result.ok is False
    assert "Yahoo Finance" in result.message


def test_check_frankfurter_unreachable(monkeypatch):
    monkeypatch.setattr(requests, "get", MagicMock(side_effect=requests.Timeout()))
    result = doctor.check_frankfurter()
    assert result.ok is False


# --- config ---------------------------------------------------------------


def test_check_config_missing_accounts_file(tmp_path, monkeypatch):
    monkeypatch.setattr(seed_module, "CONFIG_DIR", tmp_path)
    result = doctor.check_config()
    assert result.ok is False
    assert "accounts.yml" in result.message


def test_check_config_parses_cleanly(monkeypatch):
    monkeypatch.setattr(seed_module, "CONFIG_DIR", EXAMPLE_CONFIG)
    result = doctor.check_config()
    assert result.ok is True


def test_check_config_reports_dimension_errors(tmp_path, monkeypatch):
    shutil.copytree(EXAMPLE_CONFIG, tmp_path, dirs_exist_ok=True)
    (tmp_path / "instruments.csv").write_text(
        "instrument_id,isin,yf_symbol,name,asset_class,instrument_type,currency,"
        "region,sector,issuer,domicile_country,protection_type,is_pea_eligible,"
        "price_source,is_active\n"
        "BAD-ONE,,BAD,Bad Instrument,NOT_A_REAL_ASSET_CLASS,SHARE,EUR,,,,,,"
        ",yfinance,true\n"
    )
    monkeypatch.setattr(seed_module, "CONFIG_DIR", tmp_path)
    result = doctor.check_config()
    assert result.ok is False
    assert "NOT_A_REAL_ASSET_CLASS" in result.message


# --- migrations -------------------------------------------------------------


def test_check_migrations_pending(clean_test_db):
    result = doctor.check_migrations(clean_test_db)
    assert result.ok is False
    assert "folios init" in result.message


def test_check_migrations_up_to_date(migrated_conn):
    result = doctor.check_migrations(migrated_conn)
    assert result.ok is True


# --- run_doctor orchestration -----------------------------------------------


def test_run_doctor_reports_database_connection_failure(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/docker")
    monkeypatch.setattr(subprocess, "run", MagicMock(return_value=None))
    monkeypatch.setattr(doctor, "_port_is_listening", lambda port: False)
    monkeypatch.setattr(requests, "get", MagicMock(return_value=MagicMock()))
    monkeypatch.setattr(seed_module, "CONFIG_DIR", EXAMPLE_CONFIG)
    monkeypatch.setattr(
        doctor.db_module, "connect", MagicMock(side_effect=RuntimeError("connection refused"))
    )

    checks = doctor.run_doctor()

    db_check = next(c for c in checks if c.name == "database")
    assert db_check.ok is False
    assert "make up" in db_check.message
    assert not any(c.name == "migrations" for c in checks)


def test_run_doctor_happy_path(monkeypatch, migrated_conn):
    google_auth.CLIENT_SECRET_PATH.write_text("{}")
    monkeypatch.setattr(google_auth, "load_credentials", lambda: MagicMock(valid=True))
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/docker")
    monkeypatch.setattr(subprocess, "run", MagicMock(return_value=None))
    monkeypatch.setattr(doctor, "_port_is_listening", lambda port: False)
    monkeypatch.setattr(requests, "get", MagicMock(return_value=MagicMock()))
    monkeypatch.setattr(seed_module, "CONFIG_DIR", EXAMPLE_CONFIG)
    monkeypatch.setattr(doctor.db_module, "connect", lambda: migrated_conn)

    checks = doctor.run_doctor()

    assert [c.name for c in checks] == [
        "docker", "port 5432", "port 3000", "google credentials",
        "Yahoo Finance", "frankfurter.dev", "config", "migrations",
    ]
    assert all(c.ok for c in checks)
