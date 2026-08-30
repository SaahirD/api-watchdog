"""Main orchestration for the api-watchdog tool."""
import os
import re
import sys
import logging
import argparse
import subprocess
from typing import Optional
from dotenv import load_dotenv

# Add src to path for imports
sys.path.insert(0, os.path.dirname(__file__))

# Must run before importing claude_fixer — it reads ANTHROPIC_API_KEY from
# the environment as soon as it's imported.
load_dotenv()

from stripe_monitor import StripeChangeDetector
from stripe_spec_monitor import StripeSpecDetector
from claude_fixer import generate_fix_for_changes

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


def check_for_api_changes(repo_path: str = '.', days_back: int = 180) -> list:
    """
    Check for API changes and find affected code, across both detection
    sources: the changelog scraper (StripeChangeDetector) and the OpenAPI
    spec-diff detector (StripeSpecDetector — see stripe_spec_monitor.py
    and ROADMAP.md's Phase 5 for why there are two).

    Args:
        repo_path: Path to the repository to analyze
        days_back: How many days of Stripe changelog history to check
                   (StripeSpecDetector ignores this — it has no rolling
                   time window, only "since its own last checkpoint")

    Returns:
        List of changes with detected code matches
    """
    logger.info(f"Checking for Stripe API changes in {repo_path}")

    # Each detector is independently isolated — one failing (e.g. GitHub
    # API down, oasdiff crash) must never block the other. Note
    # StripeChangeDetector.check_changelog() already swallows its own
    # fetch failures internally and returns []; this try/except is a
    # second layer for anything not already caught inside each detector.
    changelog_changes = []
    try:
        changelog_changes = StripeChangeDetector().get_pending_changes(repo_path=repo_path, days_back=days_back)
    except Exception as e:
        logger.error(f"Changelog detector failed: {e}", exc_info=True)

    spec_changes = []
    try:
        spec_changes = StripeSpecDetector().get_pending_changes(repo_path=repo_path, days_back=days_back)
    except Exception as e:
        logger.error(f"Spec-diff detector failed: {e}", exc_info=True)

    # Changelog runs first (higher precision — Stripe's own hand-curated
    # `changed` symbols) and "claims" the (file, line) pairs it matched.
    # Spec-diff matches at an already-claimed line are the same real-world
    # change, independently detected twice — drop them. This is a single-
    # run, cheap dedup, not a full reconciliation system: if the two
    # detectors fire on the same real change in *different* runs, two PRs
    # on two branches can still result (see CLAUDE.md's Known Limitations).
    claimed = {(m['file'], m['line']) for c in changelog_changes for m in c.get('code_matches', [])}
    deduped_spec_changes = []
    for change in spec_changes:
        remaining = [m for m in change.get('code_matches', []) if (m['file'], m['line']) not in claimed]
        if remaining:
            change['code_matches'] = remaining
            deduped_spec_changes.append(change)
        else:
            logger.info(f"Skipping spec-diff change already covered by the changelog detector: {change.get('title')}")

    pending_changes = changelog_changes + deduped_spec_changes

    if not pending_changes:
        logger.info("No new API changes detected")
        return []

    logger.info(
        f"Found {len(pending_changes)} changes requiring attention "
        f"({len(changelog_changes)} changelog, {len(deduped_spec_changes)} spec-diff)"
    )
    return pending_changes


def generate_fixes(pending_changes: list) -> list:
    """
    Generate fixes for all detected changes.

    Args:
        pending_changes: List of changes with code matches

    Returns:
        List of generated fixes
    """
    fixes = []

    for change in pending_changes:
        matches = change.get('code_matches', [])
        if not matches:
            logger.info(f"Skipping {change.get('title')} - no code matches")
            continue

        logger.info(f"Generating fix for {change.get('title')} ({len(matches)} matches)")

        try:
            fix_result = generate_fix_for_changes(change, matches)
            if fix_result.get('fixed_files'):
                fixes.append(fix_result)
            else:
                logger.warning(f"No usable fix produced for {change.get('title')}")
        except Exception as e:
            logger.error(f"Failed to generate fix for {change.get('title')}: {e}")

    logger.info(f"Generated {len(fixes)} fixes")
    return fixes


def detect_github_repo(repo_path: str = '.') -> Optional[str]:
    """
    Best-effort detection of "owner/repo" from the git remote, so
    --create-prs doesn't require typing it out for the common case.
    Returns None (not a guess) if it can't be determined.
    """
    try:
        result = subprocess.run(
            ['git', '-C', repo_path, 'remote', 'get-url', 'origin'],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode != 0:
            return None
        url = result.stdout.strip()
        # Handles both git@github.com:owner/repo.git and
        # https://github.com/owner/repo(.git)
        m = re.search(r'github\.com[:/]([^/]+)/([^/.]+?)(\.git)?/?$', url)
        return f"{m.group(1)}/{m.group(2)}" if m else None
    except Exception:
        return None


def print_fix_summary(fixes: list):
    """Print a summary of generated fixes."""
    if not fixes:
        print("No fixes generated")
        return

    print(f"\n{'='*60}")
    print(f"Generated {len(fixes)} Fix(es)")
    print(f"{'='*60}\n")

    for i, fix in enumerate(fixes, 1):
        change = fix.get('change', {})
        print(f"Fix {i}: {change.get('title', 'Unknown change')}")
        print(f"  Release: {change.get('release', 'unknown')}")
        print(f"  Files affected: {fix.get('affected_files', 0)}")
        print(f"  Total changes: {fix.get('total_changes', 0)}")
        print()

        if fix.get('pr_description'):
            print("PR Description:")
            print("-" * 40)
            print(fix['pr_description'])
            print()

        if fix.get('diff'):
            print("Diff:")
            print("-" * 40)
            print(fix['diff'])
            print()


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description='Watch for API changes and generate fixes'
    )
    parser.add_argument(
        '--repo',
        default='.',
        help='Path to repository to analyze (default: current directory)'
    )
    parser.add_argument(
        '--days-back',
        type=int,
        default=180,
        help='How many days of Stripe changelog history to check (default: 180)'
    )
    parser.add_argument(
        '--verbose',
        action='store_true',
        help='Enable verbose logging'
    )
    parser.add_argument(
        '--create-prs',
        action='store_true',
        help='Actually open GitHub pull requests for generated fixes (default: '
             'print/dry-run only). Requires the GitHub App to be installed on '
             'the target repo.'
    )
    parser.add_argument(
        '--github-repo',
        default=None,
        help='"owner/repo" to open PRs against. Auto-detected from the git '
             'remote if omitted.'
    )
    parser.add_argument(
        '--base-branch',
        default='main',
        help='Branch to open PRs against (default: main)'
    )

    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    try:
        # Check for changes
        changes = check_for_api_changes(args.repo, days_back=args.days_back)

        if not changes:
            logger.info("No action needed - repository is up to date")
            return 0

        # Generate fixes
        fixes = generate_fixes(changes)

        # Print summary
        print_fix_summary(fixes)

        if not fixes:
            logger.warning("No fixes could be generated")
            return 1

        logger.info(f"Successfully generated {len(fixes)} fix(es)")

        if not args.create_prs:
            logger.info("Review the fixes above and re-run with --create-prs when ready")
            return 0

        github_repo = args.github_repo or detect_github_repo(args.repo)
        if not github_repo:
            logger.error(
                "Could not determine target GitHub repo. Pass --github-repo owner/repo."
            )
            return 1

        owner, repo_name = github_repo.split('/', 1)
        logger.info(f"Opening PR(s) against {github_repo}@{args.base_branch}")

        from pr_creator import create_prs_for_fixes
        prs = create_prs_for_fixes(owner, repo_name, fixes, base_branch=args.base_branch)

        if not prs:
            logger.warning("No PRs were opened — check the logs above for why")
            return 1

        print(f"\nOpened {len(prs)} PR(s):")
        for pr in prs:
            print(f"  #{pr['number']}: {pr['url']}")

        return 0

    except Exception as e:
        logger.error(f"Unexpected error: {e}", exc_info=True)
        return 1


if __name__ == '__main__':
    sys.exit(main())
