const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

// Exercise the production event handlers without a browser or third-party DOM dependency.
function composer(initialDelivery = "draft") {
  const element = () => ({
    listeners: {},
    hidden: false,
    disabled: false,
    textContent: "",
    setAttribute(name, value) { this[name] = value; },
    focus() { this.focused = true; },
    addEventListener(name, callback) { this.listeners[name] = callback; },
  });
  const controls = new Map();
  const form = element();
  const opener = element();
  const panel = element();
  panel.hidden = true;
  panel.querySelector = () => element();
  panel.closest = () => ({ scrollTop: 0 });
  const manual = element();
  form.dataset = { goalsUrl: "/synthetic/goals/" };
  form.closest = () => panel;
  form.querySelector = (selector) => {
    if (!controls.has(selector)) controls.set(selector, element());
    return controls.get(selector);
  };
  form.elements = {
    instructions: Object.assign(element(), { value: "Ask for the current plan." }),
    delivery: { value: initialDelivery },
    context_token: { value: "synthetic-context" },
  };
  // The real form contains <input name="action">, which shadows form.action.
  form.action = { value: "handoff" };
  form.getAttribute = (name) => name === "action" ? "/synthetic/handling/" : null;
  const ready = [];
  const window = { location: { reload() { window.reloads += 1; } }, reloads: 0 };
  const context = {
    window,
    document: {
      addEventListener(name, callback) {
        if (name === "DOMContentLoaded") ready.push(callback);
      },
      querySelector(selector) {
        if (selector === "[data-reply-brief]") return form;
        if (selector === "[data-manual-reply]") return manual;
        return opener;
      },
    },
    FormData: class {
      constructor(source) { this.delivery = source?.elements.delivery.value; }
      set() {}
    },
  };
  vm.runInNewContext(
    fs.readFileSync(path.join(__dirname, "../../inbox/static/js/app.js"), "utf8"),
    context,
  );
  ready.find((callback) => callback.name === "initReplyBrief")();
  const submit = controls.get("[data-submit-reply-brief]");
  submit.textContent = initialDelivery === "auto" ? "Start replying" : "Create draft";
  let resolve, reject;
  const requests = [];
  window.OperationalInbox.request = (url, options) => {
    if (url === "/synthetic/goals/") return Promise.resolve({ goals: [] });
    assert.equal(url, "/synthetic/handling/", "use the endpoint, not the named action control");
    requests.push(options.body);
    return new Promise((complete, fail) => { resolve = complete; reject = fail; });
  };
  return {
    form, submit, requests, controls, window, panel, manual, opener,
    submitForm: () => form.listeners.submit({ preventDefault() {} }),
    choose(delivery) {
      form.elements.delivery.value = delivery;
      form.listeners.change();
    },
    fail(stale = false) {
      const error = new Error("Synthetic queue failure.");
      error.payload = { stale };
      reject(error);
    },
    succeed() { resolve({ queued: true }); },
  };
}

for (const [initial, next, label] of [
  ["draft", "auto", "Start replying"],
  ["auto", "draft", "Create draft"],
]) {
  test(`failed ${initial} request reflects current ${next} delivery on retry`, async () => {
    const ui = composer(initial);
    const pending = ui.submitForm();
    ui.choose(next);
    await ui.submitForm();
    assert.equal(ui.requests.length, 1, "a second submission must not be queued");
    assert.equal(ui.requests[0].delivery, initial, "the in-flight request keeps its choice");
    assert.equal(ui.submit.disabled, true);
    assert.equal(ui.submit.textContent, "Queuing…", "delivery edits must not hide pending state");
    ui.fail();
    await pending;
    assert.equal(ui.submit.disabled, false);
    assert.equal(ui.submit.textContent, label, "retry label must match the current delivery");
  });
}

test("stale submission stays disabled after delivery changes", async () => {
  const ui = composer();
  const pending = ui.submitForm();
  ui.choose("auto");
  ui.fail(true);
  await pending;
  assert.equal(ui.submit.disabled, true);
  assert.equal(ui.controls.get("[data-refresh-brief]").hidden, false);
  assert.equal(ui.submit.textContent, "Start replying");
  await ui.submitForm();
  assert.equal(ui.requests.length, 1);
});

test("agent options toggle back to the manual reply without losing text", () => {
  const ui = composer();
  const original = ui.form.elements.instructions.value;
  ui.opener.listeners.click();
  assert.equal(ui.panel.hidden, false);
  assert.equal(ui.manual.hidden, true);
  assert.equal(ui.opener.textContent, "Reply myself");
  ui.opener.listeners.click();
  assert.equal(ui.panel.hidden, true);
  assert.equal(ui.manual.hidden, false);
  assert.equal(ui.opener.textContent, "Reply with agent");
  ui.opener.listeners.click();
  ui.controls.get("[data-cancel-reply-brief]").listeners.click();
  assert.equal(ui.panel.hidden, true);
  assert.equal(ui.manual.hidden, false);
  assert.equal(ui.form.elements.instructions.value, original);
});

test("successful submission uses the form endpoint and reloads once", async () => {
  const ui = composer();
  const pending = ui.submitForm();
  await ui.submitForm();
  ui.succeed();
  await pending;
  assert.equal(ui.requests.length, 1);
  assert.equal(ui.requests[0].delivery, "draft");
  assert.equal(ui.window.reloads, 1);
});
