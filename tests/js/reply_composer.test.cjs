const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");
const source = fs.readFileSync(path.join(__dirname, "../../inbox/static/js/app.js"), "utf8");

class Element {
  constructor(value = "") { this.value = value; this.handlers = {}; this.children = []; this.attributes = {}; this.hidden = false; }
  addEventListener(name, fn) { (this.handlers[name] ||= []).push(fn); }
  async fire(name, event = {}) { for (const fn of this.handlers[name] || []) await fn(event); }
  setAttribute(name, value) { this.attributes[name] = value; }
  getAttribute(name) { return this.attributes[name]; }
  replaceChildren(...children) { this.children = children; }
  appendChild(child) { this.children.push(child); }
  querySelectorAll() { return this.children; }
  focus() { this.focused = true; }
}
function ui(body = "Owner's exact text") {
  const ready = [], nodes = new Map(), requests = [], navigations = [];
  const node = (key) => { if (!nodes.has(key)) nodes.set(key, new Element()); return nodes.get(key); };
  const form = new Element();
  form.elements = Object.fromEntries(Object.entries({ subject: "Re: Exchange", body_text: body,
    instructions: "", context_token: "original-context", draft_id: "", expected_revision_id: "",
    idempotency_key: "same-key", context_message_id: "message" }).map(([k, v]) => [k, new Element(v)]));
  form.dataset = { assistUrl: "/reply-assist/" };
  form.setAttribute("action", "/reply/");
  form.querySelector = (selector) => node(selector.slice(6, -1));
  node("reply-assist").hidden = true;
  node("suggestion").hidden = true;
  const window = { addEventListener() {}, location: { assign: (url) => navigations.push(url), reload() {} } };
  vm.runInNewContext(source, { window, document: {
    addEventListener: (name, fn) => { if (name === "DOMContentLoaded") ready.push(fn); },
    querySelector: (selector) => selector === "[data-reply-form]" ? form : null,
    getElementById: node, createElement: () => new Element(),
  }, FormData: class {
    constructor(form) { this.values = Object.fromEntries(Object.entries(form.elements).map(([k, v]) => [k, v.value])); }
    set(key, value) { this.values[key] = value; }
  }});
  ready.find((fn) => fn.name === "initReplyComposer")();
  let respond = async () => ({ goals: [] });
  window.OperationalInbox.request = (url, options) => { requests.push({ url, ...options.body.values }); return respond(requests.at(-1)); };
  return { form, node, fields: form.elements, requests, navigations,
    respond: (fn) => { respond = fn; }, click: (key) => node(key).fire("click"),
    edit: async (key, value) => { form.elements[key].value = value; await form.elements[key].fire("input"); },
    submit: (action) => form.fire("submit", { preventDefault() {}, submitter: { value: action } }),
  };
}
const tick = () => new Promise((resolve) => setImmediate(resolve));
const generated = { subject: "Re: Link exchange", body_text: "We accept your proposal.", notes: [], context_token: "original-context" };

for (const label of ["Accept", "Decline"]) {
  test(`${label} only fills instructions and no intent is automatically selected`, async () => {
    const u = ui("");
    assert.equal(u.node("review-reply").disabled, true);
    u.respond(async () => ({ goals: ["Accept", "Decline"].map((label) => ({ label, instructions: `${label} the proposal.` })) }));
    await u.click("open-assist"); await tick();
    assert.equal(u.fields.instructions.value, "");
    assert.equal(u.fields.body_text.value, "");
    const choice = u.node("reply-goals").children.find((button) => button.textContent === label);
    assert.equal(choice.attributes["aria-pressed"], "false");
    await choice.fire("click");
    assert.equal(u.fields.instructions.value, `${label} the proposal.`);
    assert.equal(u.requests.length, 1);
    assert.equal(u.fields.body_text.value, "");
  });
}

test("custom generation and review preview never save or send; Apply validates context", async () => {
  const u = ui();
  await u.edit("instructions", "Accept and ask which page.");
  u.respond(async () => generated);
  await u.click("generate-reply");
  assert.equal(u.requests[0].operation, "generate");
  assert.equal(u.fields.body_text.value, "Owner's exact text");
  assert.equal(u.node("suggestion").hidden, false);
  await u.click("apply-suggestion");
  assert.equal(u.requests[1].operation, "validate");
  assert.equal(u.fields.body_text.value, generated.body_text);
  assert.equal(u.fields.draft_id.value, "");
  u.respond(async () => ({ ...generated, notes: ["Ask for the exact URL."] }));
  await u.click("review-reply");
  assert.equal(u.requests.at(-1).operation, "review");
  assert.equal(u.node("review-notes").children[0].textContent, "Ask for the exact URL.");
  await u.click("dismiss-suggestion");
  assert.equal(u.fields.body_text.value, generated.body_text);
  assert.ok(u.requests.every((request) => request.url === "/reply-assist/"));
});

test("typing during model response and after preview prevents stale Apply", async () => {
  const u = ui();
  let resolve;
  u.respond(() => new Promise((yes) => { resolve = yes; }));
  const run = u.click("review-reply");
  await u.edit("body_text", "New owner text");
  resolve(generated); await run;
  assert.equal(u.fields.body_text.value, "New owner text");
  assert.equal(u.node("suggestion").hidden, true);
  u.respond(async () => generated);
  await u.click("review-reply");
  await u.edit("subject", "Edited subject");
  await u.click("apply-suggestion");
  assert.equal(u.requests.length, 2);
  assert.equal(u.fields.body_text.value, "New owner text");
});

test("thread changes at Apply keep the owner text and require refresh", async () => {
  const u = ui();
  u.respond(async () => generated);
  await u.click("review-reply");
  u.respond(async () => { throw Object.assign(new Error("Thread changed"), { payload: { stale: true } }); });
  await u.click("apply-suggestion");
  assert.equal(u.fields.body_text.value, "Owner's exact text");
  assert.equal(u.node("refresh-context").hidden, false);
  assert.equal(u.node("send-reply").disabled, true);
});

test("model failure leaves manual draft actions available", async () => {
  const u = ui();
  u.respond(async () => { throw new Error("AI unavailable"); });
  await u.click("review-reply");
  assert.equal(u.fields.body_text.value, "Owner's exact text");
  assert.equal(u.node("send-reply").disabled, false);
  assert.equal(u.node("save-draft").disabled, false);
});

test("Save updates shared revision context, while send errors preserve content and key", async () => {
  const u = ui();
  u.respond(async () => ({ draft: { id: "draft", current_revision_id: "revision" }, context_token: "saved-context" }));
  await u.submit("save");
  assert.equal(u.fields.draft_id.value, "draft");
  assert.equal(u.fields.expected_revision_id.value, "revision");
  assert.equal(u.fields.context_token.value, "saved-context");
  assert.equal(u.navigations.length, 0);
  let reject;
  u.respond(() => new Promise((yes, no) => { reject = no; }));
  const send = u.submit("send");
  await u.submit("send");
  assert.equal(u.requests.length, 2);
  reject(new Error("Sending paused")); await send;
  assert.equal(u.fields.body_text.value, "Owner's exact text");
  assert.equal(u.fields.idempotency_key.value, "same-key");
  u.respond(async () => ({ url: "/thread/", status: "QUEUED" }));
  await u.submit("send");
  assert.deepEqual(u.requests[1], u.requests[2]);
  assert.deepEqual(u.navigations, ["/thread/"]);
});
