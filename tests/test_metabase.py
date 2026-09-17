import pytest
import requests

from folios import metabase


def test_get_api_key_rejects_the_env_example_placeholder(monkeypatch):
    monkeypatch.setenv("METABASE_API_KEY", "change-me")
    with pytest.raises(metabase.MetabaseConfigError):
        metabase.get_api_key()


def test_get_api_key_rejects_missing_key(monkeypatch):
    monkeypatch.delenv("METABASE_API_KEY", raising=False)
    with pytest.raises(metabase.MetabaseConfigError):
        metabase.get_api_key()


def test_get_api_key_accepts_a_real_value(monkeypatch):
    monkeypatch.setenv("METABASE_API_KEY", "mb_real_key_123")
    assert metabase.get_api_key() == "mb_real_key_123"


class FakeResponse:
    def __init__(self, json_data, status_code=200):
        self._json = json_data
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"status {self.status_code}")

    def json(self):
        return self._json


class FakeMetabaseSession:
    def __init__(self, databases):
        self.databases = databases
        self.calls = []
        self._next_card_id = 1
        self._next_dashboard_id = 1

    def get(self, url, headers=None, timeout=None):
        self.calls.append(("GET", url, headers))
        return FakeResponse({"data": self.databases})

    def post(self, url, json=None, headers=None, timeout=None):
        self.calls.append(("POST", url, json, headers))
        if url.endswith("/api/card"):
            card_id = self._next_card_id
            self._next_card_id += 1
            return FakeResponse({"id": card_id, **json})
        if url.endswith("/api/dashboard"):
            dashboard_id = self._next_dashboard_id
            self._next_dashboard_id += 1
            return FakeResponse({"id": dashboard_id, **json})
        raise AssertionError(f"unexpected POST {url}")

    def put(self, url, json=None, headers=None, timeout=None):
        self.calls.append(("PUT", url, json, headers))
        return FakeResponse({"id": 1, **json})


def _client(databases):
    session = FakeMetabaseSession(databases)
    client = metabase.MetabaseClient("http://localhost:3000", "fake-key", session=session)
    return client, session


def test_find_database_id_matches_by_name():
    client, _ = _client([{"id": 7, "name": "folios"}, {"id": 9, "name": "other"}])
    assert client.find_database_id("folios") == 7


def test_find_database_id_raises_when_missing():
    client, _ = _client([{"id": 9, "name": "other"}])
    try:
        client.find_database_id("folios")
        raise AssertionError("expected MetabaseConfigError")
    except metabase.MetabaseConfigError as exc:
        assert "folios" in str(exc)


def test_create_card_sends_native_query_and_returns_id():
    client, session = _client([])
    card_id = client.create_card(7, "Holdings", "SELECT 1", "table")
    assert card_id == 1

    method, url, body, headers = session.calls[0]
    assert method == "POST"
    assert url.endswith("/api/card")
    assert body["dataset_query"] == {
        "type": "native",
        "native": {"query": "SELECT 1"},
        "database": 7,
    }
    assert headers["x-api-key"] == "fake-key"


def test_add_cards_to_dashboard_uses_negative_temp_ids():
    client, session = _client([])
    client.add_cards_to_dashboard(
        3,
        [
            {"card_id": 1, "row": 0, "col": 0, "size_x": 6, "size_y": 6},
            {"card_id": 2, "row": 0, "col": 6, "size_x": 6, "size_y": 6},
        ],
    )
    method, url, body, _ = session.calls[0]
    assert method == "PUT"
    assert url.endswith("/api/dashboard/3")
    assert [dc["id"] for dc in body["dashcards"]] == [-1, -2]
    assert [dc["card_id"] for dc in body["dashcards"]] == [1, 2]


def test_dashboard_init_creates_every_card_and_places_them():
    client, session = _client([{"id": 7, "name": "folios"}])
    config = metabase.load_dashboard_config()

    result = metabase.dashboard_init(config, client, database_name="folios")

    assert set(result["card_ids"]) == {"Allocation by asset class", "Holdings"}
    assert result["dashboard_id"] == 1

    put_calls = [c for c in session.calls if c[0] == "PUT"]
    assert len(put_calls) == 1
    dashcards = put_calls[0][2]["dashcards"]
    assert len(dashcards) == 2
    assert {dc["card_id"] for dc in dashcards} == set(result["card_ids"].values())


def test_dashboard_init_raises_before_creating_anything_when_database_missing():
    client, session = _client([{"id": 9, "name": "not-folios"}])
    config = metabase.load_dashboard_config()

    try:
        metabase.dashboard_init(config, client, database_name="folios")
        raise AssertionError("expected MetabaseConfigError")
    except metabase.MetabaseConfigError:
        pass

    assert not any(call[0] == "POST" for call in session.calls)


def test_load_dashboard_config_matches_layout_to_defined_cards():
    config = metabase.load_dashboard_config()
    card_names = {c["name"] for c in config["cards"]}
    layout_names = {item["card"] for item in config["layout"]}
    assert layout_names <= card_names
