const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(__dirname, "../../inbox/static/js/app.js"), "utf8");

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
