# folios

A local, single-user portfolio tracker. Transactions are entered by
hand — from a phone via a Google Form, or from a terminal — prices and
FX rates are fetched automatically, everything lands in a local
PostgreSQL, and Metabase reads a set of read-only views on top.

Running cost: **€0/month**. Every external service used (Yahoo Finance,
the ECB's daily reference rates via frankfurter.dev, Google Forms/Sheets)
is free.

```
Google Form (phone)  ──▶  Google Sheet  ──┐
                                          │  folios pull
data/manual/*.csv  ───────────────────────┤
                                          ▼
                                  validate → load → Postgres core.*
                                          ▲              │
                  ECB (frankfurter) ──────┤              ▼
                  Yahoo (yfinance) ───────┘         marts.* views
                                                         │
                                                         ▼
                                                     Metabase
```

It's meant to be cloned and made your own: edit `config/`, run a couple
of commands, and you have your own instance with your own Google Form.
Nothing is hardcoded to any one person's portfolio.

## Quickstart

Full walkthrough (including the one-time Google Cloud OAuth setup) is
in [`SETUP.md`](SETUP.md). Short version, for a real setup:

```sh
cp .env.example .env               # fill in real passwords
make up                            # Postgres only
python3.12 -m venv .venv && source .venv/bin/activate
make setup                         # pip install
# edit config/ (copy config/example/ as a starting point)
folios init                        # migrations + seed config/ into the DB
folios add                         # or: folios load data/manual/transactions.csv
folios prices                      # fetch closes for anything held
folios fx --since 2020-01-01       # fetch FX rates
```

Or, to see it working without any of your own data:

```sh
make up
make demo                          # example config + sample transactions
```

Point Metabase (bring your own, or `make metabase-up` for a bundled
one) at the `folios` Postgres database, using the `metabase_ro` role —
it can only ever see the `marts` schema, never `core`.

## Phone entry

Once the Google Cloud OAuth setup in `SETUP.md` is done:

```sh
folios auth          # one-time browser sign-in
folios form-init      # builds the branching entry Form + a response Sheet
```

From there, entering a transaction on your phone reaches the dashboard
after the next `folios sync` (or `folios pull && folios load`) — no
manual steps. `folios form-sync` refreshes the Form's dropdowns after a
`config/` change (a new account, instrument, or currency).

## CLI reference

| Command | What it does |
| --- | --- |
| `folios init` | Apply pending migrations, then seed `config/` into the database |
| `folios add` | Interactively record one transaction from the terminal |
| `folios validate [path]` | Dry-run check of a transactions CSV |
| `folios load [path]` | Load a transactions CSV (refuses to insert anything if any row fails) |
| `folios rebuild` | Reproduce `core.*` from `config/` and `data/manual/` alone |
| `folios fx --since DATE` | Fetch ECB FX rates for every currency used in `config/` |
| `folios prices` | Fetch closing prices for every instrument ever held |
| `folios value [path]` | Load manual valuations (SCPI, *fonds euros*, unlisted holdings) |
| `folios exposure --refresh` | Refresh ETP look-through (country/sector) via justETF |
| `folios status` | Flag things worth a human's attention (stale valuations, missing tickers, unmapped exposure) |
| `folios fix-ticker ID TICKER` | Supply/replace an instrument's Yahoo ticker |
| `folios auth` | One-time Google OAuth (Forms/Sheets/Drive) |
| `folios form-init` | Build the branching entry Form + response Sheet from `config/` |
| `folios form-sync` | Refresh the Form's dropdowns after a `config/` change |
| `folios pull` | Read new Form responses into `data/manual/gform_*.csv` |
| `folios sync` | `pull → fx → load → value → prices → status`, logged to `logs/sync.log` — what `launchd` runs |
| `folios dashboard-init` | Create an example Metabase dashboard from `dashboards/main.yml` |
| `folios doctor` | Check Docker, ports, credentials, network reachability, config, migrations |

## Documentation

- [`SETUP.md`](SETUP.md) — Google Cloud OAuth walkthrough, clone-to-dashboard, entry format reference, symbol-resolution troubleshooting, backup/restore.
- [`PRIVACY.md`](PRIVACY.md) — what leaves the machine, and what never does.
- [`CLAUDE.md`](CLAUDE.md) — repo conventions, for anyone (human or agent) working on the code itself.

## What this is not

Not a broker integration, not multi-user, not a web app, not real-time.
No FIFO cost basis (weighted-average only), no time/money-weighted
return, no per-security look-through (aggregate only), no tax
reporting of any kind. See the build notes for the full list — these
are deliberate cuts, not gaps to fill in later.

## Stack

Python 3.12 · Typer · pydantic · psycopg (PostgreSQL 18) · yfinance ·
frankfurter.dev (ECB rates) · stockdex (justETF look-through) ·
Google Forms/Sheets/Drive APIs · Metabase (bring your own, or the
bundled optional instance) · Docker Compose · `launchd` for scheduling.
