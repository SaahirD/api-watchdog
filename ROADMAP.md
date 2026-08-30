# Roadmap

Not a commitment or a timeline — a record of what's next and why, so future
decisions don't have to be re-derived from scratch. See `CLAUDE.md` for
what's actually built and verified today.

## Now: open source, no billing

The plan is to open-source this, get it running on a handful of real repos,
and only think about charging once there's real usage and trust to build on.
Concretely, that means:

- Each installer registers **their own** GitHub App, scoped to their own
  repo(s), and runs the existing `.github/workflows/watch.yml` in *their*
  repo with their own secrets — no central server to build or pay for.
- Each installer also supplies their own `ANTHROPIC_API_KEY` — no shared
  Claude bill to manage across users.
- Docs a stranger can actually follow without reading the source (README).

**Why not one shared public GitHub App that everyone installs?** Because a
GitHub App's private key isn't scoped to one installation — anyone holding
it can mint an installation access token for *any* repo/org that has ever
installed that App (the installation list is enumerable via the App's own
JWT). Handing that key to every user as a secret in their own repo — which
is what "install our shared App, paste this key into your Actions secrets"
would require — would let any one installer impersonate the App against
every other installer's repo. Real multi-tenant GitHub Apps solve this by
keeping the private key server-side, in a service *they* run — which is
infra we're deliberately not building yet (see "Later" below). Until then,
"register your own App" is the only safe zero-infra distribution model.

No monetization work belongs in this phase — no billing integration, no
plan/tier logic, nothing gated. Revisit once there are real installs.

## Phase 5 (implemented 2026-08-30): diff Stripe's OpenAPI spec, not just the changelog

`src/stripe_spec_monitor.py` diffs
[`github.com/stripe/openapi`](https://github.com/stripe/openapi)'s spec
against a checkpoint commit using [`oasdiff`](https://github.com/oasdiff/oasdiff),
running alongside (not replacing) `stripe_monitor.py`'s changelog scraper.
See `CLAUDE.md`'s Phase 5 entry for exactly what's been verified vs. not.

**Correction to this section's original framing**: the "be faster, same-day"
claim this section used to make turned out not to hold up — research found no evidence
`stripe/openapi` updates ahead of Stripe's own changelog, and its own git
tags are unrelated sequential build numbers, not a per-dated-API-version
scheme, so there's no clean "diff version X against version Y" the way this
originally assumed. What the spec-diff source actually adds: it catches
changes structurally (removed fields, removed endpoints, changed
types/required-ness) rather than by parsing prose, which matters because
~22% of changelog entries don't cleanly name a symbol in `changed` and fall
back to weaker prose-extraction — the spec diff doesn't have that gap. A
precision/coverage improvement, not a speed one.

## Phase 6 (proposed, not started): which API to add next

Building a second *API* (not just a second detector for Stripe, which is
what Phase 5 was) is a materially bigger step — new fetch/parse logic, new
domain knowledge, a new `src/<provider>_monitor.py` following the same
`get_pending_changes()` contract `stripe_monitor.py`/`stripe_spec_monitor.py`
already establish. Not started, deliberately — `CLAUDE.md`'s "one API before
expanding" principle holds until Stripe's two-detector setup has proven
itself in production for a while.

When it's time, this is the ranked research (2026-08-30) to start from —
9 major APIs, evaluated on: does it publish a versioned/diffable spec, does
it publish a structured changelog with breaking changes labeled, and how
popular is it to integrate against:

| Rank | API | Spec-diff fit | Changelog fit | Verdict |
|---|---|---|---|---|
| 1 | **GitHub REST/GraphQL** | Excellent — actively tagged, git-diffable, official | Excellent — official page splits breaking vs. additive per dated version | Best overall match to Stripe's own model |
| 2 | **Twilio** | Excellent — `twilio-oai`, git-diffable | Excellent — `CHANGES.md` self-labels `(breaking change)` inline | Near-turnkey given explicit labels |
| 3 | **Shopify Admin API** | Moderate — REST ok, GraphQL schema is auth-gated | Excellent — explicit "Breaking changes" sections per dated quarterly version | Best-organized changelog of the set |
| 4 | Plaid | Strong — `plaid-openapi`, dated versions, `[BREAKING]` labels | Strong | Closest structural twin to Stripe, smaller reach |
| 5 | OpenAI API | Strong (tagged spec releases) | Moderate — no explicit breaking marker; real risk is model-deprecation dates, a different shape of breakage | Needs custom heuristics |
| 6 | SendGrid | Moderate — spec repo reorganized under Twilio org | Weak-moderate | Cheap add-on if Twilio built first |
| 7 | Slack Web API | Weak — official spec repo archived/stale since 2024 | Strong — dated changelog + forward-looking "Scheduled changes" page | Changelog-only, not spec-diffing |
| 8 | PayPal/Braintree | Weak-moderate — PayPal has a spec repo of unconfirmed cadence; Braintree has none | Weak | Weakest of the real candidates |
| 9 | AWS (S3 representative) | Machine-readable models exist but at ~200-service scale, high noise | Weak — no per-service breaking-change feed, just a firehose "What's New" | Poor fit — breaking changes are rare by design |

**Top pick: GitHub's REST/GraphQL API.** Closest structural analogue to
Stripe — dated API versions with a defined support window, an
actively-tagged/diffable OpenAPI spec repo, *and* an official docs page
that already separates breaking from additive changes per version, so it's
the least new detection-strategy work of any candidate. Also dogfooding-
friendly: this project already depends on PyGithub/the GitHub API itself.

**Runner-up: Twilio.** Its OpenAPI spec repo's `CHANGES.md` self-labels
`(breaking change)` inline, making changelog-style detection nearly
turnkey — a simple line-scan gets most of the value with minimal parsing.

## Later: monetization, once there's real usage to build on

No specifics decided — deliberately, since deciding now would be building
ahead of any evidence about what users actually value. When it's time to
revisit, the open questions are:
- What's free vs. paid (e.g. Stripe-only forever free, more APIs or higher
  frequency paid; or a usage-based model on top of BYO Anthropic key)?
- GitHub Marketplace billing (less code, GitHub's cut, subject to their
  review) vs. billing directly (more control, more to build)?
- Whether BYO-Anthropic-key remains the model, or a hosted tier absorbs
  that cost in exchange for a higher price.
- A single shared, hosted GitHub App (real multi-tenant SaaS, private key
  held server-side, no per-user App registration) only makes sense once
  there's a server to run it on and a reason to build one — i.e. once this
  phase actually starts. Not before.

## Much later / speculative: deeper integration with the API being watched

The long-shot version of this idea is integrating directly with an API
provider's own live/internal change signals instead of watching their
public changelog or spec at all — genuinely faster, but requires a level of
trust and partnership that has no reason to exist before this tool has real
users and a track record. Not pursued until there's traction to point to;
the OpenAPI-diff idea above (Phase 5) is the realistic, buildable version of
the same instinct for now.
