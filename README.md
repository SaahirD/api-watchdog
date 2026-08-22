# api-watchdog
A tool that watches when an API you depend on (Stripe, Twilio, etc.) changes, finds the affected code in your repo, and opens a pull request with the fix before it breaks in production.