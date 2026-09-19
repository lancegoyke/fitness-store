/* Meso — rewrite billing dates onto the viewer's local timezone (#555).
 * The server renders UTC; Stripe charges at an exact instant, so a coach west
 * of UTC needs the browser-local date, matching Stripe's own Checkout page.
 */
(function () {
  document.querySelectorAll("time[data-local-date]").forEach((el) => {
    const parsed = new Date(el.getAttribute("datetime"));
    if (isNaN(parsed.getTime())) return;
    el.textContent = parsed.toLocaleDateString("en-US", {
      month: "short",
      day: "numeric",
    });
  });
})();
