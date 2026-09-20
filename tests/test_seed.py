from pathlib import Path

import pytest

from folios.seed import (
    SeedValidationError,
    check_subtype_cross_references,
    seed,
    validate_dimension_values,
)

EXAMPLE_CONFIG = Path(__file__).resolve().parents[1] / "config" / "example"


def test_seed_example_config(migrated_conn):
    result = seed(migrated_conn, EXAMPLE_CONFIG)

    assert result.counts["accounts"] == 2
    assert result.counts["instruments"] == 6
    assert result.warnings == []

    with migrated_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM core.accounts")
        assert cur.fetchone()[0] == 2
        cur.execute("SELECT count(*) FROM core.instruments")
        assert cur.fetchone()[0] == 6
        cur.execute("SELECT count(*) FROM core.dimensions")
        assert cur.fetchone()[0] > 0


def test_seed_is_idempotent_and_updates_in_place(migrated_conn):
    seed(migrated_conn, EXAMPLE_CONFIG)

    with migrated_conn.cursor() as cur:
        cur.execute(
            "SELECT name FROM core.instruments WHERE instrument_id = 'DEMO-SHARE'"
        )
        assert cur.fetchone()[0] == "Demo Corp"

    # Re-seeding with a changed name updates the row rather than duplicating it.
    instruments_csv = EXAMPLE_CONFIG / "instruments.csv"
    original = instruments_csv.read_text()
    edited = original.replace("Demo Corp", "Demo Corp Renamed")
    try:
        instruments_csv.write_text(edited)
        result = seed(migrated_conn, EXAMPLE_CONFIG)
    finally:
        instruments_csv.write_text(original)

    assert result.counts["instruments"] == 6
    with migrated_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM core.instruments")
        assert cur.fetchone()[0] == 6
        cur.execute(
            "SELECT name FROM core.instruments WHERE instrument_id = 'DEMO-SHARE'"
        )
        assert cur.fetchone()[0] == "Demo Corp Renamed"


def test_new_dimension_row_usable_immediately(migrated_conn, tmp_path):
    # Adding a new fiscal_envelope code and using it in the same run works
    # with no migration, per the build plan's own "Done when".
    _write_config_with_new_envelope(tmp_path)

    result = seed(migrated_conn, tmp_path)
    assert result.warnings == []

    with migrated_conn.cursor() as cur:
        cur.execute(
            "SELECT fiscal_envelope FROM core.accounts WHERE account_id = 'NEW-ACC'"
        )
        assert cur.fetchone()[0] == "PEA_JEUNE"


def test_unknown_dimension_code_fails_naming_it(migrated_conn):
    accounts = [{"account_id": "BAD-ACC", "account_type": "securities",
                 "fiscal_envelope": "NOT_A_REAL_CODE", "custody_type": None}]
    dimensions = [{"dimension": "account_type", "code": "securities",
                   "label_en": "x", "sort_order": 0}]
    errors = validate_dimension_values(dimensions, accounts, [])
    assert len(errors) == 1
    assert "BAD-ACC" in errors[0]
    assert "NOT_A_REAL_CODE" in errors[0]
    assert "fiscal_envelope" in errors[0]


def test_seed_raises_and_writes_nothing_on_unknown_code(migrated_conn, tmp_path):
    _write_config_with_new_envelope(tmp_path, valid=False)

    with pytest.raises(SeedValidationError) as exc_info:
        seed(migrated_conn, tmp_path)
    assert "PEA_JEUNE" in str(exc_info.value)

    with migrated_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM core.accounts")
        assert cur.fetchone()[0] == 0


def test_subtype_cross_reference_warnings():
    instruments = [
        {"instrument_id": "MISSING-FUND-ROW", "asset_class": "FUND"},
        {"instrument_id": "OK-BOND", "asset_class": "BOND"},
    ]
    bond_rows = [{"instrument_id": "OK-BOND"}]
    orphan_bond_rows = [{"instrument_id": "OK-BOND"}, {"instrument_id": "GHOST"}]

    warnings = check_subtype_cross_references(instruments, [], bond_rows, [])
    assert any("MISSING-FUND-ROW" in w for w in warnings)

    warnings = check_subtype_cross_references(instruments, [], orphan_bond_rows, [])
    assert any("GHOST" in w for w in warnings)


def _write_config_with_new_envelope(config_dir: Path, valid: bool = True) -> None:
    (config_dir / "dimensions.csv").write_text(
        "dimension,code,label_en,sort_order\n"
        "account_type,securities,x,0\n"
        + ("fiscal_envelope,PEA_JEUNE,Youth equity plan,0\n" if valid else "")
    )
    (config_dir / "accounts.yml").write_text(
        "- account_id: NEW-ACC\n"
        "  broker: Demo Broker\n"
        "  account_type: securities\n"
        "  fiscal_envelope: PEA_JEUNE\n"
        "  base_currency: EUR\n"
    )
    (config_dir / "instruments.csv").write_text(
        "instrument_id,name,asset_class,currency\n"
    )
