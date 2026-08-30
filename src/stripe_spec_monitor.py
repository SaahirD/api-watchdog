"""Detect Stripe API breaking changes by diffing the OpenAPI spec (oasdiff).

Second, independent detection source alongside stripe_monitor.py's
changelog scraper (see ROADMAP.md's Phase 5). Diffs Stripe's public
`stripe/openapi` spec repo (github.com/stripe/openapi, MIT licensed) against
a checkpoint commit using oasdiff (github.com/oasdiff/oasdiff), a free/
open-source OpenAPI diff + breaking-change classifier.

Why checkpoint-based, not version-based: Stripe's dated API versions
(e.g. "2026-08-26.dahlia") don't map to any tag in this repo — its own git
tags are sequential build numbers cut multiple times a day, unrelated to
those dated versions (confirmed by inspecting the repo directly). So
"which two spec versions to diff" means "diff against the last commit this
tool has already processed" (same idea as stripe_monitor.py's
seen-changes cache, just for a commit SHA instead of a set of change ids),
not a semantic version lookup.

Precision/coverage tradeoff vs. the changelog detector, not a speed one:
there's no evidence this repo updates ahead of Stripe's changelog. Its real
value is catching changes the changelog's prose doesn't cleanly name a
symbol for (~22% of changelog entries per stripe_monitor.py's own comment
have no `changed` array), via a structurally independent signal — and it
classifies *why* something is breaking (removed field, newly-required
field, etc.) rather than relying on Stripe's changelog writers to say so
in English.

Symbol candidates here are derived from oasdiff's diff text plus Stripe's
internal `x-stripeOperations` codegen annotations (an undocumented, vendor
extension — not a stable published contract), not from Stripe's own
hand-curated `changed` list. Expect noisier matches than the changelog
detector — same accepted tradeoff as stripe_monitor.py's own regex-based
matching (see CLAUDE.md's Known Limitations).

Known limitation this detector has that the changelog detector does NOT:
because it's checkpoint-based rather than a rolling time window, a change
that finds no match in the repo today is discarded and will never be
re-surfaced later even if the repo starts using that symbol tomorrow —
once the checkpoint advances past it, it's gone from every future diff.
stripe_monitor.py's changelog detector avoids this deliberately (see its
process_change docstring); this detector cannot, without keeping a
separate replay buffer, which is out of scope for Phase 5.
"""
import os
import re
import json
import shutil
import logging
import tempfile
import subprocess
from datetime import datetime
from typing import Optional

import requests

from stripe_monitor import CODE_SPAN_RE, _is_specific_enough, detect_code_usage

logger = logging.getLogger(__name__)

CACHE_FILE = ".stripe_spec_cache.json"
SPEC_URL_TEMPLATE = "https://raw.githubusercontent.com/stripe/openapi/{sha}/openapi/spec3.sdk.json"
COMMITS_URL = "https://api.github.com/repos/stripe/openapi/commits/master"
COMPARE_URL_TEMPLATE = "https://github.com/stripe/openapi/compare/{old}...{new}"

REQUEST_TIMEOUT = 30      # small API calls (commit lookup)
SPEC_FETCH_TIMEOUT = 60   # the spec file itself is ~10MB
OASDIFF_TIMEOUT = 120     # observed 17.7s for a real ~3-month diff (with
                          # --allow-external-refs=false — see _run_oasdiff);
                          # steady-state (~12h between runs) should be much
                          # faster than that. Generous headroom, still well
                          # under the job's own 20-minute timeout-minutes.

# Cap on matched, code-relevant changes acted on per run. stripe/openapi is
# tagged multiple times a day and oasdiff has ~500 rule types, so — unlike
# the changelog (naturally bounded, ~69 breaking entries/year) — a window
# with an unusually large Stripe-side spec change could plausibly produce
# many matches in one run and trigger a large amount of Claude spend.
# Applied AFTER code-matching (see get_pending_changes) so it keeps the
# highest-signal matched changes, not an arbitrary prefix of the raw diff.
# When the cap is hit, the checkpoint deliberately does NOT advance (see
# get_pending_changes) so the remainder is retried, not lost.
MAX_SPEC_CHANGES_PER_RUN = 15

# Separate, earlier cap on raw oasdiff entries, applied before the
# normalize+match loop — see get_pending_changes for the real incident
# that motivated this (a large historical diff produced ~206MB of raw
# output and was still running after 7+ minutes / 1.5GB+ RSS when
# stopped). MAX_SPEC_CHANGES_PER_RUN alone doesn't help here: it only caps
# what's kept AFTER every raw entry has already paid for a full
# detect_code_usage() filesystem walk. This should never bind in normal
# ~12h-cadence operation — only after an extended gap.
MAX_RAW_ENTRIES_PER_RUN = 300

# oasdiff's own remediation-guidance-free rule ids -> a templated "what to
# do" sentence, since (unlike the changelog) oasdiff provides no migration
# prose itself and claude_fixer.py leans on this text in its prompt.
_IMPACT_TEMPLATES = {
    'api-path-removed-without-deprecation': "This endpoint no longer exists. Remove or replace any code calling it.",
    'endpoint-removed': "This endpoint no longer exists. Remove or replace any code calling it.",
    'request-property-removed': "Remove any code that sends this field — the API no longer accepts it.",
    'request-parameter-removed': "Remove any code that sends this parameter — the API no longer accepts it.",
    'request-property-became-required': "This field is now required. Ensure it's always sent, or the request will fail.",
    'request-parameter-became-required': "This parameter is now required. Ensure it's always sent, or the request will fail.",
    'response-required-property-removed': "This field no longer appears in the response. Remove any code that reads it, and handle its absence.",
    'response-optional-property-removed': "This field no longer appears in the response. Remove any code that reads it.",
    'request-body-type-changed': "The expected request type changed. Update any code that builds this request to match the new type.",
    'response-property-type-changed': "The response type changed. Update any code that reads this field to handle the new type.",
    'response-body-became-nullable': "This response field can now be null. Update any code that reads it to handle that case.",
}
_DEFAULT_IMPACT = "Review this change against Stripe's API reference and update affected code accordingly."


def _github_headers() -> dict:
    """
    Auth header for api.github.com, if GITHUB_TOKEN is available.

    Not strictly required (unauthenticated is 60 req/hr, and this module
    makes at most ~1 API call per run) but avoids sharing a runner's
    IP-wide quota with other jobs. GitHub Actions does NOT put GITHUB_TOKEN
    in a step's environment automatically — watch.yml explicitly wires it
    from the auto-created secrets.GITHUB_TOKEN for this step.
    """
    token = os.getenv('GITHUB_TOKEN')
    return {'Authorization': f'Bearer {token}'} if token else {}


def _latest_master_sha() -> Optional[str]:
    """Resolve stripe/openapi's current HEAD commit SHA on master."""
    try:
        resp = requests.get(COMMITS_URL, headers=_github_headers(), timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        return resp.json()['sha']
    except Exception as e:
        logger.error(f"Failed to resolve stripe/openapi HEAD: {e}")
        return None


def _fetch_spec(sha: str) -> Optional[dict]:
    """Fetch spec3.sdk.json (the SDK variant, which carries x-stripeOperations) as of a given commit."""
    url = SPEC_URL_TEMPLATE.format(sha=sha)
    try:
        resp = requests.get(url, timeout=SPEC_FETCH_TIMEOUT)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        logger.error(f"Failed to fetch stripe/openapi spec at {sha[:12]}: {e}")
        return None


def _build_operation_index(spec: dict) -> dict:
    """
    Map (http_verb_lowercase, path) -> {'resource': schema_key, 'method_name': ...}
    using Stripe's `x-stripeOperations` vendor extension on component
    schemas — verified directly against a real spec3.sdk.json fetch:
    components.schemas.payment_intent.x-stripeOperations includes e.g.
    {"method_name": "capture", "method_on": "service", "operation": "post",
     "path": "/v1/payment_intents/{intent}/capture", ...}.

    This is Stripe's own internal codegen convention (confirmed present,
    not officially documented as a stable contract) — treat symbol
    candidates derived from it as heuristic, same confidence tier as the
    changelog's own prose-fallback path, not as guaranteed-precise.
    """
    index = {}
    schemas = spec.get('components', {}).get('schemas', {})
    for resource_key, schema in schemas.items():
        for op in (schema.get('x-stripeOperations') or []):
            verb = (op.get('operation') or '').lower()
            path = op.get('path') or ''
            method_name = op.get('method_name')
            if verb and path and method_name:
                index[(verb, path)] = {'resource': resource_key, 'method_name': method_name}
    return index


def _pascal_case(schema_key: str) -> str:
    """'payment_intent' -> 'PaymentIntent' (Stripe's typical resource class naming)."""
    return ''.join(part.capitalize() for part in re.split(r'[_.]', schema_key) if part)


# Deliberately not a real Stripe field name in the docstring example below
# — an earlier version used a real one and it round-tripped straight into
# a self-scan false-positive match against this very file (see CLAUDE.md's
# Known Limitations: this class of false positive isn't defended against,
# but there's no reason to manufacture new instances of it in example text
# when a fake placeholder documents the same transformation just as
# clearly).
def _camel_case(token: str) -> str:
    """'some_example_field' -> 'someExampleField'."""
    parts = token.split('_')
    if len(parts) < 2:
        return token
    return parts[0] + ''.join(p.capitalize() for p in parts[1:])


def _snake_case(token: str) -> str:
    """'someExampleField' -> 'some_example_field'."""
    return re.sub(r'(?<!^)(?=[A-Z])', '_', token).lower()


def _symbols_for_entry(raw: dict, operation_index: dict) -> list:
    """
    Build candidate search symbols for one oasdiff finding, from two
    sources:

    1. Backtick-quoted identifiers in oasdiff's own `text` field — it
       already writes these the same way Stripe's changelog prose does
       (e.g. "removed the request property `some_example_field`"),
       reusing stripe_monitor.CODE_SPAN_RE the same way stripe_monitor's
       own _extract_symbols() does for changelog prose.
    2. The resource+method name from _build_operation_index, for findings
       where `text` names no field at all (e.g. a removed endpoint —
       oasdiff's text is just "api path removed without deprecation").

    Every candidate is filtered through stripe_monitor._is_specific_enough
    — the same noise filter applied to changelog-derived symbols.
    """
    symbols = []

    for span in CODE_SPAN_RE.findall(raw.get('text', '')):
        span = span.strip()
        if not span:
            continue
        symbols.append(span)
        if '_' in span:
            symbols.append(_camel_case(span))
        elif re.search(r'[a-z][A-Z]', span):
            symbols.append(_snake_case(span))

    verb = (raw.get('operation') or '').lower()
    path = raw.get('path') or ''
    op = operation_index.get((verb, path))
    if op:
        resource = _pascal_case(op['resource'])
        method = op['method_name']
        symbols.append(f"{resource}.{method}")
        if resource:
            symbols.append(f"{resource[0].lower()}{resource[1:]}.{method}")

    seen = set()
    deduped = []
    for s in symbols:
        if s not in seen and _is_specific_enough(s):
            seen.add(s)
            deduped.append(s)
    return deduped


def _normalize_oasdiff_entry(raw: dict, sha: str, base_sha: str, operation_index: dict) -> Optional[dict]:
    """
    Convert one oasdiff finding into the same change-dict schema
    stripe_monitor.check_changelog() produces: {id, title, symbols,
    breaking, products, description, breaking_description, impact,
    release, release_date, url}.

    `raw` is one element of `oasdiff breaking old.json new.json -f json`'s
    output array. Shape verified directly (ran oasdiff v1.29.1 against a
    synthetic before/after spec pair):
        {"id": "<rule-id>", "text": "<human-readable finding>",
         "level": <2|3>, "operation": "<lowercase verb>",
         "operationId": "...", "path": "...", "section": "paths",
         "fingerprint": "<stable hash>", ["baseSource": {...}]}
    level 3 = definitely breaking (ERR); level 2 = ambiguous/warning-tier
    (e.g. "request-property-removed" — only actually breaking if a caller
    was sending that field). Both are kept and let through to
    code-matching: matching against the repo is itself the noise filter,
    same as an unmatched changelog entry being silently dropped.
    """
    fingerprint = raw.get('fingerprint')
    if not fingerprint:
        return None

    rule_id = raw.get('id', 'change')
    text = (raw.get('text') or '').strip()
    title = (text[0].upper() + text[1:]) if text else rule_id
    verb = (raw.get('operation') or '').upper()
    path = raw.get('path') or ''
    if verb or path:
        title = f"{title} ({verb} {path})".strip()

    return {
        # Deliberately NOT keyed on `sha`/`base_sha` — verified (code review,
        # 2026-08-30) that doing so was a real bug: `sha` is `latest_sha`,
        # which changes on every run (stripe/openapi moves constantly), so
        # the SAME real finding got a different id every run, silently
        # breaking `self.seen_changes` dedup entirely — not just in the
        # large-diff/truncation edge case, in ordinary operation. The
        # `fingerprint` oasdiff gives each finding is already documented as
        # stable (identifies the rule+location, not which commit pair
        # produced it) — that alone, namespaced against the changelog
        # detector's ids, is both sufficient and actually stable.
        'id': f"openapi-diff:{fingerprint}",
        'title': title,
        'symbols': _symbols_for_entry(raw, operation_index),
        'breaking': True,
        'products': [],
        'description': f"Stripe's public OpenAPI spec changed: {text}." if text else rule_id,
        'breaking_description': text or rule_id,
        'impact': _IMPACT_TEMPLATES.get(rule_id, _DEFAULT_IMPACT),
        'release': '',
        'release_date': datetime.utcnow().strftime('%Y-%m-%d'),
        'url': COMPARE_URL_TEMPLATE.format(old=base_sha, new=sha),
    }


def _run_oasdiff(old_spec_path: str, new_spec_path: str) -> Optional[list]:
    """
    Shell out to the oasdiff binary. Soft dependency — the caller checks
    shutil.which('oasdiff') before calling this, same pattern as
    claude_fixer.py's `node --check` gate.

    Verified directly: `oasdiff breaking` exits 0 whether or not it finds
    breaking changes (stdout is `[]` when there are none) — a non-zero
    exit is a real failure (e.g. malformed input), not "changes found".
    Returns None (not []) on failure, so the caller can tell "oasdiff ran
    and found nothing" apart from "oasdiff itself failed" and avoid
    advancing the checkpoint on the latter.

    `--allow-external-refs=false` is load-bearing, not just defensive:
    verified directly that with oasdiff's own default (true), diffing two
    real (differing) Stripe spec versions HANGS indefinitely — 4+ minutes
    of near-zero CPU (i.e. blocked on I/O, not slow computation) before a
    280s test timeout killed it, almost certainly oasdiff trying to
    resolve some $ref out over the network. Disabling it dropped the same
    real diff to 17.7s. oasdiff's own docs frame this flag as an SSRF
    guard against untrusted specs, which is a second good reason to keep
    it off even though Stripe's spec is first-party — but the practical
    reason it's here is that it was hanging every run, every time.
    """
    try:
        result = subprocess.run(
            ['oasdiff', 'breaking', old_spec_path, new_spec_path,
             '-f', 'json', '--allow-external-refs=false'],
            capture_output=True, text=True, timeout=OASDIFF_TIMEOUT
        )
    except Exception as e:
        logger.error(f"Failed to run oasdiff: {e}")
        return None

    if result.returncode != 0:
        logger.error(f"oasdiff exited {result.returncode}: {result.stderr.strip()[:500]}")
        return None

    try:
        return json.loads(result.stdout) if result.stdout.strip() else []
    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse oasdiff output: {e}")
        return None


class StripeSpecDetector:
    """
    Second, independent Stripe breaking-change detector: diffs
    stripe/openapi's spec3.sdk.json against a checkpoint commit using
    oasdiff, rather than scraping docs.stripe.com/changelog prose (see
    stripe_monitor.StripeChangeDetector). See module docstring for why.

    Uses its own cache file (not stripe_monitor's) — StripeChangeDetector.
    save_seen_changes() does an unconditional full-dict overwrite of its
    cache file; two detectors sharing one file would have whichever saves
    second clobber the other's write, so this needs a separate file even
    with namespaced keys.
    """

    def __init__(self, cache_file: str = CACHE_FILE):
        self.cache_file = cache_file
        self.checkpoint_sha = None
        self.seen_changes = {}
        # Fingerprints looked at during a run that got held back by a cap
        # (see get_pending_changes) — NOT the same as seen_changes (which
        # is only matched changes). Tracked so a repeatedly-truncated run
        # examines a NEW slice of raw entries each time instead of the
        # same first MAX_RAW_ENTRIES_PER_RUN prefix forever (verified,
        # code review 2026-08-30: a naive positional slice doesn't
        # converge — most raw entries won't match this repo, so the
        # matched-count staying low doesn't mean the window is small).
        # Cleared once the checkpoint actually advances — it's scoped to
        # "still working through the current stuck window", not global.
        self.examined_fingerprints = set()
        self.load_state()

    def load_state(self):
        """Load {checkpoint_sha, seen_changes, examined_fingerprints} from self.cache_file."""
        try:
            if os.path.exists(self.cache_file):
                with open(self.cache_file, 'r') as f:
                    state = json.load(f)
                self.checkpoint_sha = state.get('checkpoint_sha')
                self.seen_changes = state.get('seen_changes', {})
                self.examined_fingerprints = set(state.get('examined_fingerprints', []))
                logger.info(
                    f"Loaded spec-diff checkpoint "
                    f"{self.checkpoint_sha[:12] if self.checkpoint_sha else 'none'}, "
                    f"{len(self.seen_changes)} cached change(s), "
                    f"{len(self.examined_fingerprints)} examined-but-unresolved fingerprint(s)"
                )
        except Exception as e:
            logger.error(f"Failed to load spec-diff cache: {e}")
            self.checkpoint_sha = None
            self.seen_changes = {}
            self.examined_fingerprints = set()

    def save_state(self):
        """Persist {checkpoint_sha, seen_changes, examined_fingerprints} to self.cache_file."""
        try:
            with open(self.cache_file, 'w') as f:
                json.dump({
                    'checkpoint_sha': self.checkpoint_sha,
                    'seen_changes': self.seen_changes,
                    'examined_fingerprints': sorted(self.examined_fingerprints),
                }, f, indent=2)
        except Exception as e:
            logger.error(f"Failed to save spec-diff cache: {e}")

    def get_pending_changes(self, repo_path: str = '.', days_back: int = 180) -> list:
        """
        Full pipeline: resolve latest stripe/openapi commit, diff against
        the last-processed checkpoint, find affected code, return only
        changes that actually match something in the repo.

        `days_back` is accepted but unused — kept only so main.py can call
        this detector with the exact same signature as
        StripeChangeDetector.get_pending_changes(); there's no changelog-
        style rolling time window here, only "since the last checkpoint".
        """
        latest_sha = _latest_master_sha()
        if latest_sha is None:
            logger.error("Could not resolve latest stripe/openapi commit — skipping spec diff this run")
            return []

        if self.checkpoint_sha is None:
            # Cold start: first run, or the cache file didn't exist/restore.
            # Record the baseline and emit nothing this run — diffing
            # against nothing would surface Stripe's entire API surface as
            # "new" and flood the run.
            logger.info(f"No spec-diff checkpoint yet — recording {latest_sha[:12]} as baseline, no changes this run")
            self.checkpoint_sha = latest_sha
            self.save_state()
            return []

        if self.checkpoint_sha == latest_sha:
            logger.info("stripe/openapi unchanged since last spec-diff checkpoint")
            return []

        if shutil.which('oasdiff') is None:
            logger.warning("oasdiff not installed — skipping spec diff this run (soft dependency, see claude_fixer.py's node --check gate for the same pattern)")
            return []

        base_sha = self.checkpoint_sha
        old_spec = _fetch_spec(base_sha)
        new_spec = _fetch_spec(latest_sha)
        if old_spec is None or new_spec is None:
            logger.error("Failed to fetch one or both spec versions — skipping this run, checkpoint NOT advanced")
            return []

        operation_index = _build_operation_index(new_spec)

        with tempfile.TemporaryDirectory() as tmp:
            old_path = os.path.join(tmp, 'old.json')
            new_path = os.path.join(tmp, 'new.json')
            with open(old_path, 'w', encoding='utf-8') as f:
                json.dump(old_spec, f)
            with open(new_path, 'w', encoding='utf-8') as f:
                json.dump(new_spec, f)
            raw_entries = _run_oasdiff(old_path, new_path)

        if raw_entries is None:
            logger.error("oasdiff failed — skipping this run, checkpoint NOT advanced")
            return []

        # Drop fingerprint-less entries up front — _normalize_oasdiff_entry
        # already refuses to build a change without one, but leaving them in
        # raw_entries here would mean they can never be marked examined
        # (there's no fingerprint to record), so they'd occupy the same
        # wasted slot in every truncated retry's cap forever instead of
        # just being discarded once (caught by code review, 2026-08-30 — a
        # narrower version of the exact non-convergence bug this whole cap
        # was built to fix, for the entries this can't even happen to
        # normally: oasdiff has always included one in every real run seen
        # so far, this is a malformed-input/future-oasdiff-regression guard).
        no_fingerprint = sum(1 for r in raw_entries if not r.get('fingerprint'))
        if no_fingerprint:
            logger.warning(f"Discarding {no_fingerprint} oasdiff entr(y/ies) with no fingerprint — can't track or act on them")
        raw_entries = [r for r in raw_entries if r.get('fingerprint')]

        # Cap raw entries BEFORE the normalize+match loop, not just matched
        # results after it (see the cap below). Verified directly why this
        # matters: forcing a ~3-month-old checkpoint against a real,
        # differing Stripe spec produced a 206MB oasdiff output — likely
        # many thousands of entries — and matching every single one (each
        # a full detect_code_usage() filesystem walk) was still running
        # after 7+ minutes and 1.5GB+ RSS when stopped. Steady-state
        # (~12h between successful runs) should never come close to this;
        # it only bites after an extended gap (repeated failures, a long
        # pause).
        #
        # Skip already-examined fingerprints before slicing, not just
        # already-matched ones — a naive raw_entries[:N] would re-examine
        # the identical prefix every truncated run (most raw entries won't
        # match this repo, so a low match count doesn't mean the window is
        # actually small), never making progress into what's past N. Note:
        # "examined" is only recorded once a fingerprint is fully resolved
        # below (matched-and-kept, or confirmed no match) — NOT merely
        # because it was in this run's slice, so an entry cut by the
        # matched-cap further down doesn't get wrongly marked done.
        unexamined = [r for r in raw_entries if r.get('fingerprint') not in self.examined_fingerprints]
        raw_truncated = len(unexamined) > MAX_RAW_ENTRIES_PER_RUN
        if raw_truncated:
            logger.warning(
                f"{len(unexamined)} not-yet-examined raw entries exceed the cap of "
                f"{MAX_RAW_ENTRIES_PER_RUN} — processing only the next {MAX_RAW_ENTRIES_PER_RUN} "
                f"this run to bound how long matching takes; the rest are picked up next run."
            )
        raw_entries = unexamined[:MAX_RAW_ENTRIES_PER_RUN]

        changes = []
        for raw in raw_entries:
            change = _normalize_oasdiff_entry(raw, latest_sha, base_sha, operation_index)
            if change:
                changes.append((raw.get('fingerprint'), change))

        # Match against the repo BEFORE deciding what to cache/cap — see
        # the cap-handling note below for why order matters here.
        matched = []
        resolved_fingerprints = set()  # confirmed no-match this run — safe to mark examined regardless of the matched-cap below
        for fingerprint, change in changes:
            change_id = change.get('id')
            if not change_id or change_id in self.seen_changes:
                if fingerprint:
                    resolved_fingerprints.add(fingerprint)
                continue
            matches = detect_code_usage(repo_path, change)
            if not matches:
                # Deliberately NOT cached in seen_changes — same reasoning
                # as stripe_monitor.py's process_change, though see this
                # module's docstring for why it's weaker here (checkpoint
                # advancement still means this specific diff window won't
                # be re-examined once we move past it). Still fine to mark
                # examined for THIS stuck window's progress — there's
                # genuinely no match to lose.
                if fingerprint:
                    resolved_fingerprints.add(fingerprint)
                continue
            change['code_matches'] = matches
            matched.append((fingerprint, change))

        # Cap AFTER matching (keeps highest-signal matches, not an
        # arbitrary prefix of the raw diff). If we hit the cap, mark only
        # the kept subset as seen and deliberately do NOT advance the
        # checkpoint — so next run re-diffs the SAME window, skips the
        # now-cached subset via the `change_id in self.seen_changes` check
        # above, and naturally surfaces the next batch. This converges
        # without ever silently dropping a matched change. `raw_truncated`
        # (above) holds the checkpoint back the same way, for the same
        # reason — if we didn't even look at every raw entry this run, the
        # window isn't fully processed yet either.
        matched_truncated = len(matched) > MAX_SPEC_CHANGES_PER_RUN
        if matched_truncated:
            logger.warning(
                f"{len(matched)} matched spec changes this run exceeds the cap of "
                f"{MAX_SPEC_CHANGES_PER_RUN} — keeping the first {MAX_SPEC_CHANGES_PER_RUN} "
                f"and not advancing the checkpoint, so the rest are picked up next run."
            )
            matched = matched[:MAX_SPEC_CHANGES_PER_RUN]

        # Only mark KEPT matches examined — anything cut by the cap above
        # must stay un-examined so it's looked at again (and this time
        # actually cached) next run, instead of being silently lost the
        # way a same-run "mark everything in the slice examined" would.
        resolved_fingerprints.update(fp for fp, _ in matched if fp)
        self.examined_fingerprints.update(resolved_fingerprints)

        matched = [change for _, change in matched]

        # Either cap withholds the checkpoint — raw_truncated means we
        # didn't even look at the whole diff window this run, so it isn't
        # fully processed regardless of how few matches came out of the
        # partial slice we did look at.
        truncated = raw_truncated or matched_truncated

        for change in matched:
            self.seen_changes[change['id']] = {
                'title': change.get('title'),
                'processed_at': datetime.utcnow().isoformat(),
                'matched': True,
            }

        if not truncated:
            self.checkpoint_sha = latest_sha
            # Fresh window next time this detector actually has something
            # new to diff — the fingerprints tracked above only exist to
            # make progress through *this* stuck window's backlog.
            self.examined_fingerprints = set()
        self.save_state()

        logger.info(f"{len(matched)} spec-derived change(s) have matching code and need fixes")
        return matched
