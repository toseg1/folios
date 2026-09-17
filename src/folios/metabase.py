from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import requests
import yaml
from dotenv import load_dotenv

from folios.validate import REPO_ROOT

DASHBOARD_CONFIG_PATH = REPO_ROOT / "dashboards" / "main.yml"
DEFAULT_METABASE_URL = "http://localhost:3000"
DEFAULT_DATABASE_NAME = "folios"


class MetabaseConfigError(Exception):
    pass


def get_metabase_url() -> str:
    load_dotenv(REPO_ROOT / ".env")
    return os.environ.get("METABASE_URL", DEFAULT_METABASE_URL).rstrip("/")


def get_api_key() -> str:
    load_dotenv(REPO_ROOT / ".env")
    key = os.environ.get("METABASE_API_KEY")
    if not key or key == "change-me":
        raise MetabaseConfigError(
            "Set METABASE_API_KEY in .env — create one in Metabase under "
            "Admin settings -> Authentication -> API keys."
        )
    return key


def get_database_name() -> str:
    load_dotenv(REPO_ROOT / ".env")
    return os.environ.get("METABASE_DATABASE_NAME", DEFAULT_DATABASE_NAME)


def load_dashboard_config(path: Path | None = None) -> dict[str, Any]:
    path = path if path is not None else DASHBOARD_CONFIG_PATH
    return yaml.safe_load(path.read_text())


class MetabaseClient:
    """Thin wrapper over the handful of Metabase REST endpoints
    dashboard-init needs. `session` defaults to a real requests.Session
    — inject a fake in tests, so nothing here ever makes a real HTTP
    call outside of an actual `folios dashboard-init` run."""

    def __init__(self, base_url: str, api_key: str, session: Any = None):
        self.base_url = base_url.rstrip("/")
        self.session = session or requests.Session()
        self.headers = {"x-api-key": api_key}

    def find_database_id(self, name: str) -> int:
        response = self.session.get(
            f"{self.base_url}/api/database", headers=self.headers, timeout=15
        )
        response.raise_for_status()
        payload = response.json()
        databases = payload.get("data", payload)
        for db in databases:
            if db.get("name") == name:
                return db["id"]
        raise MetabaseConfigError(
            f"No Metabase database connection named {name!r} found. Add one "
            f"first (Admin settings -> Databases -> Add database) pointing "
            f"at the metabase_ro role — see build-plan step 13."
        )

    def create_card(self, database_id: int, name: str, query: str, display: str) -> int:
        body = {
            "name": name,
            "display": display,
            "visualization_settings": {},
            "dataset_query": {
                "type": "native",
                "native": {"query": query},
                "database": database_id,
            },
        }
        response = self.session.post(
            f"{self.base_url}/api/card", json=body, headers=self.headers, timeout=15
        )
        response.raise_for_status()
        return response.json()["id"]

    def create_dashboard(self, name: str, description: str | None = None) -> int:
        body = {"name": name, "description": description}
        response = self.session.post(
            f"{self.base_url}/api/dashboard", json=body, headers=self.headers, timeout=15
        )
        response.raise_for_status()
        return response.json()["id"]

    def add_cards_to_dashboard(self, dashboard_id: int, placements: list[dict[str, Any]]) -> None:
        # Negative temporary ids mark these as new dashcards being added
        # — the same shape the Metabase frontend itself sends when you
        # save a dashboard's layout (v0.48+; confirmed against the
        # locally reachable Metabase, v0.63).
        dashcards = [
            {
                "id": -(i + 1),
                "card_id": placement["card_id"],
                "row": placement["row"],
                "col": placement["col"],
                "size_x": placement["size_x"],
                "size_y": placement["size_y"],
            }
            for i, placement in enumerate(placements)
        ]
        response = self.session.put(
            f"{self.base_url}/api/dashboard/{dashboard_id}",
            json={"dashcards": dashcards},
            headers=self.headers,
            timeout=15,
        )
        response.raise_for_status()


def dashboard_init(
    config: dict[str, Any],
    client: MetabaseClient,
    database_name: str | None = None,
) -> dict[str, Any]:
    """Creates every card in config["cards"], a dashboard, and places
    each card on it per config["layout"] — build-plan step 19. Always
    creates fresh cards/a fresh dashboard; re-running this is not an
    update-in-place (Metabase has no stable identity to match
    dashboards/main.yml's cards against across runs)."""
    database_name = database_name if database_name is not None else DEFAULT_DATABASE_NAME
    database_id = client.find_database_id(database_name)

    card_ids: dict[str, int] = {}
    for card in config["cards"]:
        card_ids[card["name"]] = client.create_card(
            database_id, card["name"], card["query"], card.get("display", "table")
        )

    dashboard_cfg = config["dashboard"]
    dashboard_id = client.create_dashboard(dashboard_cfg["name"], dashboard_cfg.get("description"))

    placements = [
        {
            "card_id": card_ids[item["card"]],
            "row": item["row"],
            "col": item["col"],
            "size_x": item["size_x"],
            "size_y": item["size_y"],
        }
        for item in config["layout"]
    ]
    client.add_cards_to_dashboard(dashboard_id, placements)

    return {"dashboard_id": dashboard_id, "card_ids": card_ids}
