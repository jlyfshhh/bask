// The solar day/night settings UI, evaluated without a browser. Same approach
// as the other frontend checks here: pull the pure functions out of app.js and
// exercise them directly, so a rendering mistake is caught in CI rather than on
// someone's dashboard.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(__dirname, "..", "frontend", "app.js"), "utf8");
const start = source.indexOf("// ── Day and night from the sun");
assert.ok(start > 0, "solar settings block is missing from app.js");
const block = source.slice(start, source.indexOf("// ── editor sheet plumbing"));

const context = { console };
vm.createContext(context);
vm.runInContext(block + "\nfunction idAttr(v){return v;}", context);

// --- offset labels -----------------------------------------------------------
assert.equal(context.fmtOffset(0), "at sunrise/sunset");
assert.equal(context.fmtOffset(30), "+30 min");
// A negative offset must read as negative; a bare minus glyph mix-up here is
// the difference between lights on before dawn and an hour after it.
assert.match(context.fmtOffset(-45), /45 min/);
assert.ok(context.fmtOffset(-45) !== context.fmtOffset(45), "sign must be visible");

// --- unplaced install --------------------------------------------------------
const unplaced = context.solarDayFields({ day_mode: "solar" });
assert.match(unplaced, /solar-zip/, "must offer a ZIP field");
assert.match(unplaced, /fall back to the set hours/, "must say what happens with no location");
assert.doesNotMatch(unplaced, /Using/, "nothing to report before a location is set");

// --- placed install ----------------------------------------------------------
const placed = context.solarDayFields({
  day_mode: "solar", latitude: 40.7128, longitude: -74.006,
  location_label: "10001", sunrise_offset_minutes: 15, sunset_offset_minutes: -30,
});
assert.match(placed, /Using 10001/, "should say where it thinks it is");
assert.match(placed, /40\.71, -74\.01/, "coordinates shown so a wrong ZIP is visible");
assert.match(placed, /\+15 min/);
assert.match(placed, /30 min/);

// A location with no label still reports its coordinates rather than "undefined".
const unlabelled = context.solarDayFields({ day_mode: "solar", latitude: 21.3, longitude: -157.8 });
assert.match(unlabelled, /Using 21\.30, -157\.80/);
assert.doesNotMatch(unlabelled, /undefined/);

// Offsets must be clamped to what the API accepts, so the control cannot ask
// for a value that will be refused.
assert.match(block, /Math\.max\(-180, Math\.min\(180,/);

console.log("solar settings UI: ok");
