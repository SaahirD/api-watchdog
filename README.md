# api-watchdog
A tool that watches when an API you depend on (Stripe, Twilio, etc.) changes, finds the affected code in your repo, and opens a pull request with the fix before it breaks in production.

## Local Development

### Webhook Testing with ngrok

`src/github_app.py` runs a Flask server on port 5000 that receives GitHub webhook events. To test against real GitHub webhook deliveries locally, expose your local port to the internet using **ngrok**:

1. **Start the Flask webhook receiver:**
   ```bash
   python src/github_app.py
   ```
   The server will listen on `http://localhost:5000/webhook`

2. **In another terminal, start an ngrok tunnel:**
   ```bash
   ngrok http 5000
   ```
   ngrok will print a forwarding URL like `https://abc123-456-def.ngrok-free.app`

3. **Configure GitHub App webhook:**
   - Use the ngrok URL as your webhook endpoint: `https://abc123-456-def.ngrok-free.app/webhook`
   - Set the events and secret, then save
   - GitHub will begin delivering webhooks to your local machine

**Note:** ngrok free tier tunnels expire after a period of inactivity. For persistent testing, [sign up for an ngrok account](https://ngrok.com), obtain an authtoken, and run:
```bash
ngrok config add-authtoken <your-auth-token>
```