"""Generate fixes for detected API changes using Claude."""
import os
import json
import logging
from typing import Optional
from pathlib import Path
from anthropic import Anthropic

logger = logging.getLogger(__name__)

# Initialize Anthropic client
ANTHROPIC_API_KEY = os.getenv('ANTHROPIC_API_KEY')
if not ANTHROPIC_API_KEY:
    logger.warning("ANTHROPIC_API_KEY not set - Claude fix generation will not work")

client = Anthropic()


class ClaudeFixer:
    """Uses Claude to generate fixes for API changes."""

    def __init__(self):
        """Initialize the Claude fixer."""
        self.model = "claude-opus-5"  # Use the latest and most capable model
        self.conversation_history = []

    def read_code_context(self, file_path: str, line_num: int, context_lines: int = 5) -> str:
        """
        Read code around a detected change for context.

        Args:
            file_path: Path to the file
            line_num: Line number where change was detected
            context_lines: How many lines before/after to include

        Returns:
            Code snippet with context
        """
        try:
            with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                lines = f.readlines()

            start = max(0, line_num - context_lines - 1)
            end = min(len(lines), line_num + context_lines)

            context = []
            for i in range(start, end):
                prefix = ">>>" if i == line_num - 1 else "   "
                context.append(f"{prefix} {i+1:4d} | {lines[i].rstrip()}")

            return '\n'.join(context)
        except Exception as e:
            logger.error(f"Failed to read code context: {e}")
            return ""

    def generate_fix(self, change: dict, match: dict) -> Optional[str]:
        """
        Generate a fix for a specific code usage of a changed API.

        Args:
            change: The API change description
                   {
                       'method': 'handleCardPayment',
                       'type': 'removal',
                       'details': 'Use confirmCardPayment instead',
                   }
            match: The code match
                  {
                      'file': 'src/payments.py',
                      'line': 42,
                      'code': 'result = stripe.handleCardPayment(...)',
                      'method': 'handleCardPayment',
                  }

        Returns:
            Generated fix code (the replacement)
        """
        try:
            if not ANTHROPIC_API_KEY:
                logger.error("ANTHROPIC_API_KEY not set - cannot generate fix")
                return None

            file_path = match.get('file', '')
            line_num = match.get('line', 0)
            code_snippet = self.read_code_context(file_path, line_num)

            # Build the prompt
            prompt = f"""You are a code migration expert. An API change requires fixing this code.

API Change:
- Method: {change.get('method', 'unknown')}
- Type: {change.get('type', 'unknown')}
- Details: {change.get('details', 'See changelog')}

Code Location: {file_path}:{line_num}

Code Context (>>> marks the line to fix):
{code_snippet}

Your task:
1. Understand the API change
2. Generate a minimal fix (just the changed line(s))
3. Preserve all other code exactly as-is
4. Only output the fixed line(s), no explanation

Provide only the replacement code, nothing else."""

            logger.info(f"Generating fix for {change.get('method')} at {file_path}:{line_num}")

            # Call Claude
            response = client.messages.create(
                model=self.model,
                max_tokens=1000,
                messages=[
                    {"role": "user", "content": prompt}
                ]
            )

            fix = response.content[0].text
            logger.info(f"Generated fix:\n{fix}")
            return fix

        except Exception as e:
            logger.error(f"Failed to generate fix: {e}")
            return None

    def generate_full_diff(self, change: dict, matches: list) -> str:
        """
        Generate a complete diff/patch for all affected code.

        Args:
            change: The API change
            matches: List of all code matches that need fixing

        Returns:
            Git-style diff patch
        """
        diffs = []

        for match in matches:
            try:
                file_path = match.get('file', '')
                line_num = match.get('line', 0)

                # Generate fix for this match
                fix = self.generate_fix(change, match)
                if not fix:
                    logger.warning(f"Failed to generate fix for {file_path}:{line_num}")
                    continue

                # Read full file
                with open(file_path, 'r', encoding='utf-8') as f:
                    lines = f.readlines()

                # Build unified diff
                old_line = lines[line_num - 1] if line_num <= len(lines) else ""
                diff_section = f"""--- a/{file_path}
+++ b/{file_path}
@@ -{line_num},{len(old_line.splitlines())} +{line_num},{len(fix.splitlines())} @@
-{old_line.rstrip()}
+{fix.rstrip()}
"""
                diffs.append(diff_section)

            except Exception as e:
                logger.error(f"Failed to generate diff for match: {e}")
                continue

        return '\n'.join(diffs)

    def generate_pr_description(self, change: dict, matches: list) -> str:
        """
        Generate a PR description for the fix.

        Args:
            change: The API change
            matches: Code matches that were fixed

        Returns:
            PR description markdown
        """
        method = change.get('method', 'Unknown API')
        change_type = change.get('type', 'change')
        details = change.get('details', '')
        file_count = len(set(m.get('file') for m in matches))
        match_count = len(matches)

        description = f"""# Fix {method} API {change_type}

## What Changed
{details}

## What This PR Does
Updates {match_count} usages of `{method}` across {file_count} file(s) to use the new API.

## Files Changed
"""

        for file_path in sorted(set(m.get('file') for m in matches)):
            file_matches = [m for m in matches if m.get('file') == file_path]
            description += f"- `{file_path}` ({len(file_matches)} changes)\n"

        description += """
## Testing
- Verify the application builds
- Run all tests to ensure no regressions
- Manual testing recommended for payment flows

## Notes
This fix was generated automatically by api-watchdog using Claude.
Please review carefully before merging.
"""

        return description


def generate_fix_for_changes(change: dict, matches: list) -> dict:
    """
    Generate a complete fix including diff and PR description.

    Args:
        change: The API change to fix
        matches: All code matches that need fixing

    Returns:
        Dictionary with 'diff' and 'pr_description' keys
    """
    fixer = ClaudeFixer()

    return {
        'change': change,
        'diff': fixer.generate_full_diff(change, matches),
        'pr_description': fixer.generate_pr_description(change, matches),
        'affected_files': len(set(m.get('file') for m in matches)),
        'total_changes': len(matches),
    }
