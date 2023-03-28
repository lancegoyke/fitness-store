console.log("payments.js loaded");

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
    const button = document.querySelector("#submitButton");

    button.addEventListener("click", () => {
      // Get Checkout Session ID
      const url =
        "/payments/create-checkout-session/?" +
        new URLSearchParams({
          productSlug: button.dataset.productSlug,
          productType: button.dataset.productType,
        });
      //product-slug=${button.dataset.productSlug}&product-type=${button.dataset.productType};

      fetch(url)
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
