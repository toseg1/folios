# Setup

This file grows as the build reaches each step that needs a click-by-click
walkthrough. Right now it covers exactly one thing: the Google Cloud OAuth
setup for `folios auth` (build-plan step 13 / §7, items 1–3). The rest
(clone-to-dashboard, entry format reference, backup/restore) lands in
step 21.

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
