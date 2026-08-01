(() => {
  "use strict";

  const csrfToken = () => document.querySelector("[name=csrfmiddlewaretoken]")?.value || "";

  window.OperationalInbox = {
    async request(url, options = {}) {
      const response = await fetch(url, {
        credentials: "same-origin",
        ...options,
        headers: {
          Accept: "application/json",
          "X-CSRFToken": csrfToken(),
          "X-Requested-With": "XMLHttpRequest",
          ...(options.headers || {}),
        },
      });
      const payload = await response.json().catch(() => ({
        code: "invalid_response",
        message: "The server returned an unreadable response.",
      }));
      if (!response.ok) {
        const error = new Error(payload.message || "The request failed.");
        error.payload = payload;
        throw error;
      }
      return payload;
    },
  };

  document.addEventListener("click", (event) => {
    const trigger = event.target.closest("[data-dismiss]");
    if (trigger) document.querySelector(trigger.dataset.dismiss)?.remove();
  });
})();
