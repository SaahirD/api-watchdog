// Example legacy Stripe.js Checkout integration.
// This intentionally uses an API that Stripe has removed, so api-watchdog
// has something real to detect. See src/stripe_monitor.py.
const stripe = Stripe('pk_test_example');
function goToCheckout(sessionUrl) {
  // `stripe.redirectToCheckout` is no longer supported. Create the Checkout
  // Session on your server and redirect the browser to `session.url`.
  window.location.href = sessionUrl;
}
module.exports = { goToCheckout };
