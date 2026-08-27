// Example legacy Stripe.js Checkout integration.
// This intentionally uses an API that Stripe has removed, so api-watchdog
// has something real to detect. See src/stripe_monitor.py.
const stripe = Stripe('pk_test_example');
function goToCheckout(session) {
  // Stripe removed stripe.redirectToCheckout. Create the Checkout Session on
  // your server and redirect the browser to the session's `url`.
  const url = typeof session === 'string' ? session : session && session.url;

  if (!url) {
    throw new Error('goToCheckout requires a Checkout Session URL (session.url).');
  }

  window.location.href = url;
}
module.exports = { goToCheckout };
