# api-watchdog

## What this project is

A tool that watches external APIs (starting with Stripe) for breaking changes, detects which parts of a connected codebase are affected, generates a fix using Claude, and opens a GitHub pull request with that fix — before the change breaks anything in production.

**Core loop:** detect change → find affected code → generate fix → open PR → human reviews and merges.

## Why this exists

Most tools (Dependabot, Renovate) watch package/dependency versions. Nobody watches *live external API behavior* — the changes that don't come with a version bump, just a changelog entry nobody reads. That's the gap this fills.

## Current phase

**Phases 0-4 are complete and verified live. Phase 5 (OpenAPI spec-diff detector) is implemented and control-flow-tested offline; not yet exercised on a real Stripe spec change in production (see Phase 5 entry below for exactly what has and hasn't been verified).**

- **Phase 0 (validation):** Tested Claude's ability to generate correct fixes for real Stripe API deprecations (handleCardPayment → confirmCardPayment, confirmSetupIntent → confirmCardSetup). Both passed.
- **Phase 1 (GitHub App integration):** Flask webhook receiver (`src/github_app.py`) with HMAC-SHA256 signature verification. Verified live: a real `git push` triggered a GitHub webhook delivery through ngrok to the local Flask app, confirmed via GitHub's own delivery log (`GET /app/hook/deliveries`).
- **Phase 2 (Stripe changelog monitoring):** `src/stripe_monitor.py` fetches and parses `docs.stripe.com/changelog`'s embedded `window.__INITIAL_STATE__` JSON (no scraping/headless browser needed), filters to breaking changes, and searches the repo for affected code usage. Verified against the live changelog (880 entries, 69 breaking in the last year).
- **Phase 3 (fix generation + PR creation):** `src/claude_fixer.py` generates fixes with real Stripe migration guidance in the prompt, validates the result still parses (`compile()` / `node --check`) before accepting it. `src/pr_creator.py` authenticates as the GitHub App installation and opens a real PR. Verified live: opened and closed [PR #1](https://github.com/SaahirD/api-watchdog/pull/1) end-to-end using the real "Removes support for the redirectToCheckout method" changelog entry.

- **Phase 4 (scheduled trigger):** `.github/workflows/watch.yml` runs
  `python src/main.py --create-prs` on a cron schedule (every 12 hours),
  in GitHub Actions — no laptop required to be on. Secrets
  (`ANTHROPIC_API_KEY`, `GH_APP_ID`, `GH_APP_PRIVATE_KEY`) live in repo
  Settings → Secrets → Actions, not just the local `.env`. A
  `.stripe_changes_cache.json` (see "no database yet" below) is kept warm
  across ephemeral runners via `actions/cache`, keyed per-run with a
  prefix `restore-keys` fallback so each run restores the previous run's
  cache and saves a fresh one. `workflow_dispatch` (manual trigger, with a
  `create_prs` checkbox) is available for on-demand runs without waiting
  for the cron. Verified live: a `workflow_dispatch` run with `create_prs`
  checked opened [PR #2](https://github.com/SaahirD/api-watchdog/pull/2)
  end-to-end, unattended, using the real "Removes support for specifying
  payment method types in Payment Intents and Setup Intents" changelog
  entry (2026-08-30).

  The webhook (`src/github_app.py`) needed no code change to be
  "demoted" — it was already fully decoupled from `main.py`'s
  `--create-prs` path (never imported, never invoked by it). Its role
  stays what CLAUDE.md's original plan said: optional, for reacting to PR
  events later (e.g. detecting when a generated PR is merged/closed), not
  for deciding *when* to check Stripe. The local Flask/ngrok setup
  (see README.md) is now purely dev-only tooling for testing the webhook
  path specifically — not part of the always-on loop, which runs entirely
  in Actions.

- **Phase 5 (OpenAPI spec-diff detector, implemented — real end-to-end
  matching confirmed, real PR not yet opened from it):** `src/stripe_spec_monitor.py`
  is a second, independent detector alongside `stripe_monitor.py`'s
  changelog scraper. It diffs `github.com/stripe/openapi`'s `spec3.sdk.json`
  against a checkpoint commit (not a Stripe dated API version — this repo's
  own git tags are unrelated sequential build numbers, cut multiple times a
  day) using [`oasdiff`](https://github.com/oasdiff/oasdiff), classifying
  ~500 structural change types as breaking/warning. Symbol candidates come
  from backtick-quoted identifiers in oasdiff's own diff text plus Stripe's
  internal `x-stripeOperations` codegen annotations (undocumented, not a
  stable contract — treat as heuristic). Wired into `main.py`'s
  `check_for_api_changes()` alongside the changelog detector, with a
  single-run dedup (changelog "claims" `(file, line)` pairs first; spec-diff
  matches at an already-claimed line are dropped as the same real change
  independently detected twice). `watch.yml` installs `oasdiff` (pinned
  version) and passes it a `GITHUB_TOKEN` for the (light) GitHub API calls
  it makes.

  **This is a precision/coverage improvement over the changelog, not a
  speed one** — no evidence `stripe/openapi` updates ahead of Stripe's own
  changelog; its value is an independent detection path that catches
  changes the changelog's prose doesn't cleanly name a symbol for (~22% of
  entries, per `stripe_monitor.py`'s own comment). Correcting `ROADMAP.md`'s
  earlier "be faster" framing to reflect this.

  **What's actually verified vs. not**: `_normalize_oasdiff_entry()` and
  `_symbols_for_entry()` were checked against a real `oasdiff v1.29.1`
  binary run (Windows release, downloaded directly) against both a
  synthetic before/after spec pair and Stripe's real, live `spec3.sdk.json`
  — confirmed the actual JSON output shape rather than assuming it, and
  confirmed `x-stripeOperations` symbol enrichment produces correct output
  (e.g. a removed `/v1/payment_intents/{intent}/capture` endpoint correctly
  yields `PaymentIntent.capture` as a candidate). The full
  `StripeSpecDetector.get_pending_changes()` control flow — cold start,
  normal matching (against `fixtures/legacy_checkout.js`, a real match),
  unchanged-checkpoint short-circuit, and the cap/backpressure logic that
  holds the checkpoint back when a run's matches exceed `MAX_SPEC_CHANGES_PER_RUN`
  — was exercised offline with the network calls monkeypatched, all passing.
  A real dry run of `main.py --repo . --verbose` also confirmed the cold-start
  path against the live `stripe/openapi` repo (recorded a real checkpoint,
  emitted no changes, exactly as designed). **Not yet done**: a real run
  where the spec-diff detector's checkpoint has actually advanced and
  produced a real matched change end-to-end through PR creation — that
  requires either waiting for a real subsequent Stripe spec change, or
  forcing an old checkpoint deliberately (see Known Limitations' testing
  note below).

  **Not built** (deliberately, per this project's scope discipline): no
  cross-run reconciliation between the two detectors (documented residual
  risk below), no AST/SDK-verified symbol resolution, no LLM
  re-classification of oasdiff's own breaking/warning output.

**Distribution model (decided 2026-08-30): open source now, monetize later,
no central server yet.** Each user registers their own GitHub App scoped to
their own repo and runs `.github/workflows/watch.yml` there with their own
`ANTHROPIC_API_KEY` — see README.md's install guide. This isn't a
placeholder for a future hosted product; it's the deliberate near-term
architecture, because a *shared* GitHub App's private key can't safely be
handed out as a per-user Actions secret (it can mint an access token for
*any* installation of that App, not just the holder's own — see README's
"Why your own GitHub App?"). A real shared/hosted App becomes worth building
only once there's a server holding that key privately — i.e. once the
monetization phase in `ROADMAP.md` actually starts, not before.

This repo's own `api-watchdog-dev` GitHub App stays what it's always been —
this project's own dev/test installation, installed only on this repo, not
something end users install. Its webhook is an ngrok tunnel that dies when
the laptop sleeps, which is fine, since it's dev-only.

**Credential rotation still applies regardless of the above** — see below.

## Project structure

```
api-watchdog/
├── README.md          <- install guide (register your own App, add secrets, done)
├── CLAUDE.md          <- this file
├── ROADMAP.md          <- what's next and why (OpenAPI-diff idea, monetization, later)
├── LICENSE             <- MIT
├── TESTING.md          <- testing/usage notes from Phase 1
├── .env               <- local secrets, not committed
├── .gitignore
├── requirements.txt
├── .github/
│   └── workflows/watch.yml <- scheduled + manual trigger (see Phase 4 above)
├── fixtures/
│   └── legacy_checkout.js  <- test fixture: real deprecated Stripe.js usage
├── src/
│   ├── main.py             <- CLI orchestrator (check → fix → optionally --create-prs)
│   ├── github_app.py       <- Flask webhook receiver (GitHub App events)
│   ├── stripe_monitor.py   <- fetches Stripe changelog, detects affected code
│   ├── stripe_spec_monitor.py <- diffs Stripe's OpenAPI spec via oasdiff (Phase 5)
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

In GitHub Actions (repo Settings → Secrets → Actions), `GITHUB_APP_ID` and
`GITHUB_PRIVATE_KEY` are stored as `GH_APP_ID` / `GH_APP_PRIVATE_KEY`
instead — GitHub reserves the `GITHUB_` prefix for secret names and
refuses to create one that starts with it. `watch.yml` maps them back to
the `GITHUB_APP_ID`/`GITHUB_PRIVATE_KEY` env var names the code actually
reads. `GITHUB_WEBHOOK_SECRET`/`GITHUB_CLIENT_ID` aren't needed as Actions
secrets — only the local Flask webhook receiver uses them.

`watch.yml` also wires `GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}` into the
run step — this is Actions' own auto-created per-run token (not something
to add in repo Settings), used by `stripe_spec_monitor.py` to authenticate
its light calls to `api.github.com/repos/stripe/openapi` at a higher rate
limit. It is **not** present in a step's environment automatically; a
workflow has to explicitly pass it via `env:`, which is why this line
exists at all.

GitHub App: `api-watchdog-dev`, installed on `SaahirD/api-watchdog`. Permissions: `contents:write`, `pull_requests:write`, `metadata:read`. Subscribed events: `push`, `pull_request`, `repository`.

**Credential rotation reminder:** the ngrok authtoken, GitHub App private key, and Anthropic API key were all exposed in plaintext in a Claude Code session transcript (2026-08-26). They still work, but should be rotated when convenient — this isn't blocking anything.

## Known limitations (not bugs, just not solved yet)

- **Self-scan false positives:** running the tool against this repo's own source produces a couple of false-positive matches, because `stripe_monitor.py`'s own docstrings/comments quote Stripe symbol names as examples (e.g. `redirectToCheckout`). Not a pattern expected in a real customer repo; not defended against. Since the scheduled workflow's default `--repo .` scans this same repo, this can occasionally surface as a non-zero exit on `watch.yml` (no usable fix generated for a false-positive match) — expected, not an incident.
- **Syntax gate ≠ semantic correctness:** `claude_fixer.py` validates that a generated fix still parses, but a syntactically valid-yet-wrong fix (e.g. duplicated code) would pass the gate. Human review remains load-bearing by design (see guiding principles below).
- **Symbol matching is regex-based, not AST-based:** works well in practice (tightened to require underscore/dot/camelCase structure to cut noise) but can still miss or mismatch in edge cases a real parser wouldn't.
- **A dry run is not read-only w.r.t. the seen-changes cache:** `stripe_monitor.py`'s `get_pending_changes()` calls `save_seen_changes()` unconditionally, before `main.py` ever checks `--create-prs`. So running without `--create-prs` still marks any matched change as "seen" — a dry run followed by a real `--create-prs` run against the same restored cache will find nothing pending and silently skip PR creation, even though no PR was ever opened. Bitten by this once verifying Phase 4 (2026-08-30): a `workflow_dispatch` dry run before the real `create_prs` run poisoned the Actions cache, so the "real" run found nothing to do. Fixed by bumping the cache key (`stripe-changes-cache-v2-` in `watch.yml`) to force a fresh cache, and going forward: don't chain a dry run immediately before a real run against the same cache — either test with a change ID not yet cached, or accept that a preceding dry run consumes it.
- **Spec-diff's symbols are lower-precision than the changelog's:** `stripe_spec_monitor.py`'s candidates come from oasdiff's diff text plus the undocumented `x-stripeOperations` codegen annotation, not from Stripe's own hand-curated `changed` list. Expect noisier/less complete matches than the changelog detector, same accepted tradeoff as the existing regex-based matcher's own limitation above — not something being fixed now.
- **Spec-diff can't re-surface an unmatched change later, unlike the changelog:** `stripe_monitor.py`'s changelog detector deliberately doesn't cache unmatched changes so a symbol adopted later still gets caught (see its `process_change` docstring). `stripe_spec_monitor.py` can't do this the same way — it's checkpoint-based (diffs "since last processed commit"), not a rolling time window, so once the checkpoint advances past a diff window, an unmatched change from that window is gone from every future diff, even if the repo starts using that symbol tomorrow. Would need a separate replay buffer to fix; out of scope for Phase 5.
- **Two detectors can still double-PR across runs:** `main.py`'s `check_for_api_changes()` dedups the changelog and spec-diff detectors' matches within a single run (by `(file, line)`), but if they independently detect the *same* real Stripe change in *different* runs, two PRs on two branches can still result — there's no cross-run reconciliation. Accepted per this project's scope discipline; revisit only if it turns out to happen often in practice.
- **Testing `stripe_spec_monitor.py` against a real (not synthetic) spec change requires manually forcing an old checkpoint** — pass a throwaway `cache_file` to `StripeSpecDetector(cache_file=...)` with a `checkpoint_sha` seeded to an old `stripe/openapi` commit (found via its GitHub API commit history), the same way `.stripe_changes_cache.json`'s poisoning bug above should never be reproduced against the real cache file.

## Guiding principles for this project

- Keep the MVP narrow: one API (Stripe) before expanding
- Every fix should default to "hold for human review" unless confidence is high — never auto-merge (enforced today: `--create-prs` never merges, just opens the PR)
- Prioritize a working end-to-end loop (even manually triggered) over polishing any single piece
- This is a solo build — avoid scope creep, resist the urge to add features before the core loop works
- Open source now, monetize later — don't build billing/plan/tier logic before there are real users to learn from; see `ROADMAP.md`. Getting real installs and trust comes first

## Commit conventions

- Do **not** add a "Co-Authored-By: Claude" (or similar) trailer to commit messages in this repo.
