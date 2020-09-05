console.log("JavaScript in the house!");

// Get Stripe publishable key
// fetch() response is a ReadableStream
fetch("/payments/config/")
  .then((result) => {
    // result.json() returns a promise
    return result.json();
  })
  .then((data) => {
    // Initialize Stripe.js
    const stripe = Stripe(data.publicKey);

    // Event handler
    document.querySelector("#submitButton").addEventListener("click", () => {
      // Get Checkout Session ID
      fetch("/payments/create-checkout-session/")
        .then((result) => {
          return result.json();
        })
        .then((data) => {
          console.log(data);
          // Redirect to Stripe Checkout
          return stripe.redirectToCheckout({ sessionId: data.sessionId });
        })
        .then((res) => {
          console.log(res);
        });
    });
  });
