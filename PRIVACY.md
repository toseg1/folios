# Privacy

This file grows as the build reaches each step that touches an external
service (see `docs/folios-build-plan.md` step 21 for the full picture,
still to come). What's true today:

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
