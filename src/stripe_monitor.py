"""Monitor Stripe changelog for breaking API changes."""
import os
import json
import logging
import hashlib
from datetime import datetime
from typing import Optional
import requests

logger = logging.getLogger(__name__)

# Stripe API URLs
STRIPE_CHANGELOG_URL = "https://stripe.com/docs/changelog/api"
STRIPE_API_REFERENCE_URL = "https://api.stripe.com/docs"


class StripeChangeDetector:
    """Detects breaking changes in Stripe API."""

    # Common breaking change patterns (method removals, parameter changes, etc.)
    BREAKING_PATTERNS = {
        'deprecated': r'(deprecated|no longer supported)',
        'removed': r'(removed|no longer available)',
        'replaced': r'(replaced with|use .* instead)',
        'changed': r'(parameter .* changed|argument .* changed)',
    }

    def __init__(self):
        """Initialize the Stripe change detector."""
        self.seen_changes = {}
        self.load_seen_changes()

    def load_seen_changes(self):
        """Load previously seen changes from cache (to avoid duplicate processing)."""
        try:
            if os.path.exists('.stripe_changes_cache.json'):
                with open('.stripe_changes_cache.json', 'r') as f:
                    self.seen_changes = json.load(f)
                logger.info(f"Loaded {len(self.seen_changes)} cached changes")
        except Exception as e:
            logger.error(f"Failed to load changes cache: {e}")
            self.seen_changes = {}

    def save_seen_changes(self):
        """Persist seen changes to avoid re-processing."""
        try:
            with open('.stripe_changes_cache.json', 'w') as f:
                json.dump(self.seen_changes, f, indent=2)
        except Exception as e:
            logger.error(f"Failed to save changes cache: {e}")

    def get_change_hash(self, change_text: str) -> str:
        """Generate a hash of a change to detect duplicates."""
        return hashlib.sha256(change_text.encode()).hexdigest()

    def check_changelog(self) -> list:
        """
        Fetch and parse Stripe changelog for breaking changes.

        Returns:
            List of detected breaking changes with metadata
        """
        changes = []

        try:
            # Note: In production, you'd scrape the actual Stripe changelog page
            # or use the Stripe API. For now, we return a structure ready for data.
            logger.info("Checking Stripe changelog for breaking changes...")

            # TODO: Implement actual changelog fetching from Stripe
            # This would involve:
            # 1. Fetching https://stripe.com/docs/changelog
            # 2. Parsing for "Breaking change" entries
            # 3. Extracting method names, parameters, etc.
            # 4. Cross-referencing with the API reference

            # For now, return empty - this will be populated once we add
            # the actual changelog scraping logic
            logger.info("Changelog check complete - 0 new changes detected")

        except Exception as e:
            logger.error(f"Failed to check Stripe changelog: {e}")

        return changes

    def detect_code_usage(self, repo_path: str, change: dict) -> list:
        """
        Find code in a repository that uses the changed Stripe API.

        Args:
            repo_path: Path to the repository to search
            change: Dictionary describing the API change
                   {
                       'method': 'handleCardPayment',
                       'type': 'removal',  # removal, parameter_change, deprecation
                       'details': 'Use confirmCardPayment instead',
                   }

        Returns:
            List of matches with file paths and line numbers
        """
        matches = []

        try:
            method_name = change.get('method', '')
            if not method_name:
                logger.warning("Change missing 'method' field")
                return matches

            logger.info(f"Searching for usages of {method_name}...")

            # Search for method calls in Python, JavaScript, etc.
            patterns = [
                f"{method_name}(",  # Direct calls
                f"stripe.{method_name}",  # Stripe SDK calls
                f"\\.{method_name}",  # Chained calls
            ]

            for root, dirs, files in os.walk(repo_path):
                # Skip common non-source directories
                dirs[:] = [d for d in dirs if d not in {'.git', '__pycache__', 'node_modules', '.venv', 'venv'}]

                for file in files:
                    # Only scan source files
                    if not any(file.endswith(ext) for ext in {'.py', '.js', '.ts', '.jsx', '.tsx', '.rb', '.java'}):
                        continue

                    file_path = os.path.join(root, file)
                    try:
                        with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                            for line_num, line in enumerate(f, 1):
                                for pattern in patterns:
                                    if pattern in line:
                                        matches.append({
                                            'file': file_path,
                                            'line': line_num,
                                            'code': line.strip(),
                                            'method': method_name,
                                            'change_type': change.get('type', 'unknown'),
                                        })
                                        logger.debug(f"Found {method_name} at {file_path}:{line_num}")
                    except Exception as e:
                        logger.debug(f"Error reading {file_path}: {e}")

        except Exception as e:
            logger.error(f"Failed to detect code usage: {e}")

        logger.info(f"Found {len(matches)} usages of {change.get('method', 'unknown')}")
        return matches

    def process_change(self, change: dict) -> dict:
        """
        Process a detected change and prepare it for fix generation.

        Args:
            change: Change dictionary

        Returns:
            Processed change with code locations and metadata
        """
        change_hash = self.get_change_hash(json.dumps(change))

        # Skip if we've already processed this change
        if change_hash in self.seen_changes:
            logger.info(f"Skipping already-processed change: {change.get('method')}")
            return None

        # Find code that needs fixing
        change['code_matches'] = self.detect_code_usage('.', change)

        if not change['code_matches']:
            logger.info(f"No code usages found for {change.get('method')}")
            return None

        # Mark as processed
        change['processed_at'] = datetime.utcnow().isoformat()
        self.seen_changes[change_hash] = change

        logger.info(f"Processed change: {change.get('method')} with {len(change['code_matches'])} matches")
        return change

    def get_pending_changes(self) -> list:
        """
        Get all pending changes that need fixes.

        Returns:
            List of changes with code locations
        """
        changes = self.check_changelog()
        pending = []

        for change in changes:
            processed = self.process_change(change)
            if processed:
                pending.append(processed)

        # Persist the cache
        self.save_seen_changes()

        logger.info(f"Found {len(pending)} pending changes requiring fixes")
        return pending
