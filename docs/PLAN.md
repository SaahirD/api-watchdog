# api-watchdog — Evolution Plan: from Stripe-only tool to hosted, any-API service

**Audience:** the three of us working on this. Two of you are picking this up fresh — Part 1
is the catch-up. Nothing here is built yet; this is the plan we execute against.

**Status of this doc:** working plan, pending team sign-off on the open questions in
§10 (notably the Phase 2–3-vs-4 sequencing and the three-lane split). Update it here as
decisions land; track phases as issues.

---

## Table of contents

1. What api-watchdog is today
2. What we're building (the three decisions from the call)
3. The gap, named
4. Architecture of the target system
5. The adaptive extraction engine (the hard part)
6. What we reuse / refactor / retire
7. The bridge: phased migration
8. Testing strategy
9. Team split & milestones
10. Risks & open questions
11. Immediate next steps

---

## 1. What api-watchdog is today

### 1.1 The core loop

> Detect an external API breaking change → find the affected code in a repo → generate a
> fix with Claude → open a GitHub PR → a human reviews and merges. **It never merges
> anything itself.**

Today this works, end to end, for **one API: Stripe**. It runs unattended on a 12-hour
cron in GitHub Actions. Real PRs have been opened
([#1](https://github.com/SaahirD/api-watchdog/pull/1),
[#2](https://github.com/SaahirD/api-watchdog/pull/2)). The repo is public, MIT-licensed,
with a working install guide.

### 1.2 Component map

| File | Role | Stripe-coupled? |
|---|---|---|
| `src/main.py` | CLI orchestrator. `check_for_api_changes()` runs two Stripe detectors with per-detector error isolation, dedups their matches by `(file, line)`, returns a list of "change dicts". `generate_fixes()` calls the fixer per change. `--create-prs` calls the PR creator. | Only the two hardcoded detector imports + log strings |
| `src/stripe_monitor.py` | **Changelog detector.** Fetches `docs.stripe.com/changelog`, parses the embedded `window.__INITIAL_STATE__` JSON, keeps entries with `breaking == true`, pulls symbols from Stripe's hand-curated `changed` array (falls back to backtick-span regex over prose for ~22% of entries). | Heavily — the URL, the JSON shape, the field names |
| `src/stripe_spec_monitor.py` | **OpenAPI spec-diff detector.** Diffs `github.com/stripe/openapi`'s `spec3.sdk.json` between a checkpoint git SHA and latest `master` using the `oasdiff` binary. Normalizes each diff entry into the same change-dict shape. Symbols come from oasdiff's text + Stripe's undocumented `x-stripeOperations` annotation. | The repo URL, the spec path, `x-stripeOperations` |
| `src/claude_fixer.py` | Builds per-file editable line "windows", prompts `claude-opus-5` for a replacement of just those lines, splices, validates syntax (`compile()` for Python; `node --check` for JS/TS; fails open otherwise). Returns `{change, diff, pr_description, fixed_files, ...}`. | **Not really** — only the literal strings "Stripe" and "payment flows" in the prompt/PR body |
| `src/pr_creator.py` | Authenticates as a GitHub App installation, creates/reuses a branch (`api-watchdog/<slug>`), force-resets a stale branch left by a merged PR, pushes exact file content via the Contents API, opens the PR with fail-closed existence checks. | **Not really** — one `"Stripe API change"` fallback string |
| `src/github_app.py` | Flask webhook receiver. HMAC-SHA256 verification works. The `push` / `pull_request` / `installation` handlers are **inert stubs** — they log and return, nothing else. Fully decoupled from the rest of `src/`. | No |
| `.github/workflows/watch.yml` | 12h cron + manual trigger. Installs `oasdiff` (pinned). Splits cache restore/save. Maps secrets `GH_APP_ID`/`GH_APP_PRIVATE_KEY` → env vars the code reads. Runs `python src/main.py --github-repo <repo> [--create-prs]`. | Named "Watch Stripe API Changes" |

### 1.3 The load-bearing contract: the "change dict"

Every layer depends on this dict shape. Both detectors produce it; the fixer and PR
creator consume it. **We do not break these field names.**

```
{ id, title, symbols, breaking, products, description,
  breaking_description, impact, release, release_date, url }
```

Then `detect_code_usage()` appends:

```
code_matches: [ { file, line, code, symbol, change_id, change_title } ]
```

### 1.4 What's already generic (more than you'd think)

- **`detect_code_usage()`** in `stripe_monitor.py` — pure filesystem walk, regex
  word-boundary match on `change['symbols']`. Zero Stripe knowledge. Already shared by
  both detectors.
- `SOURCE_EXTENSIONS`, `SKIP_DIRS`, `CODE_SPAN_RE`, `_is_specific_enough()` — generic
  token/noise filters.
- The change-dict and match-dict **schemas** — neutral field names.
- **`claude_fixer.py`** end to end — only branding strings couple it to Stripe.
- **`pr_creator.py`** end to end — only one fallback string.
- `main.py`'s orchestration shape — per-detector isolation, `(file,line)` dedup, the
  PR loop.

The Stripe-specific code is essentially: **two ingestion modules**. Everything downstream
is already API-agnostic. This makes the rewrite smaller than it looks.

### 1.5 What is verified live vs. honestly unproven

Being precise about this because it affects what we can trust as a baseline.

**Verified live in GitHub Actions:**
- The full changelog-detector → fixer → PR path (PR #1, PR #2, opened unattended).
- Both detectors running together, the `(file,line)` dedup, cache persistence across
  ephemeral runners.
- The spec-diff detector's control flow: cold start, matching, unchanged-checkpoint
  short-circuit, the cap/backpressure logic.

**Not proven live:**
- A real PR opened *from the spec-diff detector specifically*. Every attempt to force
  this has been blocked by `oasdiff` itself — its runtime on large Stripe spec diffs is
  wildly unreliable (17s once, an indefinite hang another time, 90s+ another, a full
  timeout another), and **not purely as a function of diff-window size**. The
  `timeout-minutes: 20` job guard is the current backstop (a hung run fails safely; the
  checkpoint doesn't advance, so nothing corrupts).

**Known limitations we carry forward as design inputs:**
- Self-scan false positives (the cron scans api-watchdog's own repo, whose docstrings
  quote Stripe symbols). *Disappears in the hosted model — we always scan the tenant's
  repo, never our own source.*
- Syntax gate ≠ semantic correctness. Human review stays load-bearing.
- Regex symbol matching, not AST.
- A dry run still writes the seen-changes cache (not read-only).
- Spec-diff can't re-surface an unmatched change later (checkpoint-based, not a rolling
  window). *Fixed by the hosted model — see §5.5.*
- The changelog detector **swallows fetch/parse failure and returns `[]`** — identical
  to a clean empty run. Across many APIs this is silent, indefinite non-detection. *This
  is why §5.7 exists.*

### 1.6 Current distribution model and why it's zero-infra

Each user registers **their own** GitHub App scoped to their own repo, copies `src/` +
`watch.yml` in, adds three secrets (`ANTHROPIC_API_KEY`, `GH_APP_ID`,
`GH_APP_PRIVATE_KEY`), and runs the cron. There is deliberately no server.

The reason: **a GitHub App's private key is not scoped to one installation.** Anyone
holding it can mint an access token for *any* repo that has installed that App. So a
single shared App's key cannot safely be handed to users as a secret. "Register your own
App" was the only safe zero-infra option.

**The decision in this plan is to build the server that removes that constraint** — the
key lives on our infrastructure and never leaves it.

---

## 2. What we're building (the three decisions from the call)

Restating the three decisions precisely, because the wording matters for scope:

### Decision 1 — No hardcoded per-API modules. A generic engine that adapts to any API's docs.

Not `stripe_monitor.py`, `github_monitor.py`, `twilio_monitor.py`. And **not** "just feed
every changelog to an LLM and hope." Instead: an engine where **each API source is
described by a stored, versioned, editable profile** that the engine learns and refines
over time. Stripe's `window.__INITIAL_STATE__` parser stops being code and becomes *one
profile row*. Adding an API = adding a source URL and letting the engine learn its
structure, confirmed by one of us before it goes live.

What "learns and grows" concretely means is specified in §5.2. It is **not** model
fine-tuning and **not** a crawler that finds APIs on its own.

### Decision 2 — A real hosted backend service.

A central server that holds the GitHub App private key, a database, a scheduler, and
workers. Multi-tenant: many users, many repos, many watched APIs. Users onboard by
signing in with GitHub, installing our App, and choosing which APIs to watch. The
existing detect→fix→PR pipeline becomes backend jobs.

This is a deliberate departure from the repo's historical "open source now, no server
until monetization" stance. It is an owner-approved inflection because we now have a team
and intent to make this a product. The scope discipline still applies *within* each phase
(see §10).

### Decision 3 — A time-triggered backend workflow.

The backend's own scheduler becomes the primary trigger, replacing the GitHub Actions
cron. Actions stays supported as a **self-hosted single-tenant deployment option** running
the same engine package.

### Non-goals for this effort

- No billing / plan / tier *enforcement*. We add usage **metering** from day one; we
  price later, once there's real usage. (Matches `ROADMAP.md`.)
- No auto-merge, ever. PRs always hold for human review.
- No "any URL on the internet" — "generic" is bounded to **five concrete source kinds**
  (OpenAPI spec, GraphQL schema, HTML changelog, RSS/Atom feed, markdown changelog in a
  git repo).
- No model fine-tuning.
- `claude_fixer.py` and `pr_creator.py` stay frozen (only a branding string and the
  token source change).

---

## 3. The gap, named

| Dimension | Today | Target |
|---|---|---|
| APIs supported | Stripe only, via two hardcoded modules | Any API, via a generic engine + per-source learned profile |
| Adding an API | Write a new Python module | Add a source URL; engine learns; one human confirms |
| Changelog parsing | Stripe's exact JSON blob | Deterministic where structure exists; LLM structured-extraction (profile-guided) for prose; verified |
| Spec diffing | `oasdiff` against `stripe/openapi` hardcoded | `oasdiff` (or an in-house differ) against any spec repo/URL, bounded lookback window |
| Detector abstraction | None — two duck-typed classes, hardcoded in `main.py` | `Detector` protocol + registry, config/DB-driven instantiation |
| Config | None — CLI flags + module constants | DB rows: `api_sources`, `source_endpoints`, `extraction_profiles`, `watched_apis` |
| Trigger | GitHub Actions 12h cron | Backend scheduler (DB-backed schedule + tick process); Actions as self-host option |
| Multi-tenancy | One repo per install, user holds their own App key | Many tenants, one App, key held server-side |
| Feedback loop | `github_app.py` handlers are inert stubs | `pull_request` webhook → `pr_outcomes` → profile & fix-exemplar updates |
| Tests | **Zero.** No `tests/`, no pytest | pytest: characterization tests, worker/integration tests, per-API eval corpus |
| Persistence | Two gitignored JSON cache files | Postgres |
| Silent-failure detection | None (parse failure looks like "no changes") | Per-source yield expectations → `profile_stale` alarm |

---

## 4. Architecture of the target system

### 4.1 Backend service — components

**Key structural decision: split global upstream ingestion from per-tenant code
matching.** Fetching a changelog, parsing it, and running `oasdiff` on a spec pair are
*tenant-independent*. Doing them once per tenant would mean hundreds of identical
`oasdiff` runs against a tool we already know is unreliable. So: **ingestion runs once
per source** and writes a global `source_changes` table; **matching runs per repo**
against that table.

| Component | Responsibility | Tech |
|---|---|---|
| Web / API layer | GitHub OAuth login; App webhook receiver; REST API for the dashboard; admin endpoints (add source, edit profile, mark false-positive) | **FastAPI** + uvicorn/gunicorn |
| Scheduler ("tick") | Every ~60s: find due `source_endpoints` and due `watched_apis`, enqueue jobs, advance `next_*_at`. `SELECT … FOR UPDATE SKIP LOCKED` so it's safe to run more than one. | Small dedicated process |
| Upstream-ingest workers | Per source endpoint: fetch → classify → extract/diff → write global `source_changes` + advance the endpoint checkpoint | RQ worker, `ingest` queue |
| Tenant-match workers | Per `watched_api`: shallow-clone the repo, run `detect_code_usage()` over new `source_changes`, run the Verifier, write `detected_changes`, enqueue fixes | RQ worker, `match` queue |
| Fix workers | Per detected change: call `claude_fixer.generate_fix_for_changes()` **unchanged**, persist `fixes` | RQ worker, `fix` queue |
| PR workers | Per fix: call `pr_creator` (**logic unchanged**, token comes from the server) | RQ worker, `pr` queue |
| DB | System of record | **Postgres** + SQLAlchemy 2.0 + Alembic; `pgvector` for exemplar retrieval |
| Redis | Job transport; per-endpoint / per-`watched_api` locks; installation-token cache; content-hash cache of LLM classify/extract results | Redis |
| Blob store | Saved spec snapshots, fetched changelog HTML, raw `oasdiff` JSON, run logs, `fixed_files` originals | Start: a `blobs` table / mounted volume. Grow: S3 / R2 |
| Secret storage | GitHub App private key; encrypted BYO Anthropic keys | Host secret manager; ideally a separate token-broker process that alone holds the App key |

**Why FastAPI over Flask:** the loose change-dict becomes a **Pydantic `Change` model**
shared by detectors, workers, DB serialization, and the API — one schema validated
everywhere. Async webhook + dashboard API. Auto-generated OpenAPI docs for whoever builds
the frontend. Flask exists in the repo only as the inert `github_app.py`; its ~40 lines
of HMAC verification port straight into a FastAPI route. No reason to run two frameworks.

**Why RQ for the queue:** jobs are plain functions; best-in-class introspection
(`rq-dashboard`, failed-job queue) matters for a 3-person team debugging a pipeline with
many external failure modes. The expensive operations (`oasdiff` subprocess, `git clone`,
the sync Anthropic SDK already used in `claude_fixer.py`) are blocking anyway.
**Caveat:** RQ's worker is `fork`-based and does not run natively on Windows — our dev
machines are Windows. **Local dev is docker-compose (Linux containers) regardless.** If
native-Windows worker dev becomes a hard requirement, switch to **Arq**; that, not async
fit, is the only reason to. Scale-up path is **Celery** when we need per-tenant
rate-limiting and task routing.

### 4.2 Multi-tenant GitHub App — and the security shift

**One** GitHub App (the product's). The private key never leaves the server. Ideally a
small **token-broker** process is the only thing that holds it and hands out
installation tokens over internal RPC, so fix/PR workers never see the key.

Onboarding: "Sign in with GitHub" (OAuth) → "Install the App" → GitHub redirects back
with `installation_id` → `installation` / `installation_repositories` webhooks populate
our `installations` and `repos` tables.

Per-repo operation: App JWT (10-min, signed with the private key) →
`POST /app/installations/{id}/access_tokens` **scoped to the specific repo** and to
`contents:write, pull_requests:write, metadata:read` → 1-hour token, cached in Redis
keyed by `(installation_id, repo_id)`, **never persisted to disk or DB**. This is exactly
what `pr_creator.get_installation_client()` does today via PyGithub's
`GithubIntegration` — it moves server-side and gains a cache.

**The shift to be explicit about with each other:** today, a customer's code and the
App key never leave the customer's runner. In the hosted model **we hold the key
(blast radius of compromise = every tenant repo)** and **we shallow-clone private repos
onto our infrastructure**. Mitigations are in §10; this is a real change in what we're
asking users to trust us with.

### 4.3 Data model (core tables)

Postgres. Every table has `id uuid pk`, `created_at`, `updated_at`. Abbreviated.

**Identity / tenancy**
- `users` — `github_user_id`, `github_login`, `email`, `last_login_at`
- `installations` — `github_installation_id`, `account_login`, `account_type`, `suspended_at`, `raw_payload`
- `installation_users` — `installation_id`, `user_id`, `role` (who can manage an install)
- `repos` — `installation_id`, `github_repo_id`, `full_name`, `default_branch`, `private`, `clone_url`, `active`
- `api_credentials` — `owner_type/owner_id`, `kind` (`anthropic_key`), `ciphertext`, `key_version` (envelope-encrypted)

**API catalog + global ingestion (tenant-independent)**
- `api_sources` — `key` (slug), `display_name`, `homepage_url` — the catalog of watchable APIs
- `source_endpoints` — `api_source_id`, `kind` (`openapi_spec` | `graphql_schema` | `html_changelog` | `rss_atom` | `markdown_changelog` | `git_repo_file`), `url`, `config jsonb`, `enabled`, `poll_interval_minutes`, `next_poll_at` (indexed), `last_polled_at`, `last_successful_parse_at`, `checkpoint jsonb` (the upstream cursor — formerly the JSON cache files), `expected_yield jsonb` (rolling stats for the §5.7 alarm)
- `extraction_profiles` — `source_endpoint_id`, `version int`, `status` (`learning` | `active` | `needs_review`), `profile jsonb` (the learned structure — §5.3), `confidence`, `sample_count`, `eval_scores jsonb`, `updated_by` (`bootstrap` | `auto_feedback` | `human`). One `active` row per endpoint; old versions retained for rollback + eval replay
- `profile_examples` — `source_endpoint_id`, `polarity` (`positive`|`negative`), `raw_excerpt`, `expected_symbols`, `was_breaking`, `origin` (`bootstrap`|`human`|`pr_outcome`), `embedding vector` — few-shot corpus for LLM extraction
- `source_changes` — **global, no code matches.** `source_endpoint_id`, `external_id` (the change-dict `id`), `dedup_key` (for cross-endpoint reconciliation), `title`, `symbols jsonb`, `breaking`, `severity`, `payload jsonb` (description / breaking_description / impact / url / release / release_date / products), `detector`, `raw_ref` (blob pointer), `first_detected_at`. *Because these rows persist independently of any checkpoint, a tenant that adopts an affected symbol next month re-matches against retained history — this fixes today's "spec-diff can't re-surface an unmatched change" limitation.*

**Per-tenant matching + fixes**
- `watched_apis` — `repo_id`, `api_source_id`, `enabled`, `interval_minutes`, `next_run_at` (indexed), `last_run_at`, `last_status`, `watermark jsonb` (cursor into `source_changes`), `config jsonb` (path filters, language hints), `mode` (`shadow` | `notify` | `auto_pr`)
- `detected_changes` — `watched_api_id`, `source_change_id`, `status` (`matched` | `no_match` | `verified` | `rejected` | `fix_generated` | `pr_open` | `pr_merged` | `pr_closed` | `dismissed`), `code_matches jsonb`, `verifier_result jsonb`, `first_matched_at`
- `fixes` — `detected_change_id`, `status`, `model`, `prompt_version`, `diff`, `pr_description`, `fixed_files jsonb` (blobs by reference), `validation jsonb`, `token_usage jsonb`, `attempt`, `error`
- `pull_requests` — `fix_id` **unique** (stops a retried job double-creating), `repo_id`, `github_pr_number`, `branch`, `url`, `state` (`open`|`merged`|`closed`), `head_sha`, `merged_at`, `closed_at`
- `pr_outcomes` — `pull_request_id`, `outcome` (`merged_clean` | `merged_with_edits` | `closed_unmerged` | `stale`), `merged_by`, `review_comments_count`, `human_diff_ref` (blob: our branch head vs. merged tree), `recorded_at` — **the learning signal**

**Operations**
- `run_logs` — `scope`, `ref_id`, `job_type`, `status` (`queued` | `running` | `success` | `no_changes` | `partial` | `failed` | `timeout` | `profile_stale`), timing, `stats jsonb`, `error`, `log_ref`. *`no_changes` and `profile_stale` are distinct — see §5.7.*
- `llm_usage` — `user_id`, `repo_id?`, `source_endpoint_id?`, `fix_id?`, `purpose` (`classify`|`extract`|`verify`|`fix`), `model`, `input_tokens`, `output_tokens`, `cost_usd`

Key indexes: `source_endpoints(next_poll_at) where enabled`;
`watched_apis(next_run_at) where enabled`;
`source_changes(source_endpoint_id, first_detected_at)`;
`detected_changes(watched_api_id, status)`; `pull_requests(state) where state='open'`.

### 4.4 Time-triggers — DB-backed schedule + a tick process

A dedicated process loops every ~60s:

```sql
SELECT id FROM source_endpoints WHERE enabled AND next_poll_at <= now()
  FOR UPDATE SKIP LOCKED;        -- enqueue upstream_ingest, bump next_poll_at
SELECT id FROM watched_apis     WHERE enabled AND next_run_at  <= now()
  FOR UPDATE SKIP LOCKED;        -- enqueue tenant_match, bump next_run_at
```

Chosen over APScheduler / Celery-beat because state lives in Postgres (survives
restarts), per-source **and** per-tenant cadence come for free, `SKIP LOCKED` makes
multiple tick replicas safe, and it's trivially debuggable
(`SELECT * FROM watched_apis ORDER BY next_run_at`). A missed tick just delays a poll by
60s.

### 4.5 Anthropic billing

- **Launch: BYO key, encrypted at rest.** Matches today; caps our cost exposure at zero.
  Stored in `api_credentials` with envelope encryption; decrypted in worker memory per
  run, never logged.
- **Hosted-key tier — scaffold now, bill later.** Every Anthropic call records tokens /
  model / purpose into `llm_usage`, attributed to user + repo. Build the metering table,
  not the billing integration (matches `ROADMAP.md`).
- **Cost controls regardless of tier:** deterministic-first extraction; a content-hash
  cache on classify/extract/verify results shared across tenants (they're
  tenant-independent); cheap model for classify/extract/verify, expensive model only for
  the actual code fix; per-repo daily fix budget; a per-run fix cap (this is where
  today's `MAX_SPEC_CHANGES_PER_RUN` moves to); a global circuit breaker.

### 4.6 Tenant source code on our infrastructure — a decision, not an open question

Workers **shallow-clone private repos onto our infrastructure** (`--depth 1
--single-branch` into a per-run tmpfs dir, guaranteed teardown in a `finally` plus a
reaper for crashed workers, never persisted, one job can't read another's checkout).

Why clone rather than use the GitHub Tree/Blob API: `detect_code_usage()` needs the full
working tree to `os.walk`; per-symbol code-search doesn't scale (hundreds of symbols ×
many repos, rate-limited) and misses matches the search tokenizer won't surface;
`claude_fixer` needs real files on disk to build line windows.

For prospects who can't accept "code leaves our repo" at all, the fallback offering is
**self-hosted `watch.yml` mode** — same engine package, code stays in their Actions
runner.

### 4.7 Deployment shape

One Docker image, three entrypoints: `web`, `worker` (queue via arg), `scheduler`.
`oasdiff` v1.29.1 baked in (same pin as `watch.yml`).

- **Start:** docker-compose on a single 4–8 GB VPS (Hetzner / DO) running `web`,
  `worker`, `scheduler`, `redis`; **managed Postgres** (Neon / Supabase / RDS) separate,
  so DB durability isn't tied to the box.
- **Grow:** workers to their own box (memory-capped cgroups to bound the `oasdiff` blast
  radius), a second `web` replica behind a load balancer, managed Redis.

---

## 5. The adaptive extraction engine (the hard part)

This is the section to read carefully. It's the part that's unproven, and it's what
makes the product worth building.

### 5.1 Design principles

1. **Deterministic first.** Wherever a machine-readable schema exists (OpenAPI, GraphQL),
   diff it with a tool and never ask the model. The LLM is for **prose changelogs
   only**.
2. **LLM output is verified, not trusted.** Extracted symbols must actually resolve
   against the spec / SDK surface, or they're downgraded to "heuristic tier" and gated
   harder before they can open a PR.
3. **Split global ingestion from per-tenant matching** (§4.1) — so classify/extract/diff
   happen once per source, not once per tenant.
4. **Every automated profile change is a new version row.** A bad update is one revert
   away; the eval corpus can replay against any version.

### 5.2 What "learning" concretely means

**It IS:**
- **A stored, versioned, human-editable profile per source** (§5.3) — selectors, field
  maps, the "what counts as breaking" rule, symbol-extraction rules, casing transforms,
  the versioning regex, poll cadence, ETag.
- **An accumulating symbol vocabulary and stopword list** per source, grown from
  confirmed-breaking entries and PR outcomes. (Example: a symbol that led to a PR that
  was closed unmerged because the code wasn't actually affected → moves to
  `stopword_symbols`.)
- **A small few-shot exemplar store** (`profile_examples`) — real past entries labeled
  breaking/not-breaking with their expected symbols — retrieved by similarity and
  injected into extraction prompts. **This is "the RAG": retrieval over our own verified
  history, not over the internet.**
- **Calibrated per-source confidence thresholds** that gate auto-PR vs. notify-only.

**The learning signal** is the PR outcome: merged clean → the extraction+symbol+fix chain
was right; merged with edits → partially right, store the `(our fix → human final)` diff
as a fix exemplar; closed unmerged → wrong, negative signal. Those webhook handlers in
`github_app.py` that are currently inert stubs **are exactly this missing feedback path.**

**It is NOT:**
- Model fine-tuning or any weight update.
- A crawler that discovers APIs on its own.
- A replacement for deterministic diffing.
- Unbounded per-entry LLM usage — deterministic path first, LLM only when structured
  signal is absent or ambiguous, cached by content hash.

### 5.3 The `extraction_profile` — Stripe's parser becomes one row

Today `stripe_monitor.py` *is* Stripe's profile, written as Python. In the target, that
knowledge is data:

**For changelog kinds (`html_changelog` / `markdown_changelog` / `rss_atom`):**

```jsonc
{
  "entry_locator": {
    "strategy": "embedded_json" | "css_selector" | "rss_items" | "heading_split",
    "embedded_json": { "marker": "window.__INITIAL_STATE__ = ",
                       "search_key": "releaseTrains",
                       "entries_path": ["releases", "changelogEntries"] },
    "css": { "entry": "article.changelog-entry", "title": "h3",
             "date": "time[datetime]", "body": ".changelog-body" }
  },
  "field_map": {
    "id":   "entry.id || entry.slug",
    "date": { "path": "release.published", "format": "%Y-%m-%d" },
    "breaking_rule": { "kind": "boolean_field", "field": "breaking" },
        //           | { "kind": "label_regex", "pattern": "(?i)\\[BREAKING\\]|\\(breaking change\\)" }
        //           | { "kind": "section_heading", "pattern": "(?i)breaking changes" }
        //           | { "kind": "classifier_llm" }
    "symbol_sources": [
      { "kind": "structured_field", "field": "changed" },
      { "kind": "backtick_spans", "fields": ["description","breakingDescription","impact"] },
      { "kind": "dotted_paths", "field": "affected" }
    ],
    "migration_prose_fields": ["impact", "breakingDescription"]
  },
  "versioning_scheme": { "kind": "dated", "regex": "\\d{4}-\\d{2}-\\d{2}" },
  "symbol_vocabulary": { "known_symbols": [...], "stopword_symbols": [...],
                         "casing_transforms_seen": ["snake->camel","snake->Pascal"] },
  "extraction_hints": { "min_symbol_len": 4, "require_identifier_structure": true },
  "fetch": { "url": "...", "poll_interval_minutes": 720, "etag": null }
}
```

The current Stripe behavior is reproduced exactly by a profile with
`entry_locator.strategy = embedded_json`, marker `window.__INITIAL_STATE__ = `,
`breaking_rule = boolean_field:breaking`, and those three `symbol_sources`. Running the
old module and the new profile-driven engine on the same saved fixture and diffing the
output is our regression gate.

**For spec kinds (`openapi_spec` / `graphql_schema`):**

```jsonc
{
  "artifact": { "kind": "git_repo_file", "repo": "https://github.com/stripe/openapi",
                "path": "openapi/spec3.sdk.json", "branch": "master" },
  "diff": { "tool": "oasdiff", "version": "1.29.1",
            "flags": ["--allow-external-refs=false"], "level": "breaking",
            "bounded_lookback": { "max_commits": 50, "max_days": 21,
                                  "on_exceed": "fast_forward_and_alert" } },
  "symbol_enrichment": [
    { "kind": "backtick_spans", "field": "text" },
    { "kind": "vendor_extension_operation_index", "extension": "x-stripeOperations",
      "yields": "Resource.method", "resource_pascal_case": true }
  ],
  "impact_templates": { "api-path-removed-without-deprecation": "This endpoint no longer exists...",
                        "_default": "Review this change against the API reference..." },
  "checkpoint_kind": "commit_sha"
}
```

### 5.4 Ingestion pipeline for a newly-added URL

Triggered when one of us adds an `api_source` with one or more URLs.

1. **Fetch** — follow redirects; capture final URL, content-type, ETag/Last-Modified;
   snapshot to blob store.
2. **Classify the endpoint kind** — deterministic checks first, one constrained LLM call
   only as a tiebreak:
   - parses as JSON with `openapi`/`swagger` key → `openapi_spec`
   - `{"data":{"__schema":…}}` or SDL with `type Query` → `graphql_schema`
   - RSS/Atom content-type or root element → `rss_atom`
   - host is a git forge + target is a file → `git_repo_file`, sub-classified by content
   - `text/html` with repeated sibling entry nodes + date-like text +
     "breaking"/"deprecat" tokens → `html_changelog`
   - else: one LLM classification over the first ~10 KB, output constrained to the enum
3. **Locate the diffable artifact / entry anchor** — for specs in a git repo, resolve
   `{repo, path, branch}` and use last-processed commit SHA as the checkpoint (today's
   model); for a bare spec URL, content-hash snapshots and diff last vs. current; for
   changelogs, identify the repeating entry structure.
4. **Bootstrap the profile** — for changelogs, an LLM "structure induction" pass over
   3–10 sample entries proposes selectors + field mapping; **status starts `learning`**;
   one of us confirms in the dashboard before it goes `active`. For specs the profile is
   mostly config, so straight to `learning` with an operator review.
5. **Backfill / calibrate** — run extraction over the last N entries; operator
   thumbs-up/down the parsed breaking changes; those labels seed `profile_examples` and
   set the initial `confidence`; scores land in `eval_scores`.

### 5.5 Runtime — upstream ingest (per source endpoint)

1. Redis lock on the endpoint.
2. Conditional GET (stored ETag). 304 and nothing new → `run_logs.status = no_changes`,
   bump `next_poll_at`, done.
3. Dispatch to the endpoint's Detector by `kind` (§5.10):
   - **`GenericOpenApiDetector`:** resolve latest ref; apply **bounded lookback** — never
     diff a checkpoint older than `min(max_commits, max_days)`; on exceed, fast-forward
     the checkpoint to the bound, diff only the recent window, log `partial` + alert, and
     rely on the changelog endpoint to cover the skipped span. Fetch old + new artifact
     (blob-cached). Run `oasdiff` with a hard subprocess timeout + wall-clock budget +
     memory-capped cgroup. Keep the raw-entry cap (now a global oasdiff-output bound) and
     the `examined_fingerprints` backpressure. Normalize entries via `impact_templates` +
     `symbol_enrichment`. After M consecutive `oasdiff` failures, auto-flip the endpoint
     to changelog-only and alert.
   - **`AdaptiveChangelogDetector`:** locate entries via `entry_locator`; for each entry
     newer than the checkpoint, apply `breaking_rule` (structured field / label regex /
     section heading / — only if none — one cached LLM classifier call); extract symbols
     via `symbol_sources`; if breaking but symbols are empty/low-confidence, one
     **structured LLM extraction call** guided by the profile + the k nearest
     `profile_examples`, returning `{symbols[], migration_summary, confidence}`.
4. Upsert `source_changes` (dedupe by `external_id`; set `dedup_key`). Advance the
   endpoint checkpoint (respecting backpressure holdback). Update `expected_yield` rolling
   stats.

### 5.6 Runtime — per-tenant match + the Verifier

1. Redis lock on the `watched_api`.
2. Shallow-clone the repo to a per-run tmpfs dir with a fresh repo-scoped installation
   token.
3. Select `source_changes` for this source newer than `watermark`.
4. **Run `detect_code_usage()` unchanged.** (Phase 1 uses it verbatim — it's the
   characterization baseline. Later optimization: one `os.walk` matching the union of all
   pending symbols, attribute matches back afterward, keep the current signature as a thin
   wrapper so the tests still pass. Ripgrep-as-subprocess is worth a spike.)
5. **Verifier** — `(change, code_matches, repo_dir, profile) → {verdict, confidence,
   reasons, per_match[]}`:
   - Deterministic gates: symbols pass `_is_specific_enough`; not in `stopword_symbols`;
     match count under a sanity cap; matched file isn't vendored/generated; matched line
     isn't comment/string-only (cheap heuristic, or tree-sitter if present).
   - Corroboration: if a `dedup_key`-equivalent `source_change` exists from the *other*
     detector → high confidence.
   - LLM adjudication (one cheap call, cached on `(source_change_id, file, line-hash)`):
     "given this change summary and these exact matched lines, is this code actually
     affected? yes / no / uncertain + one line why." Only `yes`, or `uncertain` +
     corroborated, proceeds when the source's confidence is below its auto threshold.
6. Persist `detected_changes`. Enqueue `fix` jobs for `verified` ones, respecting the
   per-repo daily fix budget and per-run fix cap. Advance `watermark`, set `next_run_at`.

### 5.7 The "profile is broken" alarm (must-have)

Today's changelog detector swallows fetch/parse failure and returns `[]` —
indistinguishable from a clean empty run. Across many sources that's silent, indefinite
non-detection (Stripe redesigns the docs page, `window.__INITIAL_STATE__` disappears,
every dashboard stays green forever).

- Per-endpoint **yield expectations** on the profile (`expected_yield`: rolling
  entries-per-fetch, `last_successful_parse_at`).
- A locator that reliably matched N entries now matches **zero**, or a fetch that
  historically yielded breaking entries yields none for an anomalous span →
  `extraction_profiles.status = needs_review`, `run_logs.status = profile_stale`, and an
  alert. **Not** a `no_changes` run.
- The dashboard surfaces `needs_review` profiles prominently. Onboarding a new source is
  gated on the operator seeing a healthy backfill first.

### 5.8 Feedback loop from PR outcomes

`pull_request` webhook, action `closed`:
- `merged == true` → diff **our branch head vs. the merged tree**. Identical →
  `merged_clean`. Human edited files → `merged_with_edits`, store the human diff blob.
- `merged == false` → `closed_unmerged`. Strong negative signal.

What updates (all as **new versioned profile rows**, `updated_by = auto_feedback`, never
in place):
- **Fix quality:** `merged_clean` reinforces the current `prompt_version` for that change
  type. `merged_with_edits` stores the `(our fix → human final)` pair as a fix exemplar
  `claude_fixer` retrieves for similar future changes. A cluster of `closed_unmerged` for
  a source/change-type lowers that source's auto-fix confidence.
- **Extraction profile:** `closed_unmerged` where the post-hoc verifier or a reviewer
  comment says "code wasn't actually affected" → the symbol goes to `stopword_symbols`;
  the entry becomes a negative `profile_example`. `merged_clean` → its symbols go to
  `known_symbols`, the entry becomes a positive exemplar, `sample_count++`, `confidence`
  recalibrated, `eval_scores` re-run.
- **Human corrections in the dashboard:** mark a `detected_change` false-positive, edit
  symbols, or edit the profile — each writes a new profile version
  (`updated_by = human`) and appends a `profile_example`.

### 5.9 OpenAPI-diff generalization + the oasdiff problem

`GenericOpenApiDetector` takes `(artifact_ref, checkpoint, diff_config)` from the profile.
`stripe_spec_monitor.py`'s `SPEC_URL_TEMPLATE` / `COMMITS_URL` / `COMPARE_URL_TEMPLATE`
become templates derived from `artifact.repo`. Checkpoint: commit SHA where git history
exists; content-hash + blob snapshot otherwise.

**oasdiff is unreliable independent of window size** (documented: 17s / hang / 90s /
timeout on comparable inputs). Defense in depth:
1. bounded lookback so the window is always small
2. hard subprocess timeout + wall-clock budget + memory cgroup
3. keep the raw-entry cap
4. M-consecutive-failure auto-fallback to changelog-only + alert
5. **escape hatch:** a purpose-built structural differ over the parsed spec JSON covering
   only the ~12 rule classes we actually consume (removed path; removed / became-required
   request property or param; removed response property; type change; became-nullable) —
   a fraction of oasdiff's ~500 rules, fully under our timeout control. Keep oasdiff for
   the early phases; decide on the in-house differ from real telemetry.

GraphQL: `graphql-inspector` (Node) or a Python SDL differ, same checkpoint model.

### 5.10 The detector / plugin contract

A structural `Protocol` (PEP 544) — matches the existing duck-typing, forces no
inheritance:

```python
class Detector(Protocol):
    kind: str  # matches source_endpoint.kind
    def detect(self, ctx: DetectionContext) -> DetectionResult: ...

@dataclass
class DetectionContext:
    endpoint: SourceEndpoint
    profile: ExtractionProfile
    checkpoint: dict
    http: HttpClient      # conditional GET, retries, snapshotting
    blobs: BlobStore
    llm: LlmClient        # model routing + llm_usage metering + content-hash cache
    budget: Budget        # wall-clock + LLM $ ceilings
    logger: RunLogger

@dataclass
class DetectionResult:
    changes: list[Change]     # candidates; NO code_matches (engine owns matching)
    checkpoint_update: dict
    truncated: bool           # hold the checkpoint back — existing backpressure semantics
    stats: dict
```

- **`Change`** is a Pydantic model with the **exact current field names**, so
  `claude_fixer` and `pr_creator` are untouched. It serializes to `source_changes.payload`.
- **The engine — not the detector — owns `detect_code_usage()`, the caps, and the
  Verifier.** Detectors only produce candidate `Change`s + a checkpoint update. This is
  a deliberate change from today's `get_pending_changes()`, which returns changes with
  `code_matches` already attached — the split is what makes global ingestion possible.
- **Registry:** `DETECTORS: dict[str, type[Detector]]` keyed by `source_endpoint.kind`.
  `openapi_spec → GenericOpenApiDetector`; `graphql_schema → GenericGraphqlDetector`;
  `html_changelog | markdown_changelog | rss_atom → AdaptiveChangelogDetector`.

---

## 6. What we reuse / refactor / retire

**Reused as-is (say this loudly — it's more than half the value):**
- `claude_fixer.py` — only the literal strings `"Stripe"` / `"payment flows"` become a
  source-name template variable carried on the `Change`.
- `pr_creator.py` — only the token source swaps (server-side broker instead of env var).
  Branch naming, stale-branch force-reset, Contents API create-vs-update, fail-closed PR
  existence checks — all unchanged.
- `detect_code_usage()` — moves verbatim into the engine.
- The change-dict field names — become the `Change` Pydantic model, same names.
- The `oasdiff breaking … --allow-external-refs=false` invocation string.
- `github_app.py`'s HMAC-SHA256 verification (~40 lines) — ports verbatim into a FastAPI
  route.
- The checkpoint / `examined_fingerprints` / backpressure model — moves onto
  `source_endpoints.checkpoint`.
- `_is_specific_enough`, `CODE_SPAN_RE`, `_camel_case` / `_snake_case` / `_pascal_case`,
  the `_IMPACT_TEMPLATES` dict — already generic, become profile data or shared helpers.

**Refactored:**
- `main.py` → a thin CLI over the new engine (kept for self-host / local runs).
- `stripe_monitor.py` → `AdaptiveChangelogDetector` + a Stripe profile row.
- `stripe_spec_monitor.py` → `GenericOpenApiDetector` + a Stripe profile row.
- `github_app.py` → Flask→FastAPI; the stub handlers become the real feedback loop.

**Retired:**
- The two gitignored JSON cache files → Postgres.
- The `--repo .` self-scan default → hosted always clones the tenant repo (kills the
  self-scan false-positive class entirely).
- `--days-back` as a CLI knob → per-source config.

**Net:** no detection logic is discarded. It is parameterized. The Stripe profile rows
reproduce today's hardcoded behavior; old-vs-new on the same fixtures is the gate.

---

## 7. The bridge: phased migration

**Ordering principle:** the Stripe loop stays green at every step. We prove the refactor
with tests before we touch behavior, and we stand the backend up on the *known-good*
Stripe pipeline (shadow mode) before layering the generic engine on top.

**A sequencing decision to make together (see §10):** the phases below stand the backend
skeleton up (Phase 2–3) *before* the generic changelog engine (Phase 4). Rationale: don't
build the server and the risky extraction engine simultaneously — get multi-tenant infra
proven on a pipeline we already trust, byte-matching against `watch.yml`, then build the
engine on stable ground. The alternative is to finish and validate the generic engine
end-to-end in the *current* Actions model first, and only then build the server. Pick one
before M2.

| Phase | Scope | ~Effort | Stripe loop |
|---|---|---|---|
| **0 — Scaffolding, zero behavior change** | New `api_watchdog/` package, `pyproject.toml`, pinned deps, `pytest`. Capture real fixtures: `changelog.html` (with `__INITIAL_STATE__`), a `spec3.sdk.json` old/new pair, a real `oasdiff breaking` JSON, a GitHub commits response. Write **characterization tests** locking in current outputs of both detectors, `claude_fixer` (Anthropic mocked), `pr_creator` (PyGithub mocked, incl. stale-branch reset). | ~1 wk | unchanged (CLI) |
| **1 — Detector contract + registry** | Introduce `Detector`, `DetectionContext`, `DetectionResult`, `Change`, `DETECTORS`. Refactor both Stripe detectors to consume a `profile` dict (hardcoded = current values). Engine owns `detect_code_usage` + caps + a deterministic-only Verifier. `main.py` builds the two profiles inline. **Phase-0 tests must still pass — this is the net.** | ~1 wk | unchanged (CLI) |
| **2 — Backend skeleton, Stripe only, shadow mode** | FastAPI: GitHub OAuth, webhook receiver (HMAC verbatim; real `installation` handlers). Postgres + Alembic + the data model. Server-side installation-token service + Redis cache; `pr_creator` swapped to it. RQ + Redis; `upstream_ingest` + `tenant_match` workers running the Phase-1 engine for Stripe, reading checkpoints from the DB. Shallow-clone step. Scheduler tick. `fix`/`pr` workers wrapping `claude_fixer`/`pr_creator` unchanged. Deploy compose to one VPS + managed Postgres. Onboard our own repo as tenant #1. **Cutover gate = shadow mode:** backend detects + generates + persists the fix but does **not** open a PR; we diff backend output against what `watch.yml` produced for the same `change.id`. Byte-match on identical inputs = gate passed. | ~2 wk | runs in parallel (Actions still authoritative) |
| **3 — Backend authoritative** | Backend Stripe loop opens PRs; `watch.yml` demoted to the documented self-host option. Migrate JSON cache content into `source_endpoints.checkpoint` + `watched_apis.watermark`. | ~1 wk | backend now authoritative |
| **4 — Adaptive changelog engine** | `AdaptiveChangelogDetector` fully profile-driven. Ingestion `classify → bootstrap → operator-confirm` flow. LLM structure induction + structured extraction + `profile_examples` retrieval. `extraction_profiles` live. **Onboard a second API (GitHub REST/GraphQL, per `ROADMAP.md`) purely by adding rows — no new module.** This is the proof the engine is generic. | ~2–3 wk | unaffected |
| **5 — Generic OpenAPI + bounded lookback + differ escape hatch** | `GenericOpenApiDetector` for any spec repo/URL; bounded-lookback window; snapshot checkpoint for non-git specs; build + evaluate the minimal in-house structural differ. | ~1–2 wk | unaffected |
| **6 — Feedback loop + verification LLM** | Real `pull_request` handler → `pr_outcomes`; branch-vs-merge diffing; profile/vocabulary/exemplar updates as versioned rows; dashboard for human corrections; LLM verification gate wired in with per-source thresholds; "notify-only until opt-in" for each newly onboarded repo. | ~2 wk | gains the feedback loop |
| **7 — Metering + hardening** | `llm_usage` metering; per-repo budgets; BYO-key encryption at rest; hosted-key tier flag; per-tenant rate limits; Sentry + structured logs + a `run_logs` dashboard; key-rotation runbook (one rotation is already overdue per `CLAUDE.md`). | ongoing | hardened |

---

## 8. Testing strategy

We are about to refactor a load-bearing contract with **zero existing tests**. The safety
net has to exist *before* the refactor.

- **Tooling:** `pytest`; `responses`/`respx` for HTTP; a compose Postgres (or
  `pytest-postgresql`) for DB tests; `factory-boy` fixtures.
- **Offline detector fixtures** (`tests/fixtures/`): saved `changelog.html`,
  `spec_old.json` / `spec_new.json`, `oasdiff_breaking.json`, `github_commits.json`.
  Detector tests run with **zero network**.
- **Characterization tests** (written in Phase 0, stay green through Phase 3): pin current
  outputs of both detectors; `claude_fixer` with Anthropic mocked (assert prompt
  structure, window merging, splice, `_validate_syntax` gate); `pr_creator` with PyGithub
  mocked (branch create / stale-branch force-reset / Contents API create-vs-update / PR
  create + fail-closed existence checks).
- **Engine / worker tests:** enqueue `upstream_ingest` against mocked endpoints → assert
  `source_changes` rows, checkpoint advance, `truncated` holdback, global raw-entry cap.
  Enqueue `tenant_match` against a fixture repo dir → assert `detected_changes`, Verifier
  verdicts, watermark advance, per-tenant fix cap. `fix`/`pr` jobs with mocked externals,
  including **retry-idempotency** (second run must not double-create — exercises
  `unique(fix_id)`).
- **Verifier tests:** table-driven — comment-only match, vendored path, stopword symbol,
  corroborated-by-both-detectors, LLM `uncertain` without corroboration → expected
  verdict.
- **Extraction-accuracy eval** (`tests/eval/<source>.jsonl`): a **labeled corpus per
  `api_source`** — historical entries as `{raw_entry, is_breaking, expected_symbols}`.
  `pytest -m eval` runs the profile-driven extractor over the corpus and asserts per-source
  thresholds (e.g. breaking-classification recall ≥ 0.95, symbol precision ≥ 0.80). Runs
  on every `extraction_profiles` version bump; scores persisted to `eval_scores`.
  **A source ships as `notify`/detect-only until it clears its bar.** This is how "works
  precisely with any API docs" is *measured* rather than asserted.
- **oasdiff reliability harness** (opt-in, not CI): run `oasdiff` over a matrix of
  checkpoint ages against saved specs to characterize timeout behavior and justify the
  bounded-lookback bound / the in-house differ.
- **Integration smoke:** `docker-compose up`, seed one tenant + Stripe source with a spec
  pair known to contain a breaking change, assert a PR-creation call is made (GitHub
  mocked at the transport layer).
- **CI:** GitHub Actions running `pytest` (unit + integration, network blocked) on every
  PR; the `eval` job nightly.

---

## 9. Team split & milestones

Three people, three lanes. The `Change` model and `DetectionContext` are the interface
between lanes B and C — **define those together, first.**

- **Person A — Platform / Backend.** FastAPI app, GitHub OAuth, data model + Alembic,
  server-side installation-token service (ideally the separate token-broker), webhook
  receiver + Phase-6 feedback handlers, deployment (compose, VPS, managed Postgres,
  secrets), observability. Owns Phases 2, 3, 6 (webhook half), 7.
- **Person B — Extraction Engine.** Detector contract + registry, porting both Stripe
  detectors to profile-driven, ingestion/classify pipeline,
  `AdaptiveChangelogDetector`, `GenericOpenApiDetector`, bounded lookback + in-house
  differ spike, the Verifier, profile learning + `profile_examples`. Owns Phases 1, 4, 5,
  6 (profile-update half). **Schedule risk: this is the critical path for every milestone
  after M2 and one person owns most of it. A or C should pair on Phase 4, and
  "eval bar not met → ship detect-only" is an accepted outcome, not a blocker.**
- **Person C — Workers / Fix / PR / QA.** RQ setup + job orchestration + retry
  semantics, scheduler tick, repo-clone service (tmpfs + teardown), keeping
  `claude_fixer` / `pr_creator` integrated, cost caps + `llm_usage` metering, and the
  **entire testing strategy including the eval corpus** and CI. Owns Phase 0, worker
  parts of 2/3, metering in 7.

**Milestones (~10–12 weeks):**

| # | Week | Deliverable |
|---|---|---|
| M1 | 2 | Phases 0–1 — engine refactor behind characterization tests, CLI still green |
| M2 | 5 | Phase 2 — backend runs the Stripe loop for tenant #1 through workers, shadow mode, output byte-matches `watch.yml` |
| M3 | 6 | Phase 3 — backend authoritative, Actions demoted to self-host |
| M4 | 9 | Phase 4 — adaptive changelog engine; GitHub API onboarded as a second source with **no new module** |
| M5 | 11 | Phases 5–6 — generic OpenAPI + bounded lookback; feedback loop + verification live |
| M6 | 12+ | Phase 7 — metering, hardening, closed-beta onboarding |

---

## 10. Risks & open questions

**Security — the server now holds the GitHub App private key.** Blast radius of
compromise = every tenant repo. Mitigations: key in a secret manager (not an env var in
the image); a separate token-broker process so fix/PR workers never see it; installation
tokens scoped per-request to the specific repo + minimal permissions, short TTL,
Redis-only, never persisted; audit-log every token mint; egress-restrict workers;
two-person prod access; a rotation runbook.

**Extraction accuracy.** Real risk the generic engine underperforms the hand-tuned Stripe
modules. Mitigations: the per-source eval corpus with precision/recall gates; ship a
source detect-only until it clears the bar; the Stripe profile is pinned to reproduce
current behavior and regression-tested. *Open question: what precision bar justifies
auto-opening a PR vs. only notifying?* Recommendation: PR only above a per-source
confidence threshold; else a dashboard/Slack notification.

**LLM cost at multi-tenant scale.** Mitigations: the global-ingestion split means
classify/extract happen once per source, not per tenant; content-hash cache shared across
tenants; cheap model for classify/extract/verify, expensive only for the code fix;
per-repo daily fix budget; global circuit breaker. *Open question: hosted-key tier
pricing vs. BYO.* Recommendation: BYO at launch, meter from day one, price later.

**oasdiff reliability.** Characterized as unreliable independent of window size.
Mitigations: bounded lookback, hard timeout + memory cap, consecutive-failure fallback to
changelog-only, planned in-house differ for the ~12 rule classes we consume. *Open
question: keep oasdiff at all, or build the small differ now?* Recommendation: keep for
Phases 2–3, build in Phase 5, decide from telemetry.

**False-positive PRs in someone else's repo.** Worse reputationally than in a solo tool.
Mitigations: the Verifier (deterministic gates + LLM adjudication), cross-detector
corroboration, per-source confidence gating, the feedback loop demoting bad symbols, a
"max open watchdog PRs per repo" cap, a notify-only period for each newly onboarded repo.
The self-scan false-positive class disappears (hosted never scans its own source).

**Profile rot / silent non-detection.** Addressed structurally by §5.7, but it needs
someone watching the alerts — a triage rotation for `needs_review` profiles.

**Private tenant code on our infrastructure.** Decision in §4.6 (clone to tmpfs,
guaranteed teardown, per-run isolation; self-host mode as the fallback). *Open question:
is per-tenant worker isolation (separate pods) needed for v1, or is per-run tmpdir
isolation enough for the beta?*

**Scope tension with the project's historical "keep it narrow" discipline.** This is an
owner-approved inflection, but the generic engine can sprawl. Guardrails: land it
phase-by-phase with the Stripe loop always working; the engine must reproduce hand-tuned
Stripe behavior before any new API is added; "generic" is bounded to five concrete source
kinds; a profile that can't clear its eval bar stays detect-only rather than earning a
code special-case; `claude_fixer` / `pr_creator` / `detect_code_usage` stay frozen.

**Other open questions for us to settle:**
- The Phase 2–3-before-Phase-4 sequencing decision (see §7).
- Dashboard scope for v1 — full web app, or just an onboarding flow + notifications?
- GraphQL support in v1, or defer?
- Self-hosted `watch.yml` mode — supported long-term, or deprecated once hosted is
  stable?
- When does a paid tier actually ship (metering now, billing when)?
- Which second API — `ROADMAP.md` ranks GitHub REST/GraphQL #1 (dated versions,
  git-diffable spec, official breaking-vs-additive changelog, and we already depend on
  the GitHub API), Twilio #2 (`CHANGES.md` self-labels `(breaking change)` inline).
  GitHub is the recommended M4 target.

---

## 11. Immediate next steps (week 1)

1. **Agree on the shape of this doc** and the Phase 2–3-vs-4 sequencing question.
2. **Assign the three lanes** (A / B / C above).
3. **Person C:** stand up `pyproject.toml` + `pytest` + `tests/`, and capture the Phase-0
   fixtures from live sources (changelog HTML, a real spec old/new pair, a real `oasdiff`
   JSON run).
4. **Persons B + C together:** write the `Change` Pydantic model and the
   `DetectionContext` / `DetectionResult` dataclasses — the interface everything else
   hangs off.
5. **Person A:** spike the FastAPI skeleton + the GitHub App OAuth/install flow against a
   throwaway App, and pick the VPS + managed-Postgres providers.
6. Rotate the credentials flagged as overdue in `CLAUDE.md` (ngrok authtoken, GitHub App
   private key, Anthropic key) — do this before the key starts living on a server.

---

## Appendix — critical files

| File | Why it matters to this plan |
|---|---|
| `src/main.py` | Current orchestration + cross-detector dedup contract; becomes the thin CLI over the engine |
| `src/stripe_monitor.py` | `detect_code_usage()`, `_extract_symbols()`, `_is_specific_enough()`, `_find_release_trains()` — the reused core of `AdaptiveChangelogDetector` |
| `src/stripe_spec_monitor.py` | `oasdiff` invocation, checkpoint / `examined_fingerprints` / backpressure model, `_normalize_oasdiff_entry()` — the core of `GenericOpenApiDetector` |
| `src/claude_fixer.py` | Reused as-is; defines the `Change` consumer contract and the `fixed_files` output `pr_creator` needs |
| `src/pr_creator.py` | Reused as-is except the token source; App installation auth, branch/stale-branch handling, Contents API push |
| `src/github_app.py` | HMAC verification to reuse verbatim; the stub handlers that become the real feedback loop |
| `.github/workflows/watch.yml` | The behavior the backend scheduler replaces; retained as the self-host deployment option |
