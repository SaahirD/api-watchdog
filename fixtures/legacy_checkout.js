// Example legacy Stripe.js Checkout integration.
// This intentionally uses an API that Stripe has removed, so api-watchdog
// has something real to detect. See src/stripe_monitor.py.
const stripe = Stripe('pk_test_example');

function goToCheckout(sessionId) {
  return stripe.redirectToCheckout({ sessionId: sessionId });
}

module.exports = { goToCheckout };
