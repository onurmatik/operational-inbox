const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");
const source = fs.readFileSync(path.join(__dirname, "../../inbox/static/js/mail_transfers.js"), "utf8");

function ui({ size = 100, uploadFails = false, completeFails = false, withForm = true, busy = false } = {}) {
  const requests = [], uploads = [], navigations = [], timers = [];
  const feedback = { textContent: "" }, pollStatus = { textContent: "" };
  const controls = [{ disabled: busy }, { disabled: busy }];
  const results = { innerHTML: "", querySelectorAll: () => [], contains: () => false };
  const form = {
    action: "/prepare/", elements: { file: { files: [{ name: "history.mbox", size }] }, csrfmiddlewaretoken: { value: "session-csrf" } },
    querySelector: () => feedback,
    addEventListener: (_, handler) => { form.submit = handler; },
  };
  const root = {
    dataset: { busy: String(busy), maxBytes: "250", statusUrl: "/status/" },
    querySelector: (selector) => ({ "[data-import-form]": withForm ? form : null, "[data-transfer-results]": results, "[data-poll-status]": pollStatus })[selector],
    querySelectorAll: () => controls,
  };
  class FormData { constructor() { this.values = {}; } append(key, value) { this.values[key] = value; } }
  class XHR {
    constructor() { this.upload = {}; }
    open(method, url) { this.method = method; this.url = url; }
    send(body) {
      uploads.push({ url: this.url, body: body.values });
      this.upload.onprogress({ lengthComputable: true, loaded: 50, total: 100 });
      if (uploadFails) this.onerror();
      else { this.status = 204; this.onload(); }
    }
    setRequestHeader() { throw new Error("Session headers must not reach S3"); }
  }
  const fetch = async (url, options) => {
    requests.push({ url, options });
    if (url === "/prepare/") return { ok: true, json: async () => ({ upload: { url: "https://private-s3.example/upload", fields: { key: "specific-key", policy: "signed-policy" } }, complete_url: "/complete/", cancel_url: "/cancel/", page_url: "/transfers/" }) };
    if (url === "/complete/" && completeFails) return { ok: false, json: async () => ({ error: "Temporary failure" }) };
    if (url === "/status/") return { ok: true, json: async () => ({ html: "owner-only-results", active: completeFails }) };
    return { ok: true, json: async () => ({}) };
  };
  vm.runInNewContext(source, {
    document: { querySelector: () => root, addEventListener: (_, handler) => handler(), hidden: false, activeElement: null, getElementById() {} },
    window: { location: { assign: (url) => navigations.push(url) } },
    fetch, FormData, XMLHttpRequest: XHR,
    setTimeout: (fn) => { timers.push(fn); return timers.length; }, clearTimeout() {},
  });
  return { requests, uploads, navigations, feedback, controls, results, timers, submit: () => form.submit({ preventDefault() {} }) };
}

test("upload uses signed form fields, same-origin CSRF, and finalizes only after S3 success", async () => {
  const u = ui();
  await u.submit();
  assert.deepEqual(u.requests.map((r) => r.url), ["/prepare/", "/complete/"]);
  assert.equal(u.requests[0].options.headers["X-CSRFToken"], "session-csrf");
  assert.equal(u.uploads[0].body.key, "specific-key");
  assert.equal(u.uploads[0].body.file.name, "history.mbox");
  assert.deepEqual(u.navigations, ["/transfers/"]);
});

test("oversized and empty files never start an upload", async () => {
  for (const size of [0, 251]) {
    const u = ui({ size });
    await u.submit();
    assert.equal(u.requests.length, 0);
    assert.match(u.feedback.textContent, /250 MB/);
  }
});

test("interrupted upload is abandoned and controls recover", async () => {
  const u = ui({ uploadFails: true });
  await u.submit();
  assert.deepEqual(u.requests.map((r) => r.url), ["/prepare/", "/cancel/", "/status/"]);
  assert.equal(u.navigations.length, 0);
  assert.ok(u.controls.every((control) => !control.disabled));
});

test("a lost finalize response preserves the uploaded object and offers retry", async () => {
  const u = ui({ completeFails: true });
  await u.submit();
  assert.ok(!u.requests.some((r) => r.url === "/cancel/"));
  assert.match(u.feedback.textContent, /Finish import/);
  assert.ok(u.controls.every((control) => control.disabled));
});

test("export-only pages poll without an import form", async () => {
  const u = ui({ withForm: false, busy: true });
  await u.timers[0]();
  assert.equal(u.results.innerHTML, "owner-only-results");
  assert.ok(u.controls.every((control) => !control.disabled));
});

test("completion renders even while transfer details have focus", async () => {
  const u = ui({ busy: true });
  u.results.contains = () => true;
  await u.timers[0]();
  assert.equal(u.results.innerHTML, "owner-only-results");
  assert.equal(u.timers.length, 1);
  assert.ok(u.controls.every((control) => !control.disabled));
});
