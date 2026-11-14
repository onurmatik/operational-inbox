const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(__dirname, "../../inbox/static/js/app.js"), "utf8");

test("manual send preserves text and the request key after an error and suppresses double clicks", async () => {
  const ready = [];
  const submit = {};
  const error = {};
  const fields = { body_text: "My exact reply.\n", idempotency_key: "same-request" };
  let onSubmit;
  const form = {
    getAttribute: () => "/synthetic/reply/",
    querySelector: (selector) => selector === "[data-send-reply]" ? submit : error,
    addEventListener: (name, fn) => { onSubmit = fn; },
  };
  const navigations = [];
  const window = { location: { assign: (url) => navigations.push(url) } };
  vm.runInNewContext(source, {
    window,
    document: {
      addEventListener: (name, fn) => { if (name === "DOMContentLoaded") ready.push(fn); },
      querySelector: () => form,
    },
    FormData: class { constructor() { Object.assign(this, fields); } },
  });
  ready.find((fn) => fn.name === "initManualReply")();
  const requests = [];
  let resolve, reject;
  window.OperationalInbox.request = (url, options) => {
    requests.push({ url, ...options.body });
    return new Promise((yes, no) => { resolve = yes; reject = no; });
  };
  const event = { preventDefault() {} };
  const first = onSubmit(event);
  await onSubmit(event);
  assert.equal(requests.length, 1);
  assert.equal(submit.disabled, true);
  reject(new Error("Sending is paused."));
  await first;
  assert.equal(submit.disabled, false);
  assert.equal(error.textContent, "Sending is paused.");
  assert.equal(fields.body_text, "My exact reply.\n");
  assert.equal(navigations.length, 0);
  const retry = onSubmit(event);
  assert.deepEqual(requests[1], requests[0]);
  resolve({ url: "/synthetic/thread/", status: "QUEUED" });
  await retry;
  assert.deepEqual(navigations, ["/synthetic/thread/"]);
});

for (const ok of [true, false]) {
  test(`unreadable ${ok ? "successful" : "failed"} response rejects instead of losing editor text`, async () => {
    const window = {};
    vm.runInNewContext(source, {
      window,
      document: { addEventListener() {}, querySelector: () => null },
      fetch: async () => ({ ok, json: async () => { throw new SyntaxError("HTML response"); } }),
    });
    await assert.rejects(window.OperationalInbox.request("/synthetic/reply/"), (error) => {
      assert.equal(error.payload.code, "invalid_response");
      return /unreadable response/.test(error.message);
    });
  });
}
