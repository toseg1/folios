# Setup

Everything needed to go from a fresh clone to a working instance:
clone-to-dashboard, the Google Cloud OAuth walkthrough (for phone
entry), the entry format reference, what to do when a symbol won't
resolve, and Postgres backup/restore.

## Clone to dashboard

1. **Prerequisites**: Docker Desktop, Python 3.12 (not 3.14 — pydantic's
   Rust build doesn't support it yet), `git`.

2. **Clone and configure:**

   ```sh
   git clone <your fork's URL> folios && cd folios
   cp .env.example .env
   ```

   Edit `.env` and replace every `change-me` with a real value —
   these are only used locally, by your own Postgres container.

3. **Bring up Postgres** (the `folios` warehouse only — no Metabase in
   this stack; see step 5):

   ```sh
   make up
   ```

4. **Install and initialise:**

   ```sh
   python3.12 -m venv .venv && source .venv/bin/activate
   make setup
   folios init          # applies migrations, seeds config/ into the DB
   ```

   Fresh clone, no config of your own yet? `folios init` will report
   `config/accounts.yml not found` — either write your own `config/`
   (see the entry format reference below and `config/example/` as a
   template), or run `make demo` instead, which copies the example
   config and some sample transactions in for you.

5. **Point Metabase at it.** Bring your own existing Metabase, or run
   the optional bundled one (`make metabase-up`, on `:3000` by
   default). Either way, add a **PostgreSQL** database connection in
   Metabase's admin settings:
   - Host `localhost` (or `host.docker.internal` if Metabase itself
     runs in Docker), port from `POSTGRES_PORT` in `.env`
   - Database `folios`
   - Username `metabase_ro`, password from `METABASE_RO_PASSWORD`

   `metabase_ro` can only ever see the `marts` schema — never `core`.
   Name the connection **folios** if you plan to use
   `folios dashboard-init` later (it looks the connection up by name).

6. **Enter some transactions and look at the data:**

   ```sh
   folios add                                  # interactively, one at a time
   # or: folios load data/manual/transactions.csv
   folios fx --since 2020-01-01                # or whenever your history starts
   folios prices
   ```

   Build a dashboard directly in Metabase against the `marts.*` views,
   or run `folios dashboard-init` for a minimal example (two cards) to
   confirm the Metabase API connection works.

7. **`folios doctor`** at any point to check what's missing — it names
   the fix for each problem it finds.

## Phone entry (optional)

The steps below (Google Cloud OAuth) are only needed if you want to
enter transactions from your phone via a Google Form. CSV/`folios add`
entry works with none of this.

## Google Cloud OAuth setup

This is free — no billing account, no credit card, no Google app
verification. See the project's own privacy notes in `PRIVACY.md`.

### 1. Create a Google Cloud project

1. Go to [console.cloud.google.com](https://console.cloud.google.com) and
   sign in with the Google account you want folios to use (the one whose
   Forms/Sheets/Drive it will manage).
2. Click the project dropdown at the top of the page, then **New Project**.
3. Give it any name (e.g. `folios`). Leave the organization as-is.
   Click **Create**.
4. Wait a few seconds for it to finish, then make sure it's the
   **selected** project (check the project dropdown again).

### 2. Enable the three APIs

For each of the following, use the search bar at the top of the console
to find it, open its page, and click **Enable**:

1. **Google Forms API**
2. **Google Sheets API**
3. **Google Drive API**

(Direct links, once you're in the right project: search "Forms API",
"Sheets API", "Drive API" in the console's top search bar — each takes
you straight to an Enable button.)

### 3. Configure the OAuth consent screen

1. In the left sidebar: **APIs & Services → OAuth consent screen**.
2. User type: **External** (this is fine for personal use — it does not
   mean "published publicly", see step 4).
3. Fill in the required fields: app name (e.g. `folios`), your email as
   the user support email, your email again as the developer contact.
   Everything else can be left blank.
4. Save through the Scopes and Test users screens for now — you'll add a
   test user in the next step.
5. **Leave the app in "Testing" publishing status.** Do not click
   "Publish app". Testing mode is what makes this free and skips
   Google's verification review entirely — the tradeoff is that only
   explicitly added test users can log in, which is exactly what you want
   for a personal tool.

### 4. Add yourself as a test user

1. Still on the OAuth consent screen, find the **Test users** section
   (under **Audience** in newer console layouts).
2. Click **Add users**, enter the same Google account email you used in
   step 1, save.

### 5. Create the OAuth 2.0 Client ID

1. Left sidebar: **APIs & Services → Credentials**.
2. **Create Credentials → OAuth client ID**.
3. Application type: **Desktop app** (not "Web application" — this
   matters, it changes the flow folios uses).
4. Name it anything (e.g. `folios-desktop`). Click **Create**.
5. A dialog shows your client ID and secret. Click **Download JSON**.

### 6. Install the credential file

Move the downloaded file into the repo, named exactly:

```
.credentials/client_secret.json
```

This directory is gitignored — it never gets committed, never leaves
your machine.

```sh
mkdir -p .credentials
mv ~/Downloads/client_secret_*.json .credentials/client_secret.json
```

### 7. Run `folios auth`

```sh
folios auth
```

This opens your browser once, to a Google sign-in and consent screen.
Sign in with the same account from steps 1 and 4, and approve the three
requested permissions (Forms, Sheets, and "See, edit, create, and delete
only the specific Google Drive files you use with this app" — that last
one is the `drive.file` scope, deliberately narrow).

You'll see a "received verification code" or similar confirmation in the
browser, and the terminal will print:

```
Authenticated. Token stored at .../.credentials/token.json
```

That token file is also gitignored. From here on, every folios command
that talks to Google reuses it silently — you won't be prompted again
unless you delete `.credentials/token.json` or revoke access from your
Google account's [connected apps](https://myaccount.google.com/permissions).

### Troubleshooting

- **"Google hasn't verified this app" warning during sign-in** — expected
  in Testing mode. Click **Advanced → Go to folios (unsafe)**. This
  warning exists for apps requesting broad access from the public; since
  you added yourself as the only test user and the scope is limited to
  `drive.file`, this is safe for your own app.
- **"Error 403: access_denied"** — you're signing in with an account that
  isn't listed as a test user (step 4), or the app is still missing a
  required consent-screen field (step 3).
- **`client_secret.json not found`** — step 6 wasn't done, or the file
  isn't named exactly `client_secret.json`.

## After `folios auth`: building and installing the Form

```sh
folios form-init
```

This creates the branching entry Form and a folios-managed response
Sheet in your Drive, and prints the Form's URL. Two things left to do
by hand (build-plan §7, items 5–6 — Claude Code cannot click a
browser):

1. **Open the Form's own settings and restrict responses to your
   account.** By default a Google Form is answerable by anyone with
   the link — this app assumes single-user.
2. **Install `apps_script/onFormSubmit.gs`.** From the Form's editor:
   kebab menu (⋮) → **Script editor** → paste the file in as
   `Code.gs` → **Triggers** (clock icon) → **Add trigger** → function
   `onFormSubmit`, event source **From form**, event type **On form
   submit** → **Save**, and authorise when prompted. This is what
   emails you within seconds if a `BUY`/`SELL` entry's `quantity ×
   price` doesn't match the `gross` you typed — see `PRIVACY.md` for
   exactly what it sends and to whom.

After that, editing `config/` (a new account, instrument, or currency)
and running `folios form-sync` keeps the Form's dropdowns current.

Bookmark the Form on your phone's home screen and you're done —
`folios sync` (scheduled via `launchd`, see `launchd/`) picks up new
responses automatically. `make install-launchd` renders the plist for
you; `launchctl load ~/Library/LaunchAgents/com.folios.sync.plist`
is the one command left to run yourself.

## Entry format reference

Whether typed by hand into `data/manual/transactions.csv` or produced
by `folios add`/`folios pull`, every transaction is one row:

```
date,account,type,symbol,quantity,price,gross,fee,tax,currency,note
2026-03-14,IBKR-TAXABLE,BUY,AAPL,10,182.50,1825.00,1.20,0,USD,
2026-03-20,IBKR-TAXABLE,DIVIDEND,AAPL,,,9.60,0,1.44,USD,Q1
2026-04-02,BANK-EUR,DEPOSIT,,,,2000.00,0,0,EUR,monthly transfer
```

- **Positive numbers only** — the loader derives the sign from `type`.
- Leave `gross` blank on a trade and the loader computes
  `quantity × price`; supply it and the loader checks the two agree
  (within 1 cent — this is what catches a mistyped decimal point).
- Blank cells are fine where the type doesn't need them.

`type` is one of: `BUY`, `SELL`, `DIVIDEND`, `INTEREST`, `STAKING`,
`FEE`, `TAX`, `DEPOSIT`, `WITHDRAWAL`, `TRANSFER_IN`, `TRANSFER_OUT`,
`SPLIT`, `OPENING_BALANCE`.

Run `folios validate data/manual/transactions.csv` any time — it's a
dry run, line-numbered, and never writes anything.

### When a symbol won't resolve

`Symbol resolves via instrument_aliases` is the single most common
validation error. It means the value in the `symbol` column doesn't
match any row in `config/aliases.csv`. Fixes, in order of likelihood:

1. **Typo** — `folios validate`/`folios add` suggest near matches from
   your existing aliases; check the suggestion.
2. **New instrument, not yet in config** — add a row to
   `config/instruments.csv` (and, if it's a fund/bond/crypto, the
   matching `config/instruments_*.csv` subtype file) plus an alias in
   `config/aliases.csv`, then `folios init` to seed it. Submitting
   "+ Not listed (new instrument)" from the phone Form does this step
   for you automatically (build-plan step 16), including validating
   the Yahoo ticker first.
3. **Symbol exists but under a different alias** — aliases are
   many-to-one (`config/aliases.csv` can map several spellings to the
   same `instrument_id`); add the missing spelling as a new alias row
   rather than renaming anything.

## Backup and restore

The entire database lives in one named Docker volume, `folios_pgdata`.

**Backup** (a plain SQL dump, portable across Postgres versions):

```sh
docker exec folios-postgres pg_dump -U folios_loader folios > folios-backup.sql
```

**Restore**, into a fresh volume:

```sh
make down
docker volume rm folios_pgdata
make up
sleep 5   # wait for the health check
cat folios-backup.sql | docker exec -i folios-postgres psql -U folios_loader folios
```

Remember: per the project's own design, the database is a
**projection** — `folios rebuild` reproduces `core.*` from `config/`
and `data/manual/` alone (everything except FX rates and prices, which
come from external APIs, not files). A files-only backup
(`config/`, `data/manual/`) plus `folios rebuild && folios fx --since
... && folios prices` gets you back to an equivalent state without a
`pg_dump` at all — the SQL dump above is the faster path, not the only one.
