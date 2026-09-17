from __future__ import annotations

import os
import shutil
import socket
import subprocess
from dataclasses import dataclass
from typing import Any

import requests
from dotenv import load_dotenv

from folios import db as db_module
from folios import fx, seed
from folios.google import auth as google_auth
from folios.validate import REPO_ROOT

# One message per problem, each naming the fix — build-plan step 20.
# Most people who abandon a self-hosted tool do so in the first twenty
# minutes, on an error that assumes they already know what's wrong.


@dataclass
class Check:
    name: str
    ok: bool
    message: str

    def __str__(self) -> str:
        return f"[{'ok' if self.ok else 'FAIL'}] {self.name}: {self.message}"


def check_docker() -> Check:
    if shutil.which("docker") is None:
        return Check(
            "docker", False,
            "docker not found on PATH — install Docker Desktop: "
            "https://www.docker.com/products/docker-desktop/",
        )
    try:
        subprocess.run(
            ["docker", "info"], capture_output=True, timeout=10, check=True
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        return Check("docker", False, "Docker is installed but not running — start Docker Desktop")
    return Check("docker", True, "Docker is running")


def _port_is_listening(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1)
        try:
            s.connect(("127.0.0.1", port))
            return True
        except OSError:
            return False


def _running_container_names() -> set[str]:
    try:
        result = subprocess.run(
            ["docker", "ps", "--format", "{{.Names}}"],
            capture_output=True, timeout=10, check=True, text=True,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        return set()
    return set(result.stdout.split())


def check_port(port: int, service: str, container_name: str) -> Check:
    if not _port_is_listening(port):
        return Check(f"port {port}", True, f"port {port} is free for {service}")
    if container_name in _running_container_names():
        return Check(f"port {port}", True, f"{service} is up on port {port} ({container_name})")
    return Check(
        f"port {port}", False,
        f"port {port} is already in use by something other than {container_name} — "
        f"stop whatever's using it, or change its port in .env",
    )


def check_google_credentials() -> Check:
    if not google_auth.CLIENT_SECRET_PATH.exists():
        return Check(
            "google credentials", False,
            f"{google_auth.CLIENT_SECRET_PATH} not found — see SETUP.md's "
            f"Google Cloud OAuth walkthrough",
        )
    creds = google_auth.load_credentials()
    if creds is None:
        return Check("google credentials", False, "no token yet — run `folios auth`")
    if creds.valid or (creds.expired and creds.refresh_token):
        return Check("google credentials", True, "Google credentials are valid")
    return Check(
        "google credentials", False, "Google credentials are invalid — run `folios auth` again"
    )


def _url_reachable(name: str, url: str, fix: str) -> Check:
    try:
        requests.get(url, timeout=5)
    except requests.RequestException:
        return Check(name, False, f"{name} unreachable at {url} — {fix}")
    return Check(name, True, f"{name} is reachable")


def check_yahoo() -> Check:
    return _url_reachable(
        "Yahoo Finance", "https://query1.finance.yahoo.com",
        "check your internet connection or a firewall blocking it",
    )


def check_frankfurter() -> Check:
    return _url_reachable(
        "frankfurter.dev", fx.DEFAULT_BASE_URL, "check your internet connection"
    )


def check_config() -> Check:
    config_dir = seed.CONFIG_DIR
    accounts_path = config_dir / "accounts.yml"
    if not accounts_path.exists():
        return Check(
            "config", False,
            f"{accounts_path} not found — copy config/example/ to get started, "
            f"or create your own (see SETUP.md)",
        )
    try:
        dimensions = seed.load_dimensions(config_dir / "dimensions.csv")
        accounts = seed.load_accounts(accounts_path)
        instruments = seed.load_instruments(config_dir / "instruments.csv")
        errors = seed.validate_dimension_values(dimensions, accounts, instruments)
    except Exception as exc:  # noqa: BLE001 - any parse failure is a doctor finding, not a crash
        return Check("config", False, f"config/ failed to parse: {exc}")
    if errors:
        return Check("config", False, "; ".join(errors))
    return Check("config", True, "config/ parses cleanly")


def check_migrations(conn: Any) -> Check:
    pending = db_module.pending_migrations(conn)
    if pending:
        names = ", ".join(p.name for p in pending)
        return Check("migrations", False, f"pending: {names} — run `folios init`")
    return Check("migrations", True, "migrations up to date")


def run_doctor() -> list[Check]:
    load_dotenv(REPO_ROOT / ".env")
    postgres_port = int(os.environ.get("POSTGRES_PORT", "5432"))
    metabase_port = int(os.environ.get("METABASE_PORT", "3000"))

    checks = [
        check_docker(),
        check_port(postgres_port, "Postgres", "folios-postgres"),
        check_port(metabase_port, "Metabase", "folios-metabase"),
        check_google_credentials(),
        check_yahoo(),
        check_frankfurter(),
        check_config(),
    ]

    try:
        conn = db_module.connect()
    except Exception as exc:  # noqa: BLE001 - a connection failure is itself the finding
        checks.append(
            Check(
                "database", False,
                f"could not connect to the database: {exc} — run `make up`, "
                f"then `folios init`",
            )
        )
        return checks

    try:
        checks.append(check_migrations(conn))
    finally:
        conn.close()

    return checks
