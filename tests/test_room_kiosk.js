// Haven (/room.html) night blackout is a KIOSK opt-in, not something every
// visitor gets. Runs the real frontend/room.js in a VM with a fake DOM, fake
// timers and a controllable clock, so a regression that blacks out an ordinary
// phone/laptop view at night (or stops the kiosk blacking out) fails here.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const root = path.resolve(__dirname, "..");
const source = fs.readFileSync(path.join(root, "frontend", "room.js"), "utf8");
const css = fs.readFileSync(path.join(root, "frontend", "room.css"), "utf8");

const PAYLOAD = {
  generated_at: 1790000000,
  bask: { enclosures: [], counts: {} },
  shed: { configured: false },
};

const tick = () => new Promise((resolve) => setImmediate(resolve));

function makeElement() {
  const classes = new Set();
  const element = {
    textContent: "", innerHTML: "", className: "", id: "",
    style: {}, dataset: {}, children: [], removed: false,
    classList: {
      add: (name) => classes.add(name),
      remove: (name) => classes.delete(name),
      contains: (name) => classes.has(name),
    },
    appendChild(child) { element.children.push(child); child.parent = element; return child; },
    remove() {
      element.removed = true;
      if (element.parent) element.parent.children = element.parent.children.filter((c) => c !== element);
    },
  };
  return element;
}

// Load room.js fresh for one visit. `search` is the page's query string; `hour`
// is the visitor's local hour. Returns helpers to move the clock and run timers.
function loadRoom({ search = "", hour }) {
  const elements = new Map();
  const body = makeElement();
  const document = {
    body,
    querySelector(selector) {
      if (!elements.has(selector)) elements.set(selector, makeElement());
      return elements.get(selector);
    },
    createElement: () => makeElement(),
  };

  const intervals = new Map(); // id -> { fn, ms }
  let nextId = 1;
  const fetchCalls = [];
  const context = {
    console: { log() {}, error() {}, warn() {} },
    document,
    location: { search },
    URLSearchParams,
    module: { exports: {} },
    setInterval: (fn, ms) => { const id = nextId++; intervals.set(id, { fn, ms }); return id; },
    clearInterval: (id) => { intervals.delete(id); },
    fetch: async (url) => {
      fetchCalls.push(url);
      return { ok: true, status: 200, json: async () => PAYLOAD };
    },
  };
  vm.createContext(context);
  // Controllable "now": new Date() / Date.now() read __nowMs, so the visit's
  // local hour is exactly what the scenario says regardless of the host zone.
  vm.runInContext(`
    const RealDate = Date;
    globalThis.__nowMs = 0;
    globalThis.Date = class extends RealDate {
      constructor(...args) { if (args.length) super(...args); else super(globalThis.__nowMs); }
      static now() { return globalThis.__nowMs; }
    };
  `, context);
  const setHour = (h) => {
    context.__nowMs = new Date(2026, 9, 9, h, 0, 30).getTime();
  };
  setHour(hour);
  vm.runInContext(source, context, { filename: "room.js" });

  return {
    exports: context.module.exports,
    body,
    fetchCalls,
    setHour,
    cover: () => body.children.find((child) => child.id === "night-cover") || null,
    periods: () => [...intervals.values()].map((entry) => entry.ms).sort((a, b) => a - b),
    // Fire every active timer with this period once (as the browser would).
    fire: async (ms) => {
      for (const entry of [...intervals.values()]) if (entry.ms === ms) entry.fn();
      await tick();
    },
    flush: tick,
  };
}

const DASHBOARD_PERIODS = [1000, 15000];
const KIOSK_DAY_PERIODS = [1000, 15000, 60000];
const KIOSK_NIGHT_PERIODS = [60000];

(async () => {
  // ── Pure helpers ──────────────────────────────────────────────────────────
  const { kioskSettings, isNightHour } = loadRoom({ hour: 12 }).exports;

  assert.equal(kioskSettings("").enabled, false);
  assert.equal(kioskSettings("?").enabled, false);
  assert.equal(kioskSettings("?kiosk=0").enabled, false);
  assert.equal(kioskSettings("?kiosk=").enabled, false);
  assert.equal(kioskSettings("?night=20-8").enabled, false, "night= alone must not enable kiosk mode");
  for (const on of ["?kiosk=1", "?kiosk=true", "?kiosk=ON", "?x=2&kiosk=1"]) {
    assert.equal(kioskSettings(on).enabled, true, on);
  }
  assert.deepEqual({ ...kioskSettings("?kiosk=1") }, { enabled: true, nightStart: 20, dayStart: 8 });
  assert.deepEqual({ ...kioskSettings("?kiosk=1&night=22-6") }, { enabled: true, nightStart: 22, dayStart: 6 });
  for (const bad of ["abc", "24-8", "20-24", "8-8", "20", "-5", "20-8-1"]) {
    const s = kioskSettings(`?kiosk=1&night=${bad}`);
    assert.deepEqual({ nightStart: s.nightStart, dayStart: s.dayStart }, { nightStart: 20, dayStart: 8 }, `night=${bad} falls back to defaults`);
  }

  // Default overnight window 20:00-08:00 (wraps midnight).
  for (const h of [20, 21, 23, 0, 3, 7]) assert.equal(isNightHour(h), true, `hour ${h} is night`);
  for (const h of [8, 9, 12, 17, 19]) assert.equal(isNightHour(h), false, `hour ${h} is day`);
  // Custom wrapping window and a same-day (non-wrapping) window.
  assert.equal(isNightHour(21, 22, 6), false);
  assert.equal(isNightHour(22, 22, 6), true);
  assert.equal(isNightHour(5, 22, 6), true);
  assert.equal(isNightHour(6, 22, 6), false);
  assert.equal(isNightHour(0, 1, 5), false);
  assert.equal(isNightHour(1, 1, 5), true);
  assert.equal(isNightHour(4, 1, 5), true);
  assert.equal(isNightHour(5, 1, 5), false);

  // ── Non-kiosk visitor at night: live dashboard, no cover, timers running ──
  for (const search of ["", "?foo=bar", "?kiosk=0", "?night=20-8"]) {
    for (const hour of [20, 21, 23, 0, 3, 7]) {
      const page = loadRoom({ search, hour });
      await page.flush();
      assert.equal(page.cover(), null, `no night cover for "${search}" at ${hour}:00`);
      assert.equal(page.body.classList.contains("kiosk-night"), false);
      assert.ok(page.fetchCalls.length >= 1, `dashboard fetched for "${search}" at ${hour}:00`);
      assert.deepEqual(page.periods(), DASHBOARD_PERIODS, "clock + refresh timers run; no phase watcher");
    }
  }
  {
    // Still live all night: the 15 s refresh keeps fetching across the old
    // 20:00 / 00:00 boundaries and never installs a cover.
    const page = loadRoom({ hour: 19 });
    await page.flush();
    const before = page.fetchCalls.length;
    for (const hour of [20, 22, 0, 4, 7, 8]) {
      page.setHour(hour);
      await page.fire(15000);
      assert.equal(page.cover(), null, `still no cover at ${hour}:00`);
    }
    assert.equal(page.fetchCalls.length, before + 6, "every refresh tick still fetched");
    assert.deepEqual(page.periods(), DASHBOARD_PERIODS);
  }

  // ── Kiosk at night: cover + animations frozen + timers stopped ────────────
  for (const hour of [20, 21, 23, 0, 3, 7]) {
    const page = loadRoom({ search: "?kiosk=1", hour });
    await page.flush();
    const cover = page.cover();
    assert.ok(cover, `cover present at ${hour}:00`);
    assert.match(cover.style.cssText, /position:fixed/);
    assert.match(cover.style.cssText, /background:#000/);
    assert.equal(page.body.classList.contains("kiosk-night"), true, "animations frozen via .kiosk-night");
    assert.equal(page.fetchCalls.length, 0, "no dashboard fetches under the cover");
    assert.deepEqual(page.periods(), KIOSK_NIGHT_PERIODS, "only the 60 s phase watcher remains");

    // The watcher is a no-op while the phase is unchanged: no new DOM work.
    const children = page.body.children.length;
    await page.fire(60000);
    await page.fire(60000);
    assert.equal(page.body.children.length, children);
    assert.equal(page.fetchCalls.length, 0);
    assert.deepEqual(page.periods(), KIOSK_NIGHT_PERIODS);
  }

  // ── Kiosk by day: dashboard, no cover ─────────────────────────────────────
  for (const hour of [8, 9, 12, 17, 19]) {
    const page = loadRoom({ search: "?kiosk=1", hour });
    await page.flush();
    assert.equal(page.cover(), null, `no cover at ${hour}:00`);
    assert.equal(page.body.classList.contains("kiosk-night"), false);
    assert.ok(page.fetchCalls.length >= 1);
    assert.deepEqual(page.periods(), KIOSK_DAY_PERIODS);
  }

  // ── Resume at the day boundary, and enter night at the evening boundary ───
  {
    const page = loadRoom({ search: "?kiosk=1", hour: 7 });
    await page.flush();
    assert.ok(page.cover());
    assert.equal(page.fetchCalls.length, 0);

    await page.fire(60000); // still 07:xx -> nothing changes
    assert.ok(page.cover());
    assert.equal(page.fetchCalls.length, 0);

    page.setHour(8);
    await page.fire(60000); // 08:00 -> resume
    assert.equal(page.cover(), null, "cover removed at 08:00");
    assert.equal(page.body.classList.contains("kiosk-night"), false, "animations resumed");
    assert.equal(page.fetchCalls.length, 1, "dashboard refreshed immediately on resume");
    assert.deepEqual(page.periods(), KIOSK_DAY_PERIODS, "clock + refresh timers restarted");

    page.setHour(19);
    await page.fire(60000);
    assert.equal(page.cover(), null);
    assert.deepEqual(page.periods(), KIOSK_DAY_PERIODS);

    page.setHour(20);
    await page.fire(60000); // 20:00 -> blackout again
    assert.ok(page.cover(), "cover returns at 20:00");
    assert.equal(page.body.classList.contains("kiosk-night"), true);
    assert.deepEqual(page.periods(), KIOSK_NIGHT_PERIODS, "timers stopped again at night");
    const fetched = page.fetchCalls.length;
    await page.fire(15000); // no timer left to fire
    assert.equal(page.fetchCalls.length, fetched);
    // One cover only, however many phase checks run.
    await page.fire(60000);
    assert.equal(page.body.children.filter((c) => c.id === "night-cover").length, 1);
  }

  // ── Custom / overnight schedule (night=START-END) ─────────────────────────
  {
    const page = loadRoom({ search: "?kiosk=1&night=22-6", hour: 21 });
    await page.flush();
    assert.equal(page.cover(), null, "21:00 is day under night=22-6");
    for (const [hour, dark] of [[22, true], [23, true], [0, true], [5, true], [6, false], [12, false], [21, false]]) {
      page.setHour(hour);
      await page.fire(60000);
      assert.equal(Boolean(page.cover()), dark, `night=22-6 at ${hour}:00`);
    }
  }
  {
    // A same-day window (not wrapping midnight), e.g. a daytime quiet period.
    const page = loadRoom({ search: "?kiosk=1&night=13-15", hour: 12 });
    await page.flush();
    assert.equal(page.cover(), null);
    page.setHour(13);
    await page.fire(60000);
    assert.ok(page.cover(), "13:00 is dark under night=13-15");
    page.setHour(15);
    await page.fire(60000);
    assert.equal(page.cover(), null, "15:00 is light again");
  }
  {
    // A malformed night= must not leave the kiosk always-dark or never-dark.
    const dark = loadRoom({ search: "?kiosk=1&night=nonsense", hour: 22 });
    await dark.flush();
    assert.ok(dark.cover(), "falls back to the 20-8 default");
    const light = loadRoom({ search: "?kiosk=1&night=nonsense", hour: 12 });
    await light.flush();
    assert.equal(light.cover(), null);
  }

  // ── CSS: the cover must also freeze the infinite ticker animation ─────────
  assert.match(css, /animation:\s*ticker-scroll[^;]*infinite/, "ticker still animates by day");
  const rule = css.match(/\.kiosk-night\s*\*[^{]*\{([^}]*)\}/);
  assert.ok(rule, "room.css has a .kiosk-night rule covering all descendants");
  assert.match(rule[1], /animation-play-state:\s*paused\s*!important/);
  assert.match(rule[1], /transition:\s*none\s*!important/);
  assert.match(css, /\.kiosk-night\s*\*::before/);
  assert.match(css, /\.kiosk-night\s*\*::after/);

  console.log("room kiosk blackout tests passed");
})().catch((error) => {
  console.error(error);
  process.exit(1);
});
