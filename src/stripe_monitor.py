"""Monitor Stripe's changelog for breaking API changes and find affected code.

Stripe's changelog page (https://docs.stripe.com/changelog) is server-rendered
with a `window.__INITIAL_STATE__ = {...}` JSON blob embedded in the HTML. That
blob contains every changelog entry ever published, each with:
  - `breaking` (bool)
  - `changed`: candidate identifier strings for the changed symbol, in several
    casings (e.g. ["customer_update", "customerUpdate", "CustomerUpdate"])
  - `affected`: dotted API paths (e.g. "BillingPortal.Session#create.flow_data.type")
  - `description` / `breakingDescription` / `impact`: prose with backtick-quoted
    code spans, useful as a fallback when `changed` is empty (~22% of entries)

We parse that blob directly instead of scraping the rendered HTML/DOM — it's
more stable across markup changes and gives us real identifiers instead of
having to extract them from prose with an LLM call per entry.
"""
import os
import re
import json
import logging
from datetime import datetime, timedelta
from typing import Optional
import requests

logger = logging.getLogger(__name__)

CHANGELOG_URL = "https://docs.stripe.com/changelog"
CACHE_FILE = ".stripe_changes_cache.json"

# Directories to skip when walking a repo for code usage
SKIP_DIRS = {'.git', '__pycache__', 'node_modules', '.venv', 'venv', '.pytest_cache'}
SOURCE_EXTENSIONS = {'.py', '.js', '.ts', '.jsx', '.tsx', '.rb', '.java', '.go', '.php'}

# Backtick code spans in changelog prose, e.g. `stripe.redirectToCheckout`
CODE_SPAN_RE = re.compile(r'`([^`]+)`')
# A code span is worth treating as an identifier if it looks like one:
# letters/digits/underscore/dot, no spaces, no parens-only punctuation soup.
IDENTIFIER_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_.]{2,60}$')


def _extract_initial_state(html: str) -> dict:
    """
    Extract the `window.__INITIAL_STATE__` JSON blob from the changelog page.

    Uses raw_decode from the marker position so trailing JS (';', following
    statements) doesn't matter — we just need where the JSON object ends,
    which json.JSONDecoder figures out for us.
    """
    marker = 'window.__INITIAL_STATE__ = '
    idx = html.find(marker)
    if idx == -1:
        raise ValueError("Could not find window.__INITIAL_STATE__ in changelog page")

    decoder = json.JSONDecoder()
    data, _ = decoder.raw_decode(html, idx + len(marker))
    return data


def _find_release_trains(state: dict) -> list:
    """
    Find the `releaseTrains` array within the page state.

    Its exact location (article.content.children[0].attributes.releaseTrains)
    is an implementation detail of Stripe's docs site; search for the key
    instead of hardcoding the path so a markup reshuffle doesn't silently
    break us.
    """
    stack = [state]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            if 'releaseTrains' in node and isinstance(node['releaseTrains'], list):
                return node['releaseTrains']
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    return []


def _is_specific_enough(token: str) -> bool:
    """
    Reject candidate symbols too generic to search a codebase for.

    Stripe's `changed` field includes every casing variant of a symbol,
    which for common field names decomposes into bare English words (e.g.
    "url", "type", "session", "meter", "disable", "Processing" all show up
    as standalone entries alongside the real identifier). Matching those
    word-for-word against an arbitrary codebase is almost pure noise.

    Require the token to carry real identifier structure: an underscore,
a dot (dotted API path), or a genuine internal lowercase->uppercase
    transition (true camelCase/PascalCase compounding, e.g.
    "redirectToCheckout" or "PaymentMethodTypes"). A single capitalized
    word like "Processing" has no internal transition and is excluded —
    that's deliberate, not an oversight: it's indistinguishable from
    ordinary English at this point, and the specific variant (e.g. the
    snake_case or dotted form) is already covered separately.
    """
    if len(token) < 4:
        return False
    if '_' in token or '.' in token or '::' in token or '#' in token:
        return True
    return bool(re.search(r'[a-z][A-Z]', token))


def _extract_symbols(entry: dict) -> list:
    """
    Get candidate identifier strings for a changelog entry.

    Prefers the structured `changed` field (present on ~78% of breaking
    entries). Falls back to pulling backtick code spans out of the prose
    fields for entries where `changed` is empty. Either way, filters out
    tokens too generic to be useful search terms (see _is_specific_enough).
    """
    symbols = list(entry.get('changed') or [])

    if not symbols:
        prose = ' '.join(filter(None, [
            entry.get('description', ''),
            entry.get('breakingDescription', ''),
            entry.get('impact', ''),
        ]))
        for span in CODE_SPAN_RE.findall(prose):
            span = span.strip()
            if IDENTIFIER_RE.match(span):
                symbols.append(span)

    # Dedupe while preserving order, dropping overly generic tokens
    seen = set()
    deduped = []
    for s in symbols:
        if s not in seen and _is_specific_enough(s):
            seen.add(s)
            deduped.append(s)
    return deduped


def detect_code_usage(repo_path: str, change: dict) -> list:
    """
    Find code in a repository that uses any of a change's candidate symbols.

    Module-level (not a method) because it's fully generic — it only reads
    change['symbols']/['id']/['title'] and walks the filesystem, with no
    changelog-parsing dependency. Shared by both StripeChangeDetector and
    StripeSpecDetector (src/stripe_spec_monitor.py) rather than duplicated.

    Args:
        repo_path: Path to the repository to search
        change: Change dict — must have 'symbols'

    Returns:
        List of matches: {file, line, code, symbol, change_id, change_title}
    """
    matches = []
    symbols = [s for s in change.get('symbols', []) if s]

    if not symbols:
        logger.debug(f"No symbols to search for change: {change.get('title')}")
        return matches

    # Word-boundary regex per symbol (dots need escaping; identifiers can
    # contain them, e.g. "flow_data.type").
    patterns = [(s, re.compile(r'\b' + re.escape(s) + r'\b')) for s in symbols]

    for root, dirs, files in os.walk(repo_path):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]

        for file in files:
            if not any(file.endswith(ext) for ext in SOURCE_EXTENSIONS):
                continue

            file_path = os.path.join(root, file)
            try:
                with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                    for line_num, line in enumerate(f, 1):
                        for symbol, pattern in patterns:
                            if pattern.search(line):
                                matches.append({
                                    'file': file_path,
                                    'line': line_num,
                                    'code': line.strip(),
                                    'symbol': symbol,
                                    'change_id': change.get('id'),
                                    'change_title': change.get('title'),
                                })
            except Exception as e:
                logger.debug(f"Error reading {file_path}: {e}")

    if matches:
        logger.info(f"Found {len(matches)} usage(s) for change: {change.get('title')}")
    return matches


class StripeChangeDetector:
    """Detects breaking changes in the Stripe API and finds affected code."""

    def __init__(self, cache_file: str = CACHE_FILE):
        self.cache_file = cache_file
        self.seen_changes = {}
        self.load_seen_changes()

    def load_seen_changes(self):
        """Load previously processed change IDs (to avoid duplicate processing)."""
        try:
            if os.path.exists(self.cache_file):
                with open(self.cache_file, 'r') as f:
                    self.seen_changes = json.load(f)
                logger.info(f"Loaded {len(self.seen_changes)} cached changes")
        except Exception as e:
            logger.error(f"Failed to load changes cache: {e}")
            self.seen_changes = {}

    def save_seen_changes(self):
        """Persist seen change IDs to avoid re-processing on the next run."""
        try:
            with open(self.cache_file, 'w') as f:
                json.dump(self.seen_changes, f, indent=2)
        except Exception as e:
            logger.error(f"Failed to save changes cache: {e}")

    def fetch_changelog_entries(self) -> list:
        """
        Fetch and parse every changelog entry from docs.stripe.com/changelog.

        Returns:
            List of raw entry dicts, each tagged with '_release_date' (the
            release's publish date, YYYY-MM-DD) pulled from its parent release.
        """
        logger.info(f"Fetching Stripe changelog from {CHANGELOG_URL}")
        resp = requests.get(CHANGELOG_URL, timeout=30)
        resp.raise_for_status()

        state = _extract_initial_state(resp.text)
        trains = _find_release_trains(state)

        entries = []
        for train in trains:
            for release in train.get('releases', []):
                for entry in release.get('changelogEntries', []):
                    entry = dict(entry)  # don't mutate the parsed blob
                    entry['_release_date'] = release.get('published')
                    entries.append(entry)

        logger.info(f"Fetched {len(entries)} total changelog entries")
        return entries

    def check_changelog(self, days_back: int = 180) -> list:
        """
        Fetch the changelog and return breaking changes from the last N days,
        normalized into our internal change schema.

        Args:
            days_back: Only consider releases published within this many days.

        Returns:
            List of change dicts: {id, title, symbols, breaking, products,
            description, breaking_description, impact, release, release_date, url}
        """
        changes = []

        try:
            entries = self.fetch_changelog_entries()
        except Exception as e:
            logger.error(f"Failed to fetch Stripe changelog: {e}")
            return changes

        cutoff = (datetime.utcnow() - timedelta(days=days_back)).strftime('%Y-%m-%d')

        for entry in entries:
            if not entry.get('breaking'):
                continue

            release_date = entry.get('_release_date') or ''
            if release_date and release_date < cutoff:
                continue

            symbols = _extract_symbols(entry)
            title = entry.get('title')
            title = title[0] if isinstance(title, list) and title else (title or entry.get('slug', ''))

            changes.append({
                'id': entry.get('id') or entry.get('slug'),
                'title': title,
                'symbols': symbols,
                'breaking': True,
                'products': entry.get('products', []),
                'description': entry.get('description', ''),
                'breaking_description': entry.get('breakingDescription', ''),
                'impact': entry.get('impact', ''),
                'release': entry.get('release', ''),
                'release_date': release_date,
                'url': f"{CHANGELOG_URL}#{entry.get('slug', '')}",
            })

        # Most recent first
        changes.sort(key=lambda c: c.get('release_date') or '', reverse=True)
        logger.info(f"Found {len(changes)} breaking changes in the last {days_back} days")
        return changes

    def detect_code_usage(self, repo_path: str, change: dict) -> list:
        """Thin instance-method wrapper — see module-level detect_code_usage()."""
        return detect_code_usage(repo_path, change)

    def process_change(self, change: dict, repo_path: str = '.') -> Optional[dict]:
        """
        Attach code matches to a change, skipping ones we've already processed.

        Args:
            change: Change dict from check_changelog()
            repo_path: Repository to search for affected code

        Returns:
            The change dict with 'code_matches' added, or None if already
            processed or no code was affected.
        """
        change_id = change.get('id')
        if not change_id:
            logger.warning("Change missing 'id' field, skipping")
            return None

        if change_id in self.seen_changes:
            logger.debug(f"Skipping already-processed change: {change.get('title')}")
            return None

        matches = self.detect_code_usage(repo_path, change)

        if not matches:
            # Deliberately NOT cached: a change with no matches today might
            # match tomorrow if the repo starts using the affected symbol.
            # The full changelog scan is ~0.3s (fetch is the only expensive
            # part, and that happens once per run regardless), so re-checking
            # unmatched changes on every run costs nothing and is the only
            # way to catch newly-introduced usage. Caching this permanently
            # would silently suppress the exact case the tool exists to catch.
            logger.debug(f"No code usages found for {change.get('title')}")
            return None

        # Only matched changes are cached — once a fix has been generated
        # for a change, we don't want to regenerate it every run.
        self.seen_changes[change_id] = {
            'title': change.get('title'),
            'processed_at': datetime.utcnow().isoformat(),
            'matched': True,
        }
        change['code_matches'] = matches
        return change

    def get_pending_changes(self, repo_path: str = '.', days_back: int = 180) -> list:
        """
        Full pipeline: fetch breaking changes, find affected code, return
        only the ones that actually match something in the repo.

        Args:
            repo_path: Repository to scan for affected code
            days_back: How far back to look in the changelog

        Returns:
            List of changes with 'code_matches' populated
        """
        changes = self.check_changelog(days_back=days_back)
        pending = []

        for change in changes:
            processed = self.process_change(change, repo_path=repo_path)
            if processed:
                pending.append(processed)

        self.save_seen_changes()

        logger.info(f"{len(pending)} change(s) have matching code and need fixes")
        return pending
