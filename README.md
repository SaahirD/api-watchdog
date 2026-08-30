# api-watchdog

A tool that watches when an API you depend on (starting with Stripe) changes,
finds the affected code in your repo, generates a fix with Claude, and opens
a pull request with that fix — before the change breaks anything in
production. It never merges anything itself; a human reviews and merges.

MIT licensed. See [`ROADMAP.md`](ROADMAP.md) for what's planned next and
[`CLAUDE.md`](CLAUDE.md) for the full build history and design notes.

## Install it on your own repo

Everything runs inside **your own repo's GitHub Actions** on a schedule —
there's no hosted service to sign up for, and no shared credentials to trust.
You register your own (free) GitHub App, scoped only to your own repo, and
bring your own Anthropic API key. See [Why your own GitHub App?](#why-your-own-github-app-not-a-shared-one)
below for why this is the safe way to do it with no central server.

### 1. Fork or clone this repo

Get `src/`, `requirements.txt`, and `.github/workflows/watch.yml` into a repo
you control — either fork this repo, or copy those paths into your own.

### 2. Create your own GitHub App

GitHub → Settings → Developer settings → GitHub Apps → **New GitHub App**.

- **Webhook**: uncheck "Active" — the scheduled workflow doesn't need one.
  (You can add one later if you want the optional PR-lifecycle webhook —
  see `src/github_app.py`.)
- **Permissions**: Repository permissions → `Contents: Read & write`,
  `Pull requests: Read & write`, `Metadata: Read-only`. Nothing else needed.
- **Where can this app be installed?**: "Only on this account" is fine
  unless you plan to use it across multiple repos.

After creating it, note the **App ID**, and generate + download a **private
key** (Settings → General → Private keys → Generate a private key).

### 3. Install the App on your repo

From the App's settings page → Install App → pick your repo.

### 4. Add secrets to your repo

Your repo → Settings → Secrets and variables → Actions → New repository
secret. Add exactly these three:

| Secret name | Value |
|---|---|
| `ANTHROPIC_API_KEY` | Your own [Anthropic API key](https://console.anthropic.com/) |
| `GH_APP_ID` | The App ID from step 2 |
| `GH_APP_PRIVATE_KEY` | The full private key `.pem` file contents from step 2 |

(Secret names can't start with `GITHUB_` — that prefix is reserved by
GitHub Actions — which is why the App credentials are named `GH_*` here even
though the code reads them as `GITHUB_APP_ID`/`GITHUB_PRIVATE_KEY`
internally; `watch.yml` maps between the two.)

### 5. That's it

`.github/workflows/watch.yml` runs on a 12-hour cron once it's on your
default branch, checking the Stripe changelog for breaking changes that
affect your code and opening a PR (never auto-merging) for anything found.

To run it on demand instead of waiting for the cron: **Actions** tab →
**Watch Stripe API Changes** → **Run workflow**, optionally checking
**create_prs** to actually open a PR instead of a dry run.

**Careful with a dry run right before a real run** — checking for changes
writes to a seen-changes cache regardless of whether `create_prs` was
checked, so a dry run immediately before a real run can make the real run
think there's nothing new to act on. If you want to force a completely
fresh check, delete the `.stripe_changes_cache.json` cache entry from your
repo's Actions cache (Actions tab → Caches).

### Why your own GitHub App, not a shared one?

A GitHub App's private key isn't scoped to a single installation — anyone
holding it can mint an access token for *any* repo or org that has
installed that App. Distributing one shared App's key to every user (as an
Actions secret in their own repo) would let any installer act as the App
against every other installer's repo. A real shared/hosted App is possible,
but only safely with the private key kept on a server *we* run, which isn't
built yet (see `ROADMAP.md`). Registering your own App costs nothing and
keeps your key yours.

## Local development (webhook testing only)

The steps below are dev-only tooling for testing `src/github_app.py`'s
*optional* webhook handling specifically. They are **not** required for the
scheduled Stripe-watching loop above, which runs entirely in GitHub Actions.

### Webhook testing with ngrok

`src/github_app.py` runs a Flask server on port 5000 that receives GitHub webhook events. To test against real GitHub webhook deliveries locally, expose your local port to the internet using **ngrok**:

1. **Start the Flask webhook receiver:**
   ```bash
   python src/github_app.py
   ```
   The server will listen on `http://localhost:5000/webhook`

2. **In another terminal, start an ngrok tunnel:**
   ```bash
   ngrok http 5000
   ```
   ngrok will print a forwarding URL like `https://abc123-456-def.ngrok-free.app`

3. **Configure GitHub App webhook:**
   - Use the ngrok URL as your webhook endpoint: `https://abc123-456-def.ngrok-free.app/webhook`
   - Set the events and secret, then save
   - GitHub will begin delivering webhooks to your local machine

**Note:** ngrok free tier tunnels expire after a period of inactivity. For persistent testing, [sign up for an ngrok account](https://ngrok.com), obtain an authtoken, and run:
```bash
ngrok config add-authtoken <your-auth-token>
```
