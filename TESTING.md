# Testing and Next Steps

## Phase 1: Webhook Testing ✅

### Current Status
- Flask webhook receiver: **Ready**
- GitHub App configured: **Ready**
- ngrok tunnel: **Working** (v3.39.11, auth configured)

### Test the Webhook Receiver

1. **Terminal 1: Start the Flask app**
   ```bash
   python src/github_app.py
   ```
   Expected output:
   ```
   Starting GitHub App webhook receiver (app_id=4733919)
   Listening on http://localhost:5000
   ```

2. **Terminal 2: Start ngrok tunnel**
   ```bash
   $env:Path = [System.Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path","User")
   ngrok http 5000
   ```
   Expected output:
   ```
   Forwarding    https://abc123.ngrok-free.app -> http://localhost:5000
   ```

3. **Terminal 3: Test the endpoints**
   ```bash
   # Health check
   curl https://abc123.ngrok-free.app/health
   
   # Expected: {"status": "ok", "app_id": "4733919"}
   ```

### Configure GitHub App Webhook URL
1. Go to your GitHub App settings: https://github.com/apps/[your-app-name]/settings
2. Set Webhook URL to: `https://[your-ngrok-url].ngrok-free.app/webhook`
3. Keep Webhook secret (GITHUB_WEBHOOK_SECRET in .env)
4. Subscribe to events:
   - **Push** (critical for change detection)
   - Pull request (for PR events)
   - Installation (for app setup)

### Test with Real Webhook
1. Trigger a push to the repo while the tunnel is active
2. Check Flask logs for:
   ```
   Processing push event
   Push to SaahirD/api-watchdog on refs/heads/main by [user] (X commits)
   ```

### Debugging If Webhooks Don't Arrive
1. **Check GitHub App Recent Deliveries:**
   - Go to GitHub App → Advanced → Recent Deliveries
   - Look for failed requests and error messages
   - Common issues:
     - Webhook URL is wrong (test with /health first)
     - Webhook secret doesn't match
     - Event subscriptions don't include "Push"

2. **Test webhook signature verification:**
   - Set GITHUB_WEBHOOK_SECRET in .env
   - Unsign a test webhook and compare

3. **Enable debug logging:**
   ```bash
   python src/github_app.py  # Already has DEBUG output built in
   ```

---

## Phase 2: Stripe Monitoring (In Progress)

### What Needs to Be Done

The `stripe_monitor.py` is a skeleton ready for implementation:

1. **Implement changelog fetching**
   ```python
   # In check_changelog(), replace TODO with:
   # - Fetch https://stripe.com/docs/changelog
   # - Parse for "Breaking change" entries
   # - Extract method names, types, details
   ```

2. **Integrate with Stripe API docs**
   ```python
   # Use https://api.stripe.com/docs for reference
   # Example: Look up handleCardPayment → confirmCardPayment
   ```

3. **Test code detection**
   ```bash
   python -c "
   from src.stripe_monitor import StripeChangeDetector
   detector = StripeChangeDetector()
   
   # Test with a sample change
   change = {'method': 'print', 'type': 'test'}
   matches = detector.detect_code_usage('.', change)
   print(f'Found {len(matches)} matches')
   "
   ```

### Test Data Structure
When changelog is wired up, changes will look like:
```python
{
    'method': 'handleCardPayment',
    'type': 'removal',
    'details': 'Removed in Stripe API v2024-01-01. Use confirmCardPayment instead.',
    'code_matches': [
        {
            'file': 'src/payments.py',
            'line': 42,
            'code': 'result = stripe.handleCardPayment(...)',
            'method': 'handleCardPayment',
            'change_type': 'removal',
        }
    ]
}
```

---

## Phase 3: Claude Integration (Ready)

### Current Status
- Claude fixer: **Implemented and ready**
- Anthropic API key: **Configured in .env**
- Model: **claude-opus-5** (latest and most capable)

### Test Fix Generation
```bash
python -c "
from src.stripe_monitor import StripeChangeDetector
from src.claude_fixer import generate_fix_for_changes

# Simulate a change
change = {
    'method': 'handleCardPayment',
    'type': 'removal',
    'details': 'Use confirmCardPayment instead'
}

# Simulate code match
match = {
    'file': 'test.py',
    'line': 1,
    'code': 'result = stripe.handleCardPayment(token)',
    'method': 'handleCardPayment'
}

# Generate fix (requires ANTHROPIC_API_KEY set)
result = generate_fix_for_changes(change, [match])
print('Diff:', result['diff'])
print('PR Description:', result['pr_description'])
"
```

---

## Phase 4: PR Creation (Not Yet Implemented)

Next after Stripe monitoring works:
- Use PyGithub to create PRs
- Create branches automatically
- Use git to commit and push fixes

---

## Running the Full Pipeline

Once everything is wired:
```bash
python src/main.py --repo . --verbose
```

This will:
1. Check Stripe changelog
2. Find code that needs fixing
3. Generate fixes using Claude
4. Create PR with changes

---

## Troubleshooting

### Flask App Won't Start
```bash
# Check if port 5000 is in use
netstat -ano | findstr 5000

# Or specify different port in github_app.py
```

### ngrok Tunnel Drops
```bash
# Recreate the tunnel
ngrok http 5000

# Or check if auth token is still valid
ngrok config check
```

### Webhook Signature Failures
```bash
# Verify GITHUB_WEBHOOK_SECRET in .env matches GitHub App
# Re-check GitHub App settings → Webhook secret
```

### Claude Generation Timeout
```bash
# Anthropic API calls have a timeout
# Check if ANTHROPIC_API_KEY is valid
# Try with --verbose flag to see detailed logs
```

---

## Files Reference

- `src/github_app.py` — Flask webhook receiver with signature verification
- `src/stripe_monitor.py` — Stripe changelog monitoring and code detection
- `src/claude_fixer.py` — Claude-powered fix generation
- `src/main.py` — Main orchestrator
- `requirements.txt` — Python dependencies
- `.env` — Local secrets (not committed)
- `CLAUDE.md` — Project context and current phase
- `README.md` — User-facing docs (includes ngrok setup)
- `TESTING.md` — This file
