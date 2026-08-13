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

  const initAppShell = () => {
    const dropdowns = [...document.querySelectorAll("[data-dropdown]")];

    const menuItems = (dropdown) => [
      ...dropdown.querySelectorAll('[role="menuitem"]:not([aria-disabled="true"])'),
    ];

    const closeDropdown = (dropdown, { restoreFocus = false } = {}) => {
      const button = dropdown.querySelector("[data-dropdown-button]");
      const panel = dropdown.querySelector("[data-dropdown-panel]");
      if (!button || !panel || panel.classList.contains("hidden")) return;
      panel.classList.add("hidden");
      button.setAttribute("aria-expanded", "false");
      if (restoreFocus) button.focus();
    };

    const closeOtherDropdowns = (activeDropdown) => {
      dropdowns.forEach((dropdown) => {
        if (dropdown !== activeDropdown) closeDropdown(dropdown);
      });
    };

    const openDropdown = (dropdown, { focus = "none" } = {}) => {
      const button = dropdown.querySelector("[data-dropdown-button]");
      const panel = dropdown.querySelector("[data-dropdown-panel]");
      if (!button || !panel) return;
      closeOtherDropdowns(dropdown);
      panel.classList.remove("hidden");
      button.setAttribute("aria-expanded", "true");
      const items = menuItems(dropdown);
      if (focus === "first") items[0]?.focus();
      if (focus === "last") items.at(-1)?.focus();
    };

    dropdowns.forEach((dropdown) => {
      const button = dropdown.querySelector("[data-dropdown-button]");
      const panel = dropdown.querySelector("[data-dropdown-panel]");
      if (!button || !panel) return;

      button.addEventListener("click", () => {
        if (panel.classList.contains("hidden")) openDropdown(dropdown);
        else closeDropdown(dropdown);
      });
      button.addEventListener("keydown", (event) => {
        if (event.key !== "ArrowDown" && event.key !== "ArrowUp") return;
        event.preventDefault();
        openDropdown(dropdown, { focus: event.key === "ArrowDown" ? "first" : "last" });
      });
      panel.addEventListener("keydown", (event) => {
        const items = menuItems(dropdown);
        const currentIndex = items.indexOf(document.activeElement);
        if (event.key === "Escape") {
          event.preventDefault();
          closeDropdown(dropdown, { restoreFocus: true });
          return;
        }
        if (!["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key) || !items.length)
          return;
        event.preventDefault();
        if (event.key === "Home") items[0].focus();
        else if (event.key === "End") items.at(-1).focus();
        else if (event.key === "ArrowDown") items[(currentIndex + 1) % items.length].focus();
        else items[(currentIndex - 1 + items.length) % items.length].focus();
      });
      panel.addEventListener("click", (event) => {
        if (event.target.closest("a, button")) closeDropdown(dropdown);
      });
    });

    document.addEventListener("click", (event) => {
      dropdowns.forEach((dropdown) => {
        if (!dropdown.contains(event.target)) closeDropdown(dropdown);
      });
    });
    document.addEventListener("focusin", (event) => {
      dropdowns.forEach((dropdown) => {
        if (!dropdown.contains(event.target)) closeDropdown(dropdown);
      });
    });

    const sidebarButton = document.getElementById("sidebar-toggle");
    const sidebar = document.getElementById("app-sidebar");
    const backdrop = document.getElementById("sidebar-backdrop");
    if (!sidebarButton || !sidebar || !backdrop) return;

    const closeSidebar = ({ restoreFocus = false } = {}) => {
      sidebar.classList.add("hidden");
      sidebar.classList.remove("flex");
      backdrop.classList.add("hidden");
      document.body.classList.remove("overflow-hidden");
      sidebarButton.setAttribute("aria-expanded", "false");
      if (restoreFocus) sidebarButton.focus();
    };

    const openSidebar = () => {
      closeOtherDropdowns(null);
      sidebar.classList.remove("hidden");
      sidebar.classList.add("flex");
      backdrop.classList.remove("hidden");
      document.body.classList.add("overflow-hidden");
      sidebarButton.setAttribute("aria-expanded", "true");
    };

    sidebarButton.addEventListener("click", () => {
      if (sidebar.classList.contains("hidden")) openSidebar();
      else closeSidebar();
    });
    backdrop.addEventListener("click", () => closeSidebar({ restoreFocus: true }));
    sidebar.addEventListener("click", (event) => {
      if (window.innerWidth < 1024 && event.target.closest("a")) closeSidebar();
    });
    window.addEventListener("resize", () => {
      if (window.innerWidth >= 1024) closeSidebar();
    });
    document.addEventListener("keydown", (event) => {
      if (event.key !== "Escape") return;
      const openDropdownElement = dropdowns.find(
        (dropdown) => !dropdown.querySelector("[data-dropdown-panel]")?.classList.contains("hidden")
      );
      if (openDropdownElement) {
        closeDropdown(openDropdownElement, { restoreFocus: true });
        return;
      }
      if (!sidebar.classList.contains("hidden")) closeSidebar({ restoreFocus: true });
    });
  };

  const initDomainCreate = () => {
    const form = document.querySelector("[data-domain-create]");
    if (!form) return;

    const hostnameInput = form.querySelector("[name=hostname]");
    const checkButton = form.querySelector("[data-mx-check]");
    const submitButton = form.querySelector("[data-domain-submit]");
    const choices = form.querySelector("[data-mx-choices]");
    const choiceHelp = form.querySelector("[data-mx-choice-help]");
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
    const routingClassifications = new Set([
      "NO_MX",
      "OPERATIONAL_INBOX_RECONNECT",
      "SES_MX_UNCLAIMED",
      "EXTERNAL_MX",
      "MIXED_MX",
    ]);
    const defaultChoiceHelp = choiceHelp.textContent.trim();
    let inspectedInputValue = "";
    let recommendedMode = "";
    let routingClassification = "";
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
          : kind === "warning"
            ? "border-gold-strong/40 bg-gold-soft text-gold-strong"
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
      routingClassification = "";
      form.setAttribute("aria-busy", "false");
      status.classList.add("hidden");
      choices.classList.add("hidden");
      submitButton.classList.add("hidden");
      submitButton.disabled = false;
      overrideWarning.classList.add("hidden");
      checkButton.classList.remove("hidden");
      checkButton.disabled = false;
      checkButton.textContent = "Check MX records";
      choiceHelp.textContent = defaultChoiceHelp;
      clearSelection();
    };

    const updateOverrideWarning = () => {
      const selectedMode = radios.find((radio) => radio.checked)?.value || "";
      submitButton.disabled = !selectedMode;
      if (!selectedMode) {
        submitButton.textContent = "Choose a routing method";
        overrideWarning.classList.add("hidden");
        overrideWarning.textContent = "";
        return;
      }
      submitButton.textContent =
        selectedMode === PROVIDER_FORWARD
          ? "Continue with current provider"
          : "Continue with direct routing";

      const isOverride = recommendedMode && selectedMode !== recommendedMode;
      let warning = "";
      if (isOverride) {
        warning =
          recommendedMode === PROVIDER_FORWARD
            ? "Existing provider MX records were detected. Direct routing requires removing or replacing that delivery path when you update DNS."
            : routingClassification === "OPERATIONAL_INBOX_RECONNECT"
              ? "This looks like a previous Operational Inbox direct setup. Provider forwarding only works if another mail provider accepts the domain and forwards unmatched mail to the private route."
              : "No existing mail provider was detected. Provider forwarding only works after you configure a provider with catch-all forwarding.";
      } else if (!recommendedMode && routingClassification === "MIXED_MX") {
        warning =
          selectedMode === DIRECT_MX
            ? "Mixed MX records were detected. Remove the other provider MX records before relying on direct routing."
            : "Mixed MX records were detected. Remove the Operational Inbox SES MX record and configure your provider's catch-all before testing forwarding.";
      } else if (!recommendedMode && routingClassification === "SES_MX_UNCLAIMED") {
        warning =
          selectedMode === DIRECT_MX
            ? "This SES endpoint may belong to another setup. Operational Inbox will require a new ownership record before activating direct routing."
            : "Provider forwarding requires a separate provider that accepts mail for this domain and supports catch-all forwarding.";
      }
      if (!warning) {
        overrideWarning.classList.add("hidden");
        overrideWarning.textContent = "";
        return;
      }
      overrideWarning.textContent = warning;
      overrideWarning.classList.remove("hidden");
    };

    const revealRecommendation = (mode, classification) => {
      recommendedMode = mode || "";
      routingClassification = classification;
      radios.forEach((radio) => {
        radio.checked = Boolean(mode) && radio.value === mode;
      });
      cards.forEach((card) => card.classList.remove("hidden"));
      form.querySelectorAll("[data-recommendation]").forEach((badge) => {
        badge.classList.toggle("hidden", !mode || badge.dataset.recommendation !== mode);
      });
      choices.classList.remove("hidden");
      choiceHelp.textContent = mode
        ? "We selected the safest default based on public DNS. Both routing methods remain available."
        : "The detected DNS can support more than one interpretation. Review both methods and choose explicitly.";
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
      routingClassification = "";
      clearSelection();
      choices.classList.add("hidden");
      submitButton.classList.add("hidden");
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
          routingClassifications.has(payload.mx_classification) &&
          (payload.has_operational_inbox_claim === null ||
            typeof payload.has_operational_inbox_claim === "boolean") &&
          (payload.recommended_setup_mode === null ||
            supportedModes.has(payload.recommended_setup_mode)) &&
          typeof payload.requires_explicit_choice === "boolean" &&
          payload.requires_explicit_choice === (payload.recommended_setup_mode === null) &&
          Array.isArray(payload.mx_records) &&
          payload.has_existing_mx === (payload.mx_records.length > 0) &&
          payload.mx_records.every(
            (record) =>
              Number.isInteger(record.preference) && typeof record.exchange === "string"
          );
        if (!responseIsValid) throw new Error("The server returned an unreadable DNS result.");
        if (activeSequence !== requestSequence || inputValue() !== requestedValue) return;

        inspectedInputValue = requestedValue;
        revealRecommendation(payload.recommended_setup_mode, payload.mx_classification);
        if (payload.mx_classification === "OPERATIONAL_INBOX_RECONNECT") {
          setStatus({
            kind: "result",
            title: "Previous Operational Inbox setup found",
            body:
              "This domain already points to the configured receiving service and has an older claim record. The MX can stay; a fresh ownership value will be required before activation.",
            records: formatRecords(payload.mx_records),
          });
        } else if (payload.mx_classification === "SES_MX_UNCLAIMED") {
          setStatus({
            kind: "warning",
            title: "SES receiving route found",
            body:
              "This SES endpoint is shared and does not identify a specific Operational Inbox setup. Choose the intended routing method explicitly.",
            records: formatRecords(payload.mx_records),
          });
        } else if (payload.mx_classification === "MIXED_MX") {
          setStatus({
            kind: "warning",
            title: "Mixed mail routing found",
            body:
              "Some MX records point to Operational Inbox's SES region and others point elsewhere. Choose the intended owner, then remove the conflicting MX records before testing.",
            records: formatRecords(payload.mx_records),
          });
        } else if (payload.mx_classification === "EXTERNAL_MX") {
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
        routingClassification = "";
        clearSelection();
        choices.classList.add("hidden");
        submitButton.classList.add("hidden");
        setStatus({
          kind: "error",
          title: `We could not check DNS for ${requestedValue}.`,
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
        return;
      }
      if (!radios.some((radio) => radio.checked)) {
        event.preventDefault();
        updateOverrideWarning();
        radios[0].focus();
        return;
      }
      form.setAttribute("aria-busy", "true");
      submitButton.disabled = true;
      submitButton.textContent = "Connecting domain…";
      checkButton.disabled = true;
    });

    updateExamples();
    resetInspection();
    if (inputValue()) checkMx();
  };

  const initCopyControls = () => {
    document.querySelectorAll("[data-copy-target]").forEach((button) => {
      button.addEventListener("click", () => {
        const target = document.getElementById(button.dataset.copyTarget);
        if (!target) return;
        const originalLabel = button.dataset.copyLabel || button.textContent.trim();
        const copyText = target.textContent.trim();
        const clipboardWrite = navigator.clipboard?.writeText
          ? navigator.clipboard.writeText(copyText).catch(() => false)
          : Promise.resolve(false);
        const textarea = document.createElement("textarea");
        textarea.value = copyText;
        textarea.setAttribute("readonly", "");
        textarea.style.position = "fixed";
        textarea.style.opacity = "0";
        document.body.appendChild(textarea);
        textarea.select();
        const copied = document.execCommand("copy");
        textarea.remove();
        button.textContent = copied ? "Copied" : "Copy failed";
        clipboardWrite.then((clipboardCopied) => {
          if (clipboardCopied !== false) button.textContent = "Copied";
        });
        window.setTimeout(() => {
          button.textContent = originalLabel;
        }, 1600);
      });
    });
  };

  const initAgentPromptExamples = () => {
    document.querySelectorAll("[data-agent-prompt-examples]").forEach((container) => {
      const output = container.querySelector("[data-agent-prompt-example]");
      const cursor = container.querySelector("[data-agent-prompt-cursor]");
      if (!output) return;

      let prompts = [];
      try {
        prompts = JSON.parse(container.dataset.agentPrompts || "[]");
      } catch {
        return;
      }
      if (!Array.isArray(prompts) || !prompts.every((prompt) => typeof prompt === "string"))
        return;
      const visiblePrompts = prompts.filter(Boolean);
      if (!visiblePrompts.length) return;

      output.textContent = visiblePrompts[0];
      if (window.matchMedia?.("(prefers-reduced-motion: reduce)").matches) {
        cursor?.classList.add("hidden");
        return;
      }

      let promptIndex = 0;
      let characterIndex = 0;
      let deleting = false;
      output.textContent = "";

      const updatePrompt = () => {
        const prompt = visiblePrompts[promptIndex];
        if (!deleting) {
          characterIndex += 1;
          output.textContent = prompt.slice(0, characterIndex);
          if (characterIndex === prompt.length) {
            deleting = true;
            window.setTimeout(updatePrompt, 1800);
            return;
          }
          window.setTimeout(updatePrompt, 42);
          return;
        }

        characterIndex -= 1;
        output.textContent = prompt.slice(0, characterIndex);
        if (characterIndex === 0) {
          deleting = false;
          promptIndex = (promptIndex + 1) % visiblePrompts.length;
          window.setTimeout(updatePrompt, 320);
          return;
        }
        window.setTimeout(updatePrompt, 22);
      };

      window.setTimeout(updatePrompt, 350);
    });
  };

  document.addEventListener("DOMContentLoaded", initAppShell);
  document.addEventListener("DOMContentLoaded", initDomainCreate);
  document.addEventListener("DOMContentLoaded", initCopyControls);
  document.addEventListener("DOMContentLoaded", initAgentPromptExamples);

  document.addEventListener("change", (event) => {
    const field = event.target.closest("[data-auto-submit]");
    if (field?.form) field.form.requestSubmit();
  });

  document.addEventListener("click", (event) => {
    const trigger = event.target.closest("[data-dismiss]");
    if (trigger) document.querySelector(trigger.dataset.dismiss)?.remove();
  });
})();
