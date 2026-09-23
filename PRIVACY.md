# Privacy

**In one paragraph:** ticker symbols go to Yahoo Finance; currency
codes go to the ECB (via frankfurter.dev); entered rows sit in your
own Google Sheet until `folios pull` collects them; positions, values
and returns never leave your machine. The database port is bound to
`127.0.0.1` only — nothing on your network, and certainly not your
phone, ever connects to it directly. Everything below is the detail
behind that sentence, service by service.

## Yahoo Finance (`yfinance`)

`folios prices` sends the Yahoo ticker symbols from `config/*.csv` to
Yahoo Finance to fetch closing prices — nothing else about your
portfolio (no quantities, no account names, no money amounts) is part
of that request.

## The ECB, via frankfurter.dev (`folios fx`)

`folios fx` sends only the currency codes used in `config/` (e.g.
`USD`, `GBP`) to fetch daily EUR reference rates — no portfolio data
of any kind.

## justETF, via `stockdex` (`folios exposure --refresh`)

Sends only a held exchange-traded fund's ISIN, to fetch its public
country/sector breakdown from justETF's own page. Non-fatal on failure:
the last stored snapshot stays in place, nothing else in `folios sync` is
affected.

## Google (Forms, Sheets, Drive)

folios authenticates to your own Google account via OAuth, requesting
exactly three scopes: `forms.body`, `spreadsheets`, and `drive.file`.

**`drive.file` only grants access to files this app itself creates.**
folios can never see, list, or read any other file in your Drive — not
your other Sheets, not your other Forms, nothing you didn't create
through folios itself. This is a deliberate, minimal scope choice, not
the default `drive` scope that would grant full account access.

The refresh token from this flow is stored locally, in `.credentials/`
(gitignored — never committed, never leaves your machine).

## Apps Script (`apps_script/onFormSubmit.gs`)

This one script runs inside Google's own infrastructure, not on your
machine — it's installed by hand into the Form itself (see `SETUP.md`)
and authorised separately from folios' own OAuth token above, under
your Google account's own Apps Script permissions. It only ever calls
`MailApp.sendEmail`, and only to your own address (whatever account you
installed it under) — never to anyone else, never any other Google
service. Its one job: emailing you immediately when a `BUY`/`SELL`
entry's `quantity × price` doesn't match the `gross` you typed.

## Everything else stays on your machine

Postgres (`docker/compose.yml`) binds its port to `127.0.0.1` only —
not `0.0.0.0` — so nothing else on your network can reach it, and the
phone entering a transaction via the Form never talks to the database
directly; it only ever reaches Google's servers. Metabase is local
infrastructure too (yours, or the optional bundled one in
`metabase/compose.yml`) — a separate Docker Compose project on
purpose, so it's never a dependency of the data pipeline, and nothing
about your positions, values or returns is sent to any service beyond
the three named above.
