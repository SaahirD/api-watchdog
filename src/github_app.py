"""GitHub App webhook receiver for detecting repository changes."""
import os
import json
import hmac
import hashlib
import logging
from flask import Flask, request, jsonify
from dotenv import load_dotenv

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

load_dotenv()

app = Flask(__name__)

# GitHub webhook configuration
GITHUB_WEBHOOK_SECRET = os.getenv('GITHUB_WEBHOOK_SECRET', '').encode()
GITHUB_APP_ID = os.getenv('GITHUB_APP_ID')


def verify_webhook_signature(request_body: bytes, signature_header: str) -> bool:
    """
    Verify that the webhook request came from GitHub using the webhook secret.

    GitHub sends an X-Hub-Signature-256 header with HMAC-SHA256 of the payload.
    """
    if not GITHUB_WEBHOOK_SECRET:
        logger.warning("GITHUB_WEBHOOK_SECRET not set - skipping signature verification")
        return True

    if not signature_header:
        logger.error("No signature header found in webhook request")
        return False

    # Extract the algorithm and signature
    try:
        algorithm, signature = signature_header.split('=', 1)
    except ValueError:
        logger.error(f"Invalid signature format: {signature_header}")
        return False

    if algorithm != 'sha256':
        logger.error(f"Unsupported signature algorithm: {algorithm}")
        return False

    # Compute expected signature
    expected_signature = hmac.new(
        GITHUB_WEBHOOK_SECRET,
        request_body,
        hashlib.sha256
    ).hexdigest()

    # Compare signatures (constant-time comparison to prevent timing attacks)
    if not hmac.compare_digest(signature, expected_signature):
        logger.error("Webhook signature verification failed")
        return False

    logger.debug("Webhook signature verified")
    return True


@app.route('/health', methods=['GET'])
def health():
    """Health check endpoint to verify the app is running."""
    return jsonify({'status': 'ok', 'app_id': GITHUB_APP_ID}), 200


@app.route('/webhook', methods=['POST'])
def webhook():
    """
    Receive and process GitHub webhook events.

    Supported events:
    - push: Code pushed to repository
    - pull_request: PR opened, closed, or updated
    - installation: GitHub App installed or uninstalled
    """
    logger.info("Received webhook request")

    # Get the raw request body for signature verification
    request_body = request.get_data()

    # Verify webhook signature
    signature_header = request.headers.get('X-Hub-Signature-256', '')
    if not verify_webhook_signature(request_body, signature_header):
        logger.error("Webhook verification failed - rejecting request")
        return jsonify({'error': 'Unauthorized'}), 401

    # Parse JSON payload
    try:
        payload = request.get_json()
    except Exception as e:
        logger.error(f"Failed to parse webhook payload: {e}")
        return jsonify({'error': 'Invalid JSON'}), 400

    if not payload:
        logger.warning("Received empty webhook payload")
        return jsonify({'error': 'Empty payload'}), 400

    event_type = request.headers.get('X-GitHub-Event', 'unknown')
    logger.info(f"Processing {event_type} event")

    try:
        # Route to appropriate handler based on event type
        if event_type == 'push':
            return handle_push_event(payload)
        elif event_type == 'pull_request':
            return handle_pull_request_event(payload)
        elif event_type == 'installation':
            return handle_installation_event(payload)
        else:
            logger.info(f"Ignoring unhandled event type: {event_type}")
            return jsonify({'status': 'received', 'action': 'ignored'}), 200

    except Exception as e:
        logger.error(f"Error processing {event_type} event: {e}", exc_info=True)
        return jsonify({'error': 'Processing failed'}), 500


def handle_push_event(payload: dict) -> tuple:
    """Handle push events - detect code changes that may need fixes."""
    try:
        repo_name = payload.get('repository', {}).get('full_name', 'unknown')
        ref = payload.get('ref', 'unknown')
        pusher = payload.get('pusher', {}).get('name', 'unknown')
        commits = payload.get('commits', [])

        logger.info(f"Push to {repo_name} on {ref} by {pusher} ({len(commits)} commits)")

        # TODO: Check commits for API changes using stripe_monitor
        # TODO: If changes detected, trigger claude_fixer to generate PR

        return jsonify({
            'status': 'received',
            'event': 'push',
            'repository': repo_name,
            'ref': ref,
            'commits': len(commits)
        }), 200

    except Exception as e:
        logger.error(f"Error in handle_push_event: {e}", exc_info=True)
        raise


def handle_pull_request_event(payload: dict) -> tuple:
    """Handle pull request events."""
    try:
        action = payload.get('action', 'unknown')
        pr = payload.get('pull_request', {})
        repo_name = payload.get('repository', {}).get('full_name', 'unknown')

        logger.info(f"PR {action} on {repo_name}: {pr.get('title', 'unknown')}")

        # TODO: Handle PR events (e.g., validate fixes, re-trigger analysis)

        return jsonify({
            'status': 'received',
            'event': 'pull_request',
            'action': action,
            'repository': repo_name
        }), 200

    except Exception as e:
        logger.error(f"Error in handle_pull_request_event: {e}", exc_info=True)
        raise


def handle_installation_event(payload: dict) -> tuple:
    """Handle GitHub App installation events."""
    try:
        action = payload.get('action', 'unknown')
        app_id = payload.get('installation', {}).get('app_id')
        repos = payload.get('repositories', [])

        logger.info(f"App installation {action}: app_id={app_id}, repos={len(repos)}")

        # TODO: Store app installation info in database

        return jsonify({
            'status': 'received',
            'event': 'installation',
            'action': action,
            'repositories': len(repos)
        }), 200

    except Exception as e:
        logger.error(f"Error in handle_installation_event: {e}", exc_info=True)
        raise


if __name__ == '__main__':
    logger.info(f"Starting GitHub App webhook receiver (app_id={GITHUB_APP_ID})")
    logger.info("Listening on http://localhost:5000")
    logger.info("Webhook endpoint: http://localhost:5000/webhook")
    logger.info("Health check: http://localhost:5000/health")
    app.run(port=5000, debug=True)
