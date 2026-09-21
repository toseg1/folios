# The day-to-day workflow

`README.md`/`SETUP.md` cover getting an instance running. This is the
short answer to "I just entered something — now what?"

## In one sentence

Enter data (phone Form, or CSV/`folios add`) → run `folios sync` →
look at Metabase. Nothing you enter on your phone reaches the database
until `folios sync` (or the scheduled `launchd` job, if installed) runs.

## What `folios sync` actually does

```sh
folios sync
```

runs six steps, always in this order, always all six regardless of
whether an earlier one failed — logged to `logs/sync.log`, exits
non-zero if any step reported an error:

1. **pull** — reads new Google Form responses, writes them to
   `data/manual/gform_<date>.csv` (transactions) and
   `data/manual/gform_valuations_<date>.csv` (Valuation-type entries).
   A "+ Not listed (new instrument)" answer registers the instrument
   first (into `config/instruments.csv` + its subtype file +
   `config/aliases.csv`), then completes the very transaction the
   response was carrying, now that the symbol resolves.
2. **fx** — fetches FX rates from frankfurter.dev. Must run before
   *load*, since the loader freezes the day's rate onto each row.
3. **load** — reads every CSV in `data/manual/`, validates it, writes
   to `core.transactions`. Refuses to write anything from a file that
   has any invalid row.
4. **value** — loads `data/manual/valuations.csv` (manual prices —
   SCPI, unlisted funds) into `core.prices`.
5. **prices** — fetches close prices from Yahoo Finance for every
   `price_source=yfinance` instrument ever held. Runs after *load* on
   purpose: it only fetches for instruments you actually hold now.
6. **status** — flags stale valuations and similar issues. Informational
   only — never fails the run by itself.

## Phone entry, specifically

- Submitting a transaction just queues a Form response. It does nothing
  until the next `folios sync`.
- Submitting "+ Not listed (new instrument)" both registers the
  instrument **and** completes the transaction you were entering, in
  the same `folios sync` run — you don't need to submit it twice.
- The new instrument (and its subtype row — Fund/Bond/Crypto details,
  if you filled any in) lands directly in `config/`, no manual editing
  needed for the common case.
- Run `folios form-sync` afterward so the new instrument's alias shows
  up as a Symbol choice for *future* submissions. It has no effect on
  anything already submitted.

## When you edit `config/` by hand

(new account, a new alias, a new dimension code, correcting a value)

- `folios init` reconciles the change into the database — needed for
  everything downstream of the database (loader, valuations, Metabase),
  and still worth running for its migration check.
- `folios form-sync` refreshes the *live* Form's `Account`/`Symbol`/
  `Currency` dropdowns to match. It reconciles config/ into the
  database itself first, so a hand-edited account/alias/currency shows
  up on the Form even without a prior `folios init`. `Asset class`/
  `Region`/`Sector`/etc. aren't refreshed by `form-sync` (a
  pre-existing gap, not something recent); a genuinely new dimension
  code there needs a fresh `folios form-init` to actually appear as a
  Form choice.

## The last page of an entry

The Form's "Next" button on the last page of any entry (Trade, Cash,
Cost, ...) actually submits — Google Forms always labels
branch-dependent navigation "Next", even when the answer you picked
resolves to submitting the form. The screen you land on right after is
the confirmation page, not a second required step; there's no separate
"Submit" tap. (This is a Google Forms client quirk, not a folios bug —
the API's page-break items carry no navigation field to override it.)

## Checking it worked

- `data/manual/gform_<date>.csv` — what was actually written from the
  phone, in the same format you'd hand-type.
- `core.transactions` / the `marts.*` views (or Metabase) — what's
  live in the database.
- The Form's response Sheet, `status`/`message` columns — per-submission
  pull results (`ok`, or the exact validation error).
- `folios doctor` — overall instance health (Postgres, Google auth,
  external APIs, config, migrations).

## Something not resolving?

See SETUP.md's "When a symbol won't resolve" — same checklist, not
repeated here.
