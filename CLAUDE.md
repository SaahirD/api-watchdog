# api-watchdog

## What this project is

A tool that watches external APIs (starting with Stripe) for breaking changes, detects which parts of a connected codebase are affected, generates a fix using Claude, and opens a GitHub pull request with that fix — before the change breaks anything in production.

**Core loop:** detect change → find affected code → generate fix → open PR → human reviews and merges.

## Why this exists

Most tools (Dependabot, Renovate) watch package/dependency versions. Nobody watches *live external API behavior* — the changes that don't come with a version bump, just a changelog entry nobody reads. That's the gap this fills.

## Current phase

**Phases 0-3 are complete. The full loop works end-to-end and has been verified against live data, not mocks.**

- **Phase 0 (validation):** Tested Claude's ability to generate correct fixes for real Stripe API deprecations (handleCardPayment → confirmCardPayment, confirmSetupIntent → confirmCardSetup). Both passed.
- **Phase 1 (GitHub App integration):** Flask webhook receiver (`src/github_app.py`) with HMAC-SHA256 signature verification. Verified live: a real `git push` triggered a GitHub webhook delivery through ngrok to the local Flask app, confirmed via GitHub's own delivery log (`GET /app/hook/deliveries`).
- **Phase 2 (Stripe changelog monitoring):** `src/stripe_monitor.py` fetches and parses `docs.stripe.com/changelog`'s embedded `window.__INITIAL_STATE__` JSON (no scraping/headless browser needed), filters to breaking changes, and searches the repo for affected code usage. Verified against the live changelog (880 entries, 69 breaking in the last year).
- **Phase 3 (fix generation + PR creation):** `src/claude_fixer.py` generates fixes with real Stripe migration guidance in the prompt, validates the result still parses (`compile()` / `node --check`) before accepting it. `src/pr_creator.py` authenticates as the GitHub App installation and opens a real PR. Verified live: opened and closed [PR #1](https://github.com/SaahirD/api-watchdog/pull/1) end-to-end using the real "Removes support for the redirectToCheckout method" changelog entry.

**Next up (not started): give this a real trigger, not a local one.**

Today `main.py` only runs when manually launched — it has no trigger of its
own. The originally-planned next step ("wire the push webhook to trigger
main.py") turns out to be the wrong primary trigger: a push to *this* repo
says nothing about whether *Stripe* changed anything, which is the actual
thing being watched for. The plan instead:

1. **Add a scheduled runner** — a GitHub Actions workflow
   (`.github/workflows/watch.yml`) on a cron schedule (e.g. every 6 hours)
   running `python src/main.py --create-prs`. Runs in the cloud, doesn't
   depend on a laptop being on, secrets move to repo Settings → Secrets.
2. **Demote the webhook** from "primary trigger" to optional — keep it for
   reacting to PR events later (e.g. detecting when a generated PR is
   merged/closed, to update the cache), not for deciding *when* to check
   Stripe.
3. Once the scheduled runner works unattended, the local Flask/ngrok setup
   becomes dev-only tooling for testing the webhook path specifically — not
   part of the always-on loop.

**Before testing with real users** (separate from the above, no rush):
this is currently a single-tenant dev sandbox — the `api-watchdog-dev`
GitHub App is installed only on this repo, its webhook is an ngrok tunnel
that dies when the laptop sleeps, and its private key already leaked once
into a session transcript (see below). Recommended before onboarding
anyone else, even though GitHub Apps can technically be multi-tenant on a
single registration:
- Register a **separate, real GitHub App** for anything beyond this repo —
  isolates a dev-key leak from ever touching a real user's install, and
  keeps dev secrets out of a shared/production `.env`.
- Give it **real hosting** for the webhook endpoint (Fly.io, Render,
  Lambda, etc.) — ngrok isn't viable once uptime matters.
- Think through **Anthropic API cost/billing** once usage scales across
  multiple users' repos instead of one dev sandbox.

## Project structure

```
api-watchdog/
├── README.md
├── CLAUDE.md          <- this file
├── TESTING.md          <- testing/usage notes from Phase 1
├── .env               <- local secrets, not committed
├── .gitignore
├── requirements.txt
├── fixtures/
│   └── legacy_checkout.js  <- test fixture: real deprecated Stripe.js usage
├── src/
│   ├── main.py             <- CLI orchestrator (check → fix → optionally --create-prs)
│   ├── github_app.py       <- Flask webhook receiver (GitHub App events)
│   ├── stripe_monitor.py   <- fetches Stripe changelog, detects affected code
│   ├── claude_fixer.py     <- generates + validates fixes via Claude
│   └── pr_creator.py       <- opens GitHub PRs via the App installation
```

## Tech stack

- Python + Flask (webhook receiver)
- PyGithub 2.10.0 + PyJWT (GitHub App JWT/installation auth, PR creation)
- Anthropic SDK 1.1.0, model `claude-opus-5` (fix generation — wired in and working)
- ngrok for local webhook testing (installed via winget: `Ngrok.Ngrok`)
- No database yet — `.stripe_changes_cache.json` (gitignored) tracks which changes have already produced a matched fix, so they aren't reprocessed every run. Unmatched changes are deliberately NOT cached (see stripe_monitor.py's `process_change` — caching misses permanently would silently suppress detecting the API being adopted later). Postgres would only matter once this runs across multiple repos/machines.

## Environment variables (.env, not committed)

```
GITHUB_APP_ID=4733919
GITHUB_CLIENT_ID=Iv23liYYZuMPfJp8N1cY
GITHUB_PRIVATE_KEY=       <- must be quoted (multi-line PEM) or python-dotenv silently truncates it to one line
GITHUB_WEBHOOK_SECRET=    <- set, enforced via HMAC-SHA256 in github_app.py
ANTHROPIC_API_KEY=        <- set and working
```

GitHub App: `api-watchdog-dev`, installed on `SaahirD/api-watchdog`. Permissions: `contents:write`, `pull_requests:write`, `metadata:read`. Subscribed events: `push`, `pull_request`, `repository`.

**Credential rotation reminder:** the ngrok authtoken, GitHub App private key, and Anthropic API key were all exposed in plaintext in a Claude Code session transcript (2026-08-26). They still work, but should be rotated when convenient — this isn't blocking anything.

## Known limitations (not bugs, just not solved yet)

- **Self-scan false positives:** running the tool against this repo's own source produces a couple of false-positive matches, because `stripe_monitor.py`'s own docstrings/comments quote Stripe symbol names as examples (e.g. `redirectToCheckout`). Not a pattern expected in a real customer repo; not defended against.
- **Syntax gate ≠ semantic correctness:** `claude_fixer.py` validates that a generated fix still parses, but a syntactically valid-yet-wrong fix (e.g. duplicated code) would pass the gate. Human review remains load-bearing by design (see guiding principles below).
- **Symbol matching is regex-based, not AST-based:** works well in practice (tightened to require underscore/dot/camelCase structure to cut noise) but can still miss or mismatch in edge cases a real parser wouldn't.

## Guiding principles for this project

- Keep the MVP narrow: one API (Stripe) before expanding
- Every fix should default to "hold for human review" unless confidence is high — never auto-merge (enforced today: `--create-prs` never merges, just opens the PR)
- Prioritize a working end-to-end loop (even manually triggered) over polishing any single piece
- This is a solo build — avoid scope creep, resist the urge to add features before the core loop works
