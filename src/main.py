"""Main orchestration for the api-watchdog tool."""
import os
import sys
import logging
import argparse
from typing import Optional
from dotenv import load_dotenv

# Add src to path for imports
sys.path.insert(0, os.path.dirname(__file__))

from stripe_monitor import StripeChangeDetector
from claude_fixer import generate_fix_for_changes

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

load_dotenv()


def check_for_api_changes(repo_path: str = '.') -> list:
    """
    Check for API changes and find affected code.

    Args:
        repo_path: Path to the repository to analyze

    Returns:
        List of changes with detected code matches
    """
    logger.info(f"Checking for Stripe API changes in {repo_path}")

    detector = StripeChangeDetector()
    pending_changes = detector.get_pending_changes()

    if not pending_changes:
        logger.info("No new API changes detected")
        return []

    logger.info(f"Found {len(pending_changes)} changes requiring attention")
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
            logger.info(f"Skipping {change.get('method')} - no code matches")
            continue

        logger.info(f"Generating fix for {change.get('method')} ({len(matches)} matches)")

        try:
            fix_result = generate_fix_for_changes(change, matches)
            fixes.append(fix_result)
        except Exception as e:
            logger.error(f"Failed to generate fix for {change.get('method')}: {e}")

    logger.info(f"Generated {len(fixes)} fixes")
    return fixes


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
        print(f"Fix {i}: {change.get('method', 'Unknown')}")
        print(f"  Type: {change.get('type', 'unknown')}")
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
        '--verbose',
        action='store_true',
        help='Enable verbose logging'
    )

    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    try:
        # Check for changes
        changes = check_for_api_changes(args.repo)

        if not changes:
            logger.info("No action needed - repository is up to date")
            return 0

        # Generate fixes
        fixes = generate_fixes(changes)

        # Print summary
        print_fix_summary(fixes)

        if fixes:
            logger.info(f"Successfully generated {len(fixes)} fix(es)")
            logger.info("Review the fixes above and open a PR when ready")
            return 0
        else:
            logger.warning("No fixes could be generated")
            return 1

    except Exception as e:
        logger.error(f"Unexpected error: {e}", exc_info=True)
        return 1


if __name__ == '__main__':
    sys.exit(main())
