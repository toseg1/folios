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
