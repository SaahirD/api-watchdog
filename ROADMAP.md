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

## Phase 5 (proposed, not started): diff Stripe's OpenAPI spec, not just the changelog

Today, `stripe_monitor.py` parses prose from `docs.stripe.com/changelog` —
which means a breaking change is only detectable once someone at Stripe
writes a changelog entry describing it in English. Stripe also publishes
its full API surface as a versioned, machine-readable OpenAPI spec at
[`github.com/stripe/openapi`](https://github.com/stripe/openapi), updated
same-day as the API itself changes.

Diffing spec versions directly (removed fields, removed endpoints, changed
types/required-ness) would:
- Catch changes structurally, not by parsing prose — more reliable, and
  catches things a changelog entry might describe ambiguously or omit.
- Be faster: available same-day, not whenever the changelog is written up.
- Need no special access or partnership — the spec repo is public.

This is a natural next detection source to add alongside (not necessarily
replacing) the changelog — the two could cross-validate each other. Not
started; flagged here so the idea isn't lost.

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
