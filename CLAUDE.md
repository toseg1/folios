# CLAUDE.md

folios is a local, single-user portfolio tracker: Postgres warehouse,
Metabase dashboard, phone entry via a Google Form, market data from
yfinance/frankfurter.dev/justETF. See `README.md` for what it does,
`SETUP.md` for how to run it, `PRIVACY.md` for what leaves the
machine. `docs/folios-build-plan.md` and
`docs/folios-data-entry-reference.md` are the original design docs —
present locally, gitignored, not guaranteed to exist in every clone.

## Commands

```sh
python3.12 -m venv .venv && source .venv/bin/activate   # 3.12 exactly —
                                                          # pydantic's Rust
                                                          # build doesn't
                                                          # support 3.14 yet
make setup      # pip install; stockdex needs a --no-deps follow-up, see Makefile
make up         # Postgres only, on POSTGRES_PORT from .env
ruff check .
pytest -q       # needs a running disposable Postgres — see below
```

Tests need `FOLIOS_TEST_DATABASE_URL` pointing at a **disposable**
Postgres (never `folios`, the real database) — e.g. a throwaway
`postgres:18` container on its own port:

```sh
docker run -d --name folios-test-pg -p 5434:5432 \
  -e POSTGRES_USER=folios_test -e POSTGRES_PASSWORD=folios_test -e POSTGRES_DB=folios_test \
  postgres:18
FOLIOS_TEST_DATABASE_URL=postgresql://folios_test:folios_test@localhost:5434/folios_test pytest -q
```

CI does exactly this (`.github/workflows/ci.yml`) with a `postgres:18`
service container on the default port. Remove the throwaway container
when done — never point tests at the real `folios` database, and never
leave test infra running that the user didn't ask for.

## Non-negotiable rules

- **Money and quantities are `numeric`, never `float`/`real`.** No
  `EXP(SUM(LN(x)))` either — it's a float in disguise.
- **Entry files are the source of truth.** The database is a
  projection (`folios rebuild` must reproduce `core.*` from `config/`
  + `data/manual/` alone); the Google Sheet is a transport, never a
  source of truth — the loader reads only files.
- **Transactions are immutable facts; positions are derived.** Never
  add a "positions" table — everything downstream is a view.
- **Migrations are forward-only**, applied in filename order, never
  edited after being applied. Add a new one instead.
- **Metabase only ever sees `marts`.** Never grant it access to `core`
  or `staging`.
- **Entered values are always positive**; the loader derives the sign
  from `txn_type` (`models.QUANTITY_SIGN`, `compute_net_amount`).
- **`entry_id` is a stable key**, never a content hash:
  `csv:<relative path>:<line>` for hand-edited/pulled CSV rows. A
  content hash (`txn_hash`) is stored alongside, for change detection
  only — never as the primary key.
- **Dimensions are data, transaction types are code.** `asset_class`,
  `region`, `sector`, etc. are extended by adding a row to
  `config/dimensions.csv`. `txn_type` keeps a CHECK constraint because
  a new type changes how numbers are computed, not merely how they're
  grouped.
- **No source adapter adds columns to `core.transactions`.** That's
  what keeps a future data source (e.g. PDF parsing) an adapter rather
  than a rewrite.
- **Never commit** `data/manual/`, `data/raw/`, `.env`, or
  `.credentials/` — real financial data and credentials.

## Testing conventions

- `tests/conftest.py` provides `clean_test_db` (empty, migrations not
  applied) and `migrated_conn` (migrations applied). Most test files
  add their own `seeded_conn` fixture on top, seeding
  `config/example/` (`tests/test_seed.py`'s `EXAMPLE_CONFIG`) via
  `seed.seed(conn, EXAMPLE_CONFIG)`.
- Never call real external APIs in a test. Every provider is
  dependency-injected and gets a fake in tests: `PriceProvider`
  (yfinance), `FxProvider` (frankfurter), `ExposureProvider`
  (stockdex/justETF), the Google `forms`/`sheets` API clients (fakes
  that apply requests to an in-memory item list the way the real API
  does — see `tests/test_google_forms.py`), the Metabase HTTP client
  (`tests/test_metabase.py`).
- A few live, throwaway smoke tests against real external services
  were run by hand during development (not part of the suite) to
  confirm assumptions about API shapes before building against them —
  see commit messages on the `google/forms.py` and `google/sheets.py`
  steps for what was actually confirmed live (e.g. the Forms API has
  no way to link a response Sheet, or to validate answers).
- Any module writing into `config/` or `data/manual/` in a test must
  monkeypatch the relevant path constant (`seed.CONFIG_DIR`,
  `loader.DEFAULT_DATA_DIR`, `valuations.DEFAULT_VALUATIONS_PATH`,
  `google.forms.FORM_STATE_PATH`, `google.sheets.PULL_STATE_PATH`,
  `google.sheets.MANUAL_DIR`, `sync.LOG_PATH`) to a `tmp_path` — never
  let a test touch the real repo's `config/`/`data/`/`logs/`.
- Watch fixture teardown order when a test monkeypatches a method on a
  fixture object (e.g. neutering `conn.close()`): `psycopg`'s
  `close()` is idempotent, so it's almost never necessary to override
  it — a real bug from doing this anyway leaked a connection and hung
  the suite partway through a run (see the `doctor`-step commit).

## Layout

```
src/folios/
  cli.py              typer app — thin; business logic lives in the modules below
  db.py, models.py, validate.py, loader.py     core entry pipeline
  seed.py             config/ -> database, also reused by the new-instrument path
  fx.py, prices.py, exposure.py, valuations.py, new_instrument.py, status.py
  sync.py             pull -> fx -> load -> value -> prices -> status
  metabase.py, doctor.py
  google/
    auth.py           OAuth installed-app flow
    forms.py          form-init / form-sync
    sheets.py         pull + status write-back
migrations/           001..005, forward-only, applied by db.run_migrations
config/                real (gitignored *.yml/*.csv except example/)
config/example/        the fixture EXAMPLE_CONFIG tests build on, and `make demo` copies
apps_script/            Form-bound instant-validation script (not Sheet-bound — no native linked Sheet exists)
dashboards/main.yml     folios dashboard-init's card definitions
launchd/                com.folios.sync.plist.template, rendered by `make install-launchd`
```

## Explicitly out of scope

Do not build these, and do not partially build them "for later": FIFO
cost basis, time/money-weighted return, per-security look-through,
corporate actions beyond `SPLIT`, accrued interest, PDF parsing, tax
reporting of any kind, multi-user/auth/hosting, a web UI, real-time
prices, broker API integrations.
