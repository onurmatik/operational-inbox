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

  const initDomainCreate = () => {
    const form = document.querySelector("[data-domain-create]");
    if (!form || form.dataset.hasErrors === "true") return;

    const hostnameInput = form.querySelector("[name=hostname]");
    const checkButton = form.querySelector("[data-mx-check]");
    const submitButton = form.querySelector("[data-domain-submit]");
    const choices = form.querySelector("[data-mx-choices]");
    const choiceHelp = form.querySelector("[data-mx-choice-help]");
    const alternativeButton = form.querySelector("[data-show-alternative]");
    const overrideWarning = form.querySelector("[data-override-warning]");
    const status = form.querySelector("#mx-inspection-status");
    const statusTitle = form.querySelector("[data-mx-status-title]");
    const statusBody = form.querySelector("[data-mx-status-body]");
    const statusRecords = form.querySelector("[data-mx-records]");
    const cards = [...form.querySelectorAll("[data-setup-card]")];
    const radios = [...form.querySelectorAll("input[name=setup_mode]")];
    const examples = [...form.querySelectorAll("[data-domain-example]")];
    const requiredElements = [
      hostnameInput,
      checkButton,
      submitButton,
      choices,
      choiceHelp,
      alternativeButton,
      overrideWarning,
      status,
      statusTitle,
      statusBody,
      statusRecords,
    ];
    if (requiredElements.some((element) => !element) || !cards.length || !radios.length) return;

    const DIRECT_MX = "DIRECT_MX";
    const PROVIDER_FORWARD = "PROVIDER_FORWARD";
    const supportedModes = new Set([DIRECT_MX, PROVIDER_FORWARD]);
    const defaultChoiceHelp = choiceHelp.textContent.trim();
    let inspectedInputValue = "";
    let recommendedMode = "";
    let requestSequence = 0;
    let controller = null;

    const inputValue = () => hostnameInput.value.trim().replace(/\.$/, "");

    const updateExamples = () => {
      const hostname = inputValue() || "yourcompany.com";
      examples.forEach((example) => {
        example.textContent = `requests@${hostname}`;
      });
    };

    const setStatus = ({ kind, title, body, records = "" }) => {
      const palette =
        kind === "error"
          ? "border-coral-strong/40 bg-coral-soft text-coral-strong"
          : "border-blue-strong/40 bg-blue-soft text-blue-strong";
      status.className = `mt-5 border p-4 ${palette}`;
      status.setAttribute("role", kind === "error" ? "alert" : "status");
      statusTitle.textContent = title;
      statusBody.textContent = body;
      statusRecords.textContent = records;
      statusRecords.classList.toggle("hidden", !records);
    };

    const clearSelection = () => {
      radios.forEach((radio) => {
        radio.checked = false;
      });
      cards.forEach((card) => card.classList.remove("hidden"));
      form.querySelectorAll("[data-recommendation]").forEach((badge) => {
        badge.classList.add("hidden");
      });
    };

    const resetInspection = ({ abort = true } = {}) => {
      if (abort && controller) controller.abort();
      controller = null;
      requestSequence += 1;
      inspectedInputValue = "";
      recommendedMode = "";
      form.setAttribute("aria-busy", "false");
      status.classList.add("hidden");
      choices.classList.add("hidden");
      submitButton.classList.add("hidden");
      alternativeButton.classList.add("hidden");
      alternativeButton.setAttribute("aria-expanded", "false");
      overrideWarning.classList.add("hidden");
      checkButton.classList.remove("hidden");
      checkButton.disabled = false;
      checkButton.textContent = "Check MX records";
      choiceHelp.textContent = defaultChoiceHelp;
      clearSelection();
    };

    const updateOverrideWarning = () => {
      const selectedMode = radios.find((radio) => radio.checked)?.value || "";
      if (selectedMode) {
        submitButton.textContent =
          selectedMode === PROVIDER_FORWARD
            ? "Continue with current provider"
            : "Continue with direct routing";
      }
      const isOverride = recommendedMode && selectedMode && selectedMode !== recommendedMode;
      if (!isOverride) {
        overrideWarning.classList.add("hidden");
        overrideWarning.textContent = "";
        return;
      }
      overrideWarning.textContent =
        recommendedMode === PROVIDER_FORWARD
          ? "Existing MX records were detected. Direct routing will replace the current mail delivery path when you update DNS."
          : "No existing mail provider was detected. Provider forwarding only works after you configure a provider with catch-all forwarding.";
      overrideWarning.classList.remove("hidden");
    };

    const revealRecommendation = (mode) => {
      recommendedMode = mode;
      radios.forEach((radio) => {
        radio.checked = radio.value === mode;
      });
      cards.forEach((card) => {
        card.classList.toggle("hidden", card.dataset.setupCard !== mode);
      });
      form.querySelectorAll("[data-recommendation]").forEach((badge) => {
        badge.classList.toggle("hidden", badge.dataset.recommendation !== mode);
      });
      choices.classList.remove("hidden");
      choiceHelp.textContent =
        "We selected the safest default based on public MX records. Review it or choose a different setup.";
      alternativeButton.classList.remove("hidden");
      alternativeButton.setAttribute("aria-expanded", "false");
      submitButton.textContent =
        mode === PROVIDER_FORWARD
          ? "Continue with current provider"
          : "Continue with direct routing";
      submitButton.classList.remove("hidden");
      checkButton.classList.add("hidden");
      updateOverrideWarning();
    };

    const formatRecords = (records) => {
      const visibleRecords = records
        .slice(0, 3)
        .map((record) => `${record.preference} ${record.exchange}`)
        .join(" · ");
      const remainder = records.length > 3 ? ` · +${records.length - 3} more` : "";
      return visibleRecords ? `Detected MX: ${visibleRecords}${remainder}` : "";
    };

    const checkMx = async () => {
      const requestedValue = inputValue();
      if (!requestedValue) {
        resetInspection();
        setStatus({
          kind: "error",
          title: "Enter a domain first.",
          body: "Use a domain such as example.com or a dedicated subdomain such as inbox.example.com.",
        });
        hostnameInput.focus();
        return;
      }

      if (controller) controller.abort();
      controller = new AbortController();
      const activeController = controller;
      const activeSequence = ++requestSequence;
      inspectedInputValue = "";
      recommendedMode = "";
      clearSelection();
      choices.classList.add("hidden");
      submitButton.classList.add("hidden");
      alternativeButton.classList.add("hidden");
      overrideWarning.classList.add("hidden");
      checkButton.classList.remove("hidden");
      checkButton.disabled = true;
      checkButton.textContent = "Checking MX records…";
      form.setAttribute("aria-busy", "true");
      setStatus({
        kind: "info",
        title: `Checking MX records for ${requestedValue}…`,
        body: "This usually takes a few seconds. Nothing is being changed.",
      });

      try {
        const payload = await window.OperationalInbox.request(form.dataset.mxInspectUrl, {
          method: "POST",
          body: new URLSearchParams({ hostname: requestedValue }),
          signal: activeController.signal,
        });
        const responseIsValid =
          payload &&
          typeof payload.hostname === "string" &&
          typeof payload.has_existing_mx === "boolean" &&
          supportedModes.has(payload.recommended_setup_mode) &&
          payload.has_existing_mx ===
            (payload.recommended_setup_mode === PROVIDER_FORWARD) &&
          Array.isArray(payload.mx_records) &&
          payload.mx_records.every(
            (record) =>
              Number.isInteger(record.preference) && typeof record.exchange === "string"
          );
        if (!responseIsValid) throw new Error("The server returned an unreadable MX result.");
        if (activeSequence !== requestSequence || inputValue() !== requestedValue) return;

        inspectedInputValue = requestedValue;
        revealRecommendation(payload.recommended_setup_mode);
        if (payload.has_existing_mx) {
          setStatus({
            kind: "result",
            title: "Existing mail service found",
            body:
              "Keeping these MX records avoids interrupting existing mailboxes. Your current provider is the recommended setup.",
            records: formatRecords(payload.mx_records),
          });
        } else {
          setStatus({
            kind: "result",
            title: "No public MX records found",
            body: `We did not detect an existing mail provider for ${payload.hostname}. Direct routing is the recommended setup.`,
          });
        }
      } catch (error) {
        if (error.name === "AbortError" || activeSequence !== requestSequence) return;
        inspectedInputValue = "";
        recommendedMode = "";
        clearSelection();
        choices.classList.add("hidden");
        submitButton.classList.add("hidden");
        alternativeButton.classList.add("hidden");
        setStatus({
          kind: "error",
          title: `We could not check MX records for ${requestedValue}.`,
          body: `${error.message} Nothing was changed; try again.`,
        });
        checkButton.classList.remove("hidden");
        checkButton.disabled = false;
        checkButton.textContent = "Try again";
      } finally {
        if (activeSequence === requestSequence) {
          form.setAttribute("aria-busy", "false");
          controller = null;
        }
      }
    };

    alternativeButton.addEventListener("click", () => {
      cards.forEach((card) => card.classList.remove("hidden"));
      alternativeButton.classList.add("hidden");
      alternativeButton.setAttribute("aria-expanded", "true");
    });
    radios.forEach((radio) => radio.addEventListener("change", updateOverrideWarning));
    checkButton.addEventListener("click", checkMx);
    hostnameInput.addEventListener("input", () => {
      updateExamples();
      resetInspection();
    });
    form.addEventListener("submit", (event) => {
      if (form.getAttribute("aria-busy") === "true") {
        event.preventDefault();
        return;
      }
      if (!inspectedInputValue || inspectedInputValue !== inputValue()) {
        event.preventDefault();
        checkMx();
      }
    });

    updateExamples();
    resetInspection();
    if (inputValue()) checkMx();
  };

  document.addEventListener("DOMContentLoaded", initDomainCreate);

  document.addEventListener("click", (event) => {
    const trigger = event.target.closest("[data-dismiss]");
    if (trigger) document.querySelector(trigger.dataset.dismiss)?.remove();
  });
})();
