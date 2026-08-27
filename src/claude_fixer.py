"""Generate fixes for detected Stripe API changes using Claude."""
import os
import re
import shutil
import difflib
import tempfile
import subprocess
import logging
from collections import defaultdict
from typing import Optional
from anthropic import Anthropic

logger = logging.getLogger(__name__)

# How many lines of slack to give the model around a matched line. A fix
# that requires touching the enclosing signature (e.g. renaming a function's
# parameter to match a new API) needs more than the single matched line —
# see WINDOW_MARGIN's role in _windows_for_matches.
WINDOW_MARGIN = 2
# Extra lines of read-only context shown around the editable window, purely
# so the model understands surrounding code without being allowed to touch it.
DISPLAY_MARGIN = 3

# Deliberately NOT read at import time: whether ANTHROPIC_API_KEY is set
# depends on load_dotenv() having already run, and import order across
# callers isn't something this module should have to assume.
_client: Optional[Anthropic] = None


def _get_client() -> Optional[Anthropic]:
    global _client
    if _client is None:
        api_key = os.getenv('ANTHROPIC_API_KEY')
        if not api_key:
            logger.error("ANTHROPIC_API_KEY not set - cannot generate fix")
            return None
        _client = Anthropic(api_key=api_key)
    return _client

# Strips a ```lang\n...\n``` fence (or a bare ```...``` fence) from a model
# response, since we ask for code-only output but models often fence anyway.
FENCE_RE = re.compile(r'^```[a-zA-Z0-9_+-]*\n(.*?)\n?```$', re.DOTALL)


def _strip_fence(text: str) -> str:
    text = text.strip()
    m = FENCE_RE.match(text)
    return m.group(1) if m else text


def _windows_for_matches(matches: list, total_lines: int, margin: int = WINDOW_MARGIN) -> list:
    """
    Turn per-line matches into editable line ranges, merging any that overlap
    or sit adjacent to each other.

    A fix confined to exactly the matched line can't touch an enclosing
    function signature that also needs to change (e.g. renaming a parameter
    to access a field on it) — the model then either fails or produces a
    replacement that duplicates the untouched signature. Giving it a small
    window around each match lets it rewrite what actually needs rewriting.
    Merging overlapping windows avoids generating two conflicting edits for
    matches that are already close together.

    Args:
        matches: Match dicts (must have 'line') for a single file
        total_lines: Line count of that file, to clamp windows in range
        margin: Lines of slack before/after each match

    Returns:
        List of {'start', 'end', 'matches'} dicts, 1-indexed inclusive,
        sorted by start line ascending.
    """
    raw = []
    for m in matches:
        start = max(1, m['line'] - margin)
        end = min(total_lines, m['line'] + margin)
        raw.append({'start': start, 'end': end, 'matches': [m]})
    raw.sort(key=lambda w: w['start'])

    merged = []
    for w in raw:
        if merged and w['start'] <= merged[-1]['end'] + 1:
            merged[-1]['end'] = max(merged[-1]['end'], w['end'])
            merged[-1]['matches'].extend(w['matches'])
        else:
            merged.append(w)
    return merged


def _validate_syntax(file_path: str, content: str) -> tuple:
    """
    Best-effort syntax check on a patched file's full new content, so a
    broken fix never makes it into the diff silently.

    Args:
        file_path: Used only to pick a validator by extension
        content: The full proposed new file content

    Returns:
        (ok, error_message) — ok is True when valid OR when no validator
        is available for this file type (we don't fail closed on file
        types we have no way to check).
    """
    ext = os.path.splitext(file_path)[1]

    if ext == '.py':
        try:
            compile(content, file_path, 'exec')
            return True, ''
        except SyntaxError as e:
            return False, f"{e.msg} (line {e.lineno})"

    if ext in ('.js', '.jsx', '.ts', '.tsx') and shutil.which('node'):
        # TS/JSX aren't valid plain JS, but `node --check` still catches
        # gross structural breakage (unbalanced braces, dangling tokens),
        # which is the failure mode we're actually guarding against here.
        try:
            with tempfile.NamedTemporaryFile('w', suffix='.js', delete=False, encoding='utf-8') as tf:
                tf.write(content)
                tmp_path = tf.name
            result = subprocess.run(
                ['node', '--check', tmp_path],
                capture_output=True, text=True, timeout=10
            )
            os.unlink(tmp_path)
            if result.returncode != 0:
                return False, result.stderr.strip()
            return True, ''
        except Exception as e:
            logger.debug(f"node --check unavailable/failed to run: {e}")
            return True, ''  # don't fail closed on tooling problems

    return True, ''  # no validator for this file type


class ClaudeFixer:
    """Uses Claude to generate fixes for Stripe API changes."""

    def __init__(self, model: str = "claude-opus-5"):
        self.model = model

    def _read_lines(self, file_path: str) -> Optional[list]:
        try:
            with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                return f.readlines()
        except Exception as e:
            logger.error(f"Failed to read {file_path}: {e}")
            return None

    def generate_window_fix(self, change: dict, file_path: str, lines: list,
                             start: int, end: int, symbols: list) -> Optional[str]:
        """
        Ask Claude to rewrite one editable window (lines start..end, 1-indexed
        inclusive) within a file, given the real Stripe migration guidance.

        Args:
            change: Change dict from stripe_monitor (title, description,
                    breaking_description, impact, symbols)
            file_path: File being fixed (for logging/prompt context only)
            lines: Full file content as a list of lines (with newlines)
            start, end: The editable window, 1-indexed inclusive
            symbols: Which matched symbols fall inside this window

        Returns:
            Replacement text for exactly that window, or None on failure.
        """
        client = _get_client()
        if client is None:
            return None

        display_start = max(0, start - 1 - DISPLAY_MARGIN)
        display_end = min(len(lines), end + DISPLAY_MARGIN)

        context = []
        for i in range(display_start, display_end):
            marker = ">>>" if start <= i + 1 <= end else "   "
            context.append(f"{marker} {i+1:4d} | {lines[i].rstrip()}")
        code_snippet = '\n'.join(context)

        prompt = f"""You are a code migration expert fixing a breaking Stripe API change.

## Stripe changelog entry
Title: {change.get('title', 'unknown')}
What changed: {change.get('description', '')}
Breaking behavior: {change.get('breaking_description', '')}
Required migration: {change.get('impact', '')}
Reference: {change.get('url', '')}

## Code to fix
File: {file_path}
Matched symbol(s): {', '.join(symbols)}
Editable range: lines {start}-{end} (marked with >>> below; unmarked lines are
read-only context — do not repeat or re-emit them)

{code_snippet}

## Task
Return the complete replacement for ONLY lines {start}-{end}, rewritten to
comply with the migration guidance above. You may change the number of
lines. Preserve indentation and style consistent with the surrounding code.
Output ONLY the replacement code — no explanation, no markdown fence, no
commentary."""

        logger.info(f"Generating fix for {change.get('title')} at {file_path}:{start}-{end}")

        try:
            response = client.messages.create(
                model=self.model,
                max_tokens=1500,
                messages=[{"role": "user", "content": prompt}]
            )
            # response.content can include non-text blocks first (e.g.
            # ThinkingBlock for extended-thinking models) — pull out the
            # text blocks specifically rather than assuming content[0].
            text_parts = [b.text for b in response.content if b.type == 'text']
            if not text_parts:
                logger.error(f"No text block in Claude response (block types: {[b.type for b in response.content]})")
                return None
            fix = _strip_fence(''.join(text_parts))
            logger.debug(f"Generated fix:\n{fix}")
            return fix
        except Exception as e:
            logger.error(f"Failed to generate fix: {e}")
            return None

    def generate_full_diff(self, change: dict, matches: list) -> str:
        """
        Generate a real unified diff covering every matched file.

        Groups matches by file, merges nearby matches into shared editable
        windows (see _windows_for_matches), asks Claude for a replacement
        per window, splices those into an in-memory copy of the file, checks
        the result still parses (see _validate_syntax), and diffs old vs.
        new with difflib.

        Args:
            change: The Stripe change being fixed
            matches: All code matches for this change

        Returns:
            Unified diff text covering all affected, successfully-validated files
        """
        by_file = defaultdict(list)
        for m in matches:
            by_file[m['file']].append(m)

        diffs = []
        for file_path, file_matches in by_file.items():
            original_lines = self._read_lines(file_path)
            if original_lines is None:
                continue

            windows = _windows_for_matches(file_matches, len(original_lines))
            new_lines = list(original_lines)

            # Apply bottom-to-top so earlier windows' line numbers stay
            # valid as later (higher-numbered) windows are spliced in place.
            any_failed = False
            for window in sorted(windows, key=lambda w: w['start'], reverse=True):
                symbols = sorted({m['symbol'] for m in window['matches']})
                fix = self.generate_window_fix(
                    change, file_path, original_lines,
                    window['start'], window['end'], symbols
                )
                if fix is None:
                    logger.warning(f"Skipping {file_path}:{window['start']}-{window['end']} — fix generation failed")
                    any_failed = True
                    continue

                start_idx, end_idx = window['start'] - 1, window['end']
                had_newline = new_lines[end_idx - 1].endswith('\n')
                fixed_lines = fix.splitlines()
                replacement = [
                    (line + '\n') if (had_newline or i < len(fixed_lines) - 1) else line
                    for i, line in enumerate(fixed_lines)
                ]
                new_lines[start_idx:end_idx] = replacement

            if new_lines == original_lines:
                if any_failed:
                    logger.warning(f"No usable fix produced for {file_path}")
                continue

            new_content = ''.join(new_lines)
            ok, error = _validate_syntax(file_path, new_content)
            if not ok:
                logger.error(f"Generated fix for {file_path} fails to parse, discarding: {error}")
                continue

            diff = difflib.unified_diff(
                original_lines, new_lines,
                fromfile=f"a/{file_path}", tofile=f"b/{file_path}",
            )
            diffs.append(''.join(diff))

        return '\n'.join(diffs)

    def generate_pr_description(self, change: dict, matches: list) -> str:
        """Generate a PR description explaining the fix."""
        title = change.get('title', 'Stripe API change')
        file_count = len(set(m.get('file') for m in matches))
        match_count = len(matches)

        description = f"""# Fix: {title}

## What changed in Stripe's API
{change.get('description', '')}

## Why this breaks your code
{change.get('breaking_description', '')}

## Migration guidance (from Stripe)
{change.get('impact', '')}

Reference: {change.get('url', '')}

## What this PR does
Updates {match_count} usage(s) across {file_count} file(s) to match the new API.

## Files changed
"""
        for file_path in sorted(set(m.get('file') for m in matches)):
            file_matches = [m for m in matches if m.get('file') == file_path]
            description += f"- `{file_path}` ({len(file_matches)} change(s))\n"

        description += """
## Testing
- Verify the application builds
- Run the test suite
- Manual testing recommended for payment flows

---
This fix was generated automatically by api-watchdog using Claude, based on
Stripe's own changelog and migration guidance. **Please review carefully
before merging** — this tool defaults to hold-for-review, never auto-merge.
"""
        return description


def generate_fix_for_changes(change: dict, matches: list) -> dict:
    """
    Generate a complete fix package: diff + PR description.

    Args:
        change: The Stripe change to fix
        matches: All code matches for this change

    Returns:
        {'change', 'diff', 'pr_description', 'affected_files', 'total_changes'}
    """
    fixer = ClaudeFixer()
    return {
        'change': change,
        'diff': fixer.generate_full_diff(change, matches),
        'pr_description': fixer.generate_pr_description(change, matches),
        'affected_files': len(set(m.get('file') for m in matches)),
        'total_changes': len(matches),
    }
