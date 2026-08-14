// QC-19 browser regressions: editor revisions stay snapshot-bound and rapid
// settings taps serialize instead of racing with one shared precondition.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const root = path.resolve(__dirname, "..");
const source = fs.readFileSync(path.join(root, "frontend", "app.js"), "utf8");
const code = source.slice(0, source.indexOf("// ── init"));

function response(status, body, headers = {}) {
  const normalized = Object.fromEntries(
    Object.entries(headers).map(([key, value]) => [key.toLowerCase(), String(value)]));
  return {
    status,
    ok: status >= 200 && status < 300,
    headers: { get: (name) => normalized[name.toLowerCase()] ?? null },
    json: async () => body,
  };
}

const settingValue = { textContent: "" };
const settingElement = {
  dataset: { val: "10", step: "1", unit: "min" },
  querySelector: () => settingValue,
};
class HTMLElementMock {}
const genericElement = {
  classList: { add() {}, remove() {}, contains() { return false; }, toggle() {} },
  style: {}, dataset: {},
  querySelector: () => ({ textContent: "" }),
};

let fetchImpl = async () => response(200, {});
const context = vm.createContext({
  console,
  fetch: (...args) => fetchImpl(...args),
  document: {
    getElementById: (id) => id === "set-stale_after_minutes" ? settingElement : genericElement,
    querySelectorAll: () => [],
    addEventListener() {},
    documentElement: genericElement,
    body: genericElement,
  },
  HTMLElement: HTMLElementMock,
  navigator: {},
  MutationObserver: class { observe() {} disconnect() {} },
  confirm: () => true,
  prompt: () => null,
  setTimeout: (fn) => fn(),
  clearTimeout() {},
  setInterval() {},
  clearInterval() {},
});
vm.runInContext(code, context, { filename: "frontend/app.js" });

async function run() {
  const snapshot = {
    revision: 10,
    sensors: [], enclosures: [], species: [], thermostats: [],
    settings: { stale_after_minutes: 10 },
  };
  vm.runInContext("applyManageSnapshot", context)(snapshot);
  assert.equal(vm.runInContext("_configRevision", context), 10);

  // A background response can observe a later server revision, but it is not
  // the snapshot backing the open editor and must not bless that stale form.
  fetchImpl = async () => response(200, { species: [] }, { "X-Bask-Revision": "99" });
  await vm.runInContext("api", context)("GET", "/api/species");
  assert.equal(vm.runInContext("_configRevision", context), 10);

  const writes = [];
  fetchImpl = (url, options) => new Promise((resolve) => {
    writes.push({ url, options, resolve });
  });

  const stepSetting = vm.runInContext("stepSetting", context);
  const first = stepSetting("stale_after_minutes", 1);
  const second = stepSetting("stale_after_minutes", 1);
  await new Promise((resolve) => setImmediate(resolve));

  assert.equal(writes.length, 1, "the second click must wait for the first write");
  assert.equal(writes[0].options.headers["X-Bask-Revision"], "10");
  assert.deepEqual(JSON.parse(writes[0].options.body), { stale_after_minutes: 11 });

  writes[0].resolve(response(200, { ok: true }, {
    "X-Bask-Revision": "11", "X-Bask-Revision-Applied": "true",
  }));
  await new Promise((resolve) => setImmediate(resolve));

  assert.equal(writes.length, 2);
  assert.equal(writes[1].options.headers["X-Bask-Revision"], "11");
  assert.deepEqual(JSON.parse(writes[1].options.body), { stale_after_minutes: 12 });

  writes[1].resolve(response(200, { ok: true }, {
    "X-Bask-Revision": "12", "X-Bask-Revision-Applied": "true",
  }));
  await Promise.all([first, second]);
  assert.equal(vm.runInContext("_configRevision", context), 12);
  assert.equal(vm.runInContext("_settings.stale_after_minutes", context), 12);

  console.log("Frontend concurrency tests passed.");
}

run().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
