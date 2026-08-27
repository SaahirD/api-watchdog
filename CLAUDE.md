# api-watchdog

## What this project is

A tool that watches external APIs (starting with Stripe) for breaking changes, detects which parts of a connected codebase are affected, generates a fix using Claude, and opens a GitHub pull request with that fix — before the change breaks anything in production.

**Core loop:** detect change → find affected code → generate fix → open PR → human reviews and merges.

## Why this exists

Most tools (Dependabot, Renovate) watch package/dependency versions. Nobody watches *live external API behavior* — the changes that don't come with a version bump, just a changelog entry nobody reads. That's the gap this fills.

## Current phase

**Phase 1: GitHub App integration (in progress)**
- GitHub App registered, installed on this repo
- Flask webhook receiver built (`src/github_app.py`)
- Currently debugging webhook delivery — GitHub isn't successfully reaching the local ngrok endpoint yet

**Phase 0 (validation) is complete.** Tested Claude's ability to generate correct fixes for real Stripe API deprecations (handleCardPayment → confirmCardPayment, confirmSetupIntent → confirmCardSetup). Both tests passed — Claude correctly identified argument shape changes, left unrelated code untouched, and produced minimal, mergeable diffs.

## Project structure

```
api-watchdog/
├── README.md
├── CLAUDE.md          <- this file
├── .env               <- local secrets, not committed
├── .gitignore
├── requirements.txt
├── src/
│   ├── github_app.py      <- Flask webhook receiver (GitHub App events)
│   ├── stripe_monitor.py  <- (not yet built) watches Stripe changelog for changes
│   └── claude_fixer.py    <- (not yet built) sends change + code to Claude, gets fix
```

## Tech stack

- Python + Flask (webhook receiver)
- PyGithub (GitHub API interactions)
- Anthropic API (Claude) for fix generation — not yet wired in, billing not yet set up
- ngrok for local webhook testing
- Postgres (planned, not yet added) — to track seen changes and avoid duplicate processing

## Environment variables (.env, not committed)

```
GITHUB_APP_ID=
GITHUB_CLIENT_ID=
GITHUB_PRIVATE_KEY=
GITHUB_WEBHOOK_SECRET=   <- placeholder until real webhook URL is set
ANTHROPIC_API_KEY=       <- not yet added, billing pending
```

## What's NOT built yet

- Stripe changelog monitoring (Phase 1, next after webhook works)
- Code matching (finding where in a repo an API change applies)
- Claude fix generation wiring
- PR creation logic
- Any dashboard or UI
- Support for any API beyond Stripe
- Confidence scoring / human-review-hold logic

## Known open issue right now

Webhook isn't being received in the Flask app despite the GitHub App being installed and ngrok running. Debugging steps in progress:
- Checking GitHub App's "Recent Deliveries" log for failed/missing attempts
- Verifying the app is installed on the correct repo
- Verifying "Push" event is subscribed to, not just Installation/Repository events

## Guiding principles for this project

- Keep the MVP narrow: one API (Stripe) before expanding
- Every fix should default to "hold for human review" unless confidence is high — never auto-merge
- Prioritize a working end-to-end loop (even manually triggered) over polishing any single piece
- This is a solo build — avoid scope creep, resist the urge to add features before the core loop works
