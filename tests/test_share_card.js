// The status card is intentionally a one-way privacy boundary. Exercise the
// small allowlisted model directly so a future design change cannot start
// exporting live readings, network details, or animal names by accident.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const root = path.resolve(__dirname, "..");
const source = fs.readFileSync(path.join(root, "frontend", "app.js"), "utf8");
const start = source.indexOf("// ── Privacy-safe room status card");
const end = source.indexOf("function shareCardCanvas", start);
assert.ok(start > 0 && end > start, "share-card privacy model is missing from app.js");

const context = { console };
vm.createContext(context);
vm.runInContext(source.slice(start, end), context, { filename: "share-card-model.js" });
const shareCardModel = vm.runInContext("shareCardModel", context);

const fixture = {
  counts: { ok: 2, warning: 1, danger: 0, stale: 1, no_data: 1, no_ranges: 0 },
  enclosures: [
    {
      id: "PRIVATE-ENCLOSURE-ID",
      name: "PRIVATE-ANIMAL-NAME",
      status: "warning",
      warm: { temp: 99.9, mac: "PRIVATE-SENSOR-MAC" },
      sensors: [{ mac: "PRIVATE-SENSOR-MAC", rssi: -42 }],
      local_ip: "192.168.99.99",
      history: "PRIVATE-HISTORY",
      credential: "PRIVATE-CREDENTIAL",
    },
  ],
  room_climate: { ip: "10.0.0.8", api_key: "PRIVATE-API-KEY" },
};

const privateModel = shareCardModel(fixture);
const privateText = JSON.stringify(privateModel);
for (const forbidden of [
  "PRIVATE-ANIMAL-NAME", "PRIVATE-ENCLOSURE-ID", "PRIVATE-SENSOR-MAC",
  "192.168.99.99", "PRIVATE-HISTORY", "PRIVATE-CREDENTIAL", "PRIVATE-API-KEY",
  "99.9", "-42",
]) {
  assert.ok(!privateText.includes(forbidden), `${forbidden} must not enter the default card model`);
}
assert.equal(privateModel.namesIncluded, false);
assert.deepEqual(JSON.parse(JSON.stringify(privateModel.counts)), {
  ok: 2, check: 1, alert: 0, waiting: 2,
});
assert.match(privateModel.headline, /2 enclosures to check/);

const namedModel = shareCardModel(fixture, true);
const namedText = JSON.stringify(namedModel);
assert.equal(namedModel.namesIncluded, true);
assert.equal(namedModel.enclosures[0].name, "PRIVATE-ANIMAL-NAME");
for (const forbidden of [
  "PRIVATE-ENCLOSURE-ID", "PRIVATE-SENSOR-MAC", "192.168.99.99",
  "PRIVATE-HISTORY", "PRIVATE-CREDENTIAL", "PRIVATE-API-KEY", "99.9", "-42",
]) {
  assert.ok(!namedText.includes(forbidden), `${forbidden} must not enter the opt-in card model`);
}

// Names are capped both for layout safety and to prevent a malformed imported
// config from allocating an unbounded canvas string.
const many = shareCardModel({
  counts: { ok: 20 },
  enclosures: Array.from({ length: 20 }, (_, index) => ({
    name: `Enclosure ${index} ${"x".repeat(100)}`,
    status: "ok",
  })),
}, true);
assert.equal(many.enclosures.length, 9);
assert.equal(many.hiddenEnclosures, 11);
assert.ok(many.enclosures.every(item => item.name.length <= 42));

// The named layout is a fixed three columns × three rows. Keeping the cap at
// nine reserves the bottom 40 px for branding and the overflow count rather
// than letting a fourth row collide with the footer.
assert.match(source, /const SHARE_NAME_LIMIT = 9/);
assert.match(source, /\+\$\{model\.hiddenEnclosures\} more`, 52, 590/);
assert.match(source, /ctx\.fillText\(model\.site, 1148, 598\)/);
assert.match(source, /const name = truncateText\(item\.name, 220\)/,
  "long or wide enclosure names must be clipped by rendered width");

// The UI must require a fresh, visible opt-in. It is deliberately not a saved
// setting, and the rendered checkbox must never arrive pre-checked.
const checkbox = source.match(/<input type="checkbox" id="share-card-names"[^>]*>/)?.[0];
assert.ok(checkbox, "share-card name opt-in is missing");
assert.ok(!/\bchecked\b/.test(checkbox), "enclosure names must be hidden by default");
assert.match(source, /generated entirely\s+in this browser and is never uploaded by Bask/);
assert.match(source, /https:\/\/ko-fi\.com\/jlyfshhh/);
assert.match(source, /https:\/\/instagram\.com\/thebioactivekeeper/);

// The rendered card is information, not decoration. Its dynamic alternative
// text must expose the same headline and status totals to screen-reader users.
assert.match(source, /preview\.alt = `Bask room status card: \$\{model\.headline\}/);
assert.match(source, /\$\{model\.counts\.ok\} in range/);
assert.match(source, /Enclosure names \$\{includeNames \? "included" : "hidden"\}/);
assert.match(source, /applyShareCardPreview\(snapshot\)/,
  "the downloaded/shared file must refresh the preview from the same snapshot");

console.log("Privacy-safe share card tests passed.");
