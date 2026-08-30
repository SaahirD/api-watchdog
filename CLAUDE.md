# api-watchdog

## What this project is

A tool that watches external APIs (starting with Stripe) for breaking changes, detects which parts of a connected codebase are affected, generates a fix using Claude, and opens a GitHub pull request with that fix — before the change breaks anything in production.

**Core loop:** detect change → find affected code → generate fix → open PR → human reviews and merges.

## Why this exists

Most tools (Dependabot, Renovate) watch package/dependency versions. Nobody watches *live external API behavior* — the changes that don't come with a version bump, just a changelog entry nobody reads. That's the gap this fills.

## Current phase

**Status as of 2026-08-30, for anyone (human or Claude) picking this up fresh:**
Phases 0-4 are complete, verified live, and running unattended on a
12-hour cron in GitHub Actions — real PRs have been opened
([#1](https://github.com/SaahirD/api-watchdog/pull/1),
[#2](https://github.com/SaahirD/api-watchdog/pull/2)), the repo is public
under MIT, and `README.md` has a working install guide for a stranger.
Phase 5 (a second, independent detection source — diffing Stripe's
OpenAPI spec, not just its changelog) is built, its own control flow is
verified live in Actions, and two real bugs it surfaced along the way are
fixed — but the one thing not yet confirmed live is a real PR opened
*from* the spec-diff detector specifically (see its entry below for
exactly what is and isn't proven, and why). Nothing is currently broken
or blocking; the loop runs itself. **Next**, per `ROADMAP.md`: nothing
urgent is queued — candidates are adding a second *API* (GitHub's
REST/GraphQL API is the top pick), or simply waiting for real usage
before deciding what's next, per the "open source now, get trust first"
plan below.

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

- **Phase 5 (OpenAPI spec-diff detector, running live in Actions; PR-creation path not yet confirmed live):** `src/stripe_spec_monitor.py`
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
  emitted no changes, exactly as designed).

  **Verified live in GitHub Actions (2026-08-30)**, via 4 real
  `workflow_dispatch` runs while getting this working end-to-end — not a
  clean first try, and worth recording exactly what broke and what that
  proved:
  1. Run 1 crashed: the changelog detector's self-scan (a known, accepted
     limitation — see Known Limitations) matched a live Stripe changelog
     entry against `stripe_spec_monitor.py`'s own docstring examples, and
     `pr_creator.py`'s branch-reuse logic then crashed trying to reuse a
     stale branch from an already-merged PR (a real, separate bug, fixed —
     see below).
  2. Fixed `pr_creator.py`; run 2 still failed — same self-scan false
     positive, this time via a *different*, pre-existing docstring example
     in `stripe_monitor.py` itself, and Claude correctly refused to
     generate a fix for a non-real usage site (exit 1, exactly the
     documented "no fixes could be generated" case). Checking the repo's
     actual Actions cache list (`GET /repos/.../actions/caches`) afterward
     showed **no new cache entry from either failed run** — disproving my
     original assumption that the combined `actions/cache` action saves on
     the post-step regardless of job failure. Without a fix, that same
     false positive would have refetched and failed on *every* future run,
     forever, since the cache holding "already seen" never persisted.
  3. Fixed by splitting into `actions/cache/restore` (early) +
     `actions/cache/save` (`if: always()`, at the end) — confirmed by
     checking the caches list again after run 3 (which still failed on the
     same false positive) that a new entry now appeared.
  4. Run 4: clean success — `Loaded 1 cached changes`, correctly skipped
     the now-cached false positive, spec-diff detector correctly reported
     "unchanged since last checkpoint", exited 0, cache saved. This
     confirms the full production pipeline (both detectors, dedup, cache
     persistence) genuinely works unattended in Actions.

  **Attempted to force a full end-to-end close-out (2026-08-30)**: seeded
  a real ~3-month-old `stripe/openapi` commit as the local checkpoint and
  ran `main.py --create-prs` for real, specifically to exercise a genuine
  matched spec-diff change through to PR creation. This surfaced two more
  real bugs (both fixed, see the `619053d` commit and Known Limitations):
  `oasdiff`'s own `--allow-external-refs` default (`true`) made it hang
  indefinitely trying to resolve `$refs` over the network (confirmed: 4+
  minutes of near-zero CPU, i.e. genuinely blocked, not slow) — fixed by
  passing `--allow-external-refs=false`. Separately, matching *every* raw
  oasdiff entry against the repo before capping doesn't scale to a large
  historical diff (a real ~206MB oasdiff output was still being processed
  after 7+ minutes / 1.5GB+ RSS when stopped) — fixed with a new
  `MAX_RAW_ENTRIES_PER_RUN` cap applied before the matching loop, using
  the same checkpoint-holdback backpressure as the existing matched-cap.

  **Honest residual gap**: even with both fixes, `oasdiff`'s own runtime
  on this same large diff proved genuinely unreliable across repeat
  attempts — 17.7s once, a real hang another time (confirmed via
  `Get-NetTCPConnection`: zero open connections, zero CPU — not the
  external-refs issue recurring, something else), 90+ seconds of heavy
  multi-core computation another time. This is specific to a *large*
  diff window (an extended gap since the last successful run) — steady-
  state ~12h-cadence operation has never shown this. `watch.yml`'s
  `timeout-minutes: 20` job guard is the real backstop (a hung run fails
  safely; the checkpoint doesn't advance either way, so nothing gets
  corrupted, just retried next cron cycle) — not a fix for the
  unreliability itself. A bounded-lookback-window design (cap how old a
  checkpoint can be before diffing, rather than diffing an arbitrarily
  large gap) would close this properly; not built — real scope beyond
  today, and not a risk in normal operation. Because of this, the actual
  matched-change-through-PR-creation path for the spec-diff detector
  specifically remains unconfirmed live (see below).

  **Still not exercised end-to-end**: an actual PR opened by the spec-diff
  detector, or `pr_creator.py`'s stale-branch-reset fix specifically being
  hit again — neither has had a real trigger since being fixed. Will
  happen naturally on a future real Stripe change or a recurring
  self-scan false positive.

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

- **Self-scan false positives:** running the tool against this repo's own source produces a couple of false-positive matches, because `stripe_monitor.py`'s own docstrings/comments quote Stripe symbol names as examples (e.g. `redirectToCheckout`, and `_is_specific_enough`'s own docstring quotes `PaymentMethodTypes`). Not a pattern expected in a real customer repo; not defended against. Since the scheduled workflow's default `--repo .` scans this same repo, this can occasionally surface as a non-zero exit on `watch.yml` (no usable fix generated for a false-positive match) — expected, not an incident. Confirmed live, not just hypothetical: the `PaymentMethodTypes` docstring example above caused two real `watch.yml` failures on 2026-08-30 once a real Stripe changelog entry happened to use that exact symbol — see Phase 5's entry above for the full story and the (separate, real) bugs that surfaced alongside it.
- **Syntax gate ≠ semantic correctness:** `claude_fixer.py` validates that a generated fix still parses, but a syntactically valid-yet-wrong fix (e.g. duplicated code) would pass the gate. Human review remains load-bearing by design (see guiding principles below).
- **Symbol matching is regex-based, not AST-based:** works well in practice (tightened to require underscore/dot/camelCase structure to cut noise) but can still miss or mismatch in edge cases a real parser wouldn't.
- **A dry run is not read-only w.r.t. the seen-changes cache:** `stripe_monitor.py`'s `get_pending_changes()` calls `save_seen_changes()` unconditionally, before `main.py` ever checks `--create-prs`. So running without `--create-prs` still marks any matched change as "seen" — a dry run followed by a real `--create-prs` run against the same restored cache will find nothing pending and silently skip PR creation, even though no PR was ever opened. Bitten by this once verifying Phase 4 (2026-08-30): a `workflow_dispatch` dry run before the real `create_prs` run poisoned the Actions cache, so the "real" run found nothing to do. Fixed by bumping the cache key (`stripe-changes-cache-v2-` in `watch.yml`) to force a fresh cache, and going forward: don't chain a dry run immediately before a real run against the same cache — either test with a change ID not yet cached, or accept that a preceding dry run consumes it.
- **Spec-diff's symbols are lower-precision than the changelog's:** `stripe_spec_monitor.py`'s candidates come from oasdiff's diff text plus the undocumented `x-stripeOperations` codegen annotation, not from Stripe's own hand-curated `changed` list. Expect noisier/less complete matches than the changelog detector, same accepted tradeoff as the existing regex-based matcher's own limitation above — not something being fixed now.
- **Spec-diff can't re-surface an unmatched change later, unlike the changelog:** `stripe_monitor.py`'s changelog detector deliberately doesn't cache unmatched changes so a symbol adopted later still gets caught (see its `process_change` docstring). `stripe_spec_monitor.py` can't do this the same way — it's checkpoint-based (diffs "since last processed commit"), not a rolling time window, so once the checkpoint advances past a diff window, an unmatched change from that window is gone from every future diff, even if the repo starts using that symbol tomorrow. Would need a separate replay buffer to fix; out of scope for Phase 5.
- **Two detectors can still double-PR across runs:** `main.py`'s `check_for_api_changes()` dedups the changelog and spec-diff detectors' matches within a single run (by `(file, line)`), but if they independently detect the *same* real Stripe change in *different* runs, two PRs on two branches can still result — there's no cross-run reconciliation. Accepted per this project's scope discipline; revisit only if it turns out to happen often in practice.
- **Testing `stripe_spec_monitor.py` against a real (not synthetic) spec change requires manually forcing an old checkpoint** — pass a throwaway `cache_file` to `StripeSpecDetector(cache_file=...)` with a `checkpoint_sha` seeded to an old `stripe/openapi` commit (found via its GitHub API commit history), the same way `.stripe_changes_cache.json`'s poisoning bug above should never be reproduced against the real cache file. Be aware doing this forces a large diff window — see the next item.
- **`oasdiff` itself is unreliable on a large diff window, even with the fixes applied:** confirmed directly (2026-08-30, forcing a ~3-month-old checkpoint to test the above) — `--allow-external-refs=false` (see `stripe_spec_monitor.py`'s `_run_oasdiff`) fixes a genuine hang from external `$ref` resolution, and `MAX_RAW_ENTRIES_PER_RUN` bounds how much *this codebase* processes after oasdiff returns, but `oasdiff`'s own runtime on the identical two ~10MB real spec files still ranged unpredictably from 17.7s to a separate genuine hang (confirmed via `Get-NetTCPConnection`: zero open connections, zero CPU) to 90+ seconds of heavy multi-core computation. `OASDIFF_TIMEOUT` is 120s, which is not a guarantee for a large window. `watch.yml`'s `timeout-minutes: 20` job guard is the real backstop — a hung run fails safely (checkpoint doesn't advance either way) rather than corrupting anything, just costs an Actions run and retries next cron cycle. Only a real risk after an extended gap (repeated failures, a long pause) — steady-state ~12h-cadence operation keeps the diff window small and has never shown this. A bounded-lookback-window design would close this properly; not built, real scope beyond what's been done so far.

**Two more real bugs found via a `/code-review high` audit pass (2026-08-30), both fixed same-day — worth recording precisely because they'd have quietly broken production, not just an edge case:**
- **The spec-diff `id` embedded the wrong (volatile) SHA, breaking dedup entirely:** `_normalize_oasdiff_entry` built `id` as `f"openapi-diff:{latest_sha[:12]}:{fingerprint}"`. `latest_sha` changes on *every* run — stripe/openapi is tagged multiple times a day — so the exact same real finding (same `fingerprint`, which oasdiff itself documents as stable) got a *different* `id` every run. `self.seen_changes` dedup checks by `id`, so this never actually matched on a second run — meaning the spec-diff detector would have re-detected, re-fixed, and re-opened a PR for the *same* real Stripe change on every single 12h run, forever, once one was ever found live. Fixed: `id` is now `f"openapi-diff:{fingerprint}"` — no SHA. Verified with a test that forces two different `latest_sha` values with the same fingerprint and confirms the second run correctly sees it as already-seen.
- **The `MAX_RAW_ENTRIES_PER_RUN` fix above didn't actually converge:** slicing `raw_entries[:300]` positionally re-examined the *identical* prefix every truncated run, since most raw entries don't match a given repo (the real noise-filter is `detect_code_usage`, not the cap) — a low match count didn't mean the window was small, so a real match sitting past position 300 would never be reached. Fixed by tracking `examined_fingerprints` (persisted in `.stripe_spec_cache.json` alongside `checkpoint_sha`/`seen_changes`, cleared once the checkpoint actually advances) so each truncated run skips what it already looked at and makes real progress into the backlog — verified with a test where the first 3 (capped) raw entries are all noise and the real match sits 4th; run 1 finds nothing, run 2 correctly reaches it. A related subtlety caught in a second self-review pass while building this fix: a finding that *matches* but gets cut by the separate `MAX_SPEC_CHANGES_PER_RUN` cap must NOT be marked examined (it hasn't been cached/acted on yet) — only genuinely-resolved fingerprints (no match, or matched-and-kept) get marked, so a cap-cut match still gets a real second chance next run instead of being silently dropped. Also verified with its own test.

## Guiding principles for this project

- Keep the MVP narrow: one API (Stripe) before expanding
- Every fix should default to "hold for human review" unless confidence is high — never auto-merge (enforced today: `--create-prs` never merges, just opens the PR)
- Prioritize a working end-to-end loop (even manually triggered) over polishing any single piece
- This is a solo build — avoid scope creep, resist the urge to add features before the core loop works
- Open source now, monetize later — don't build billing/plan/tier logic before there are real users to learn from; see `ROADMAP.md`. Getting real installs and trust comes first

## Commit conventions

- Do **not** add a "Co-Authored-By: Claude" (or similar) trailer to commit messages in this repo.
