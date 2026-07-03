// ============================================================
//  Bask — dashboard front-end (vanilla JS, no build step)
// ============================================================
const REFRESH_MS = 15000;

let _dash = null;
let _species = [];
let _sensors = [];
let _enclosures = [];
let _tempUnit = "F";
const NIGHT_FIELDS = ["night_warm_temp_min", "night_warm_temp_max", "night_cool_temp_min",
                      "night_cool_temp_max", "night_humidity_min", "night_humidity_max"];

// ── helpers ──────────────────────────────────────────────────
async function api(method, url, body) {
  const opt = { method, headers: { "Content-Type": "application/json" } };
  if (body !== undefined) opt.body = JSON.stringify(body);
  const res = await fetch(url, opt);
  if (!res.ok) throw new Error(`${method} ${url} -> ${res.status}`);
  return res.status === 204 ? null : res.json();
}
function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, c =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function fmtAge(sec) {
  if (sec == null) return "never";
  if (sec < 60) return `${sec}s ago`;
  if (sec < 3600) return `${Math.floor(sec / 60)}m ago`;
  if (sec < 86400) return `${Math.floor(sec / 3600)}h ago`;
  return `${Math.floor(sec / 86400)}d ago`;
}
const STATUS_LABEL = {
  ok: "OK", warning: "Check", danger: "Alert",
  stale: "Stale", no_data: "No data", no_ranges: "No range",
};

// ── clock ────────────────────────────────────────────────────
function tickClock() {
  const d = new Date();
  document.getElementById("clock").textContent =
    d.toLocaleDateString([], { weekday: "short", month: "short", day: "numeric" }) + "  " +
    d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

// ── dashboard ────────────────────────────────────────────────
async function refreshDashboard() {
  try {
    const data = await api("GET", "/api/dashboard");
    _dash = data;
    _tempUnit = data.temp_unit;
    renderSummary(data.counts);
    renderStatusBanner(data);
    renderPeriod(data);
    renderThermostats(data);
    renderGrid(data);
    const t = new Date(data.updated_at * 1000);
    document.getElementById("updated").textContent =
      "Updated " + t.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  } catch (e) {
    document.getElementById("updated").textContent = "⚠ connection lost — retrying";
  }
}

function renderSummary(counts) {
  const attention = (counts.danger || 0) + (counts.warning || 0);
  const parts = [];
  if (attention === 0 && (counts.stale || 0) === 0 && (counts.ok || 0) > 0) {
    parts.push(`<span class="pill allgood"><span class="dot"></span>All ${counts.ok} good</span>`);
  } else {
    if (counts.danger)  parts.push(pill("danger",  counts.danger,  "alert"));
    if (counts.warning) parts.push(pill("warning", counts.warning, "check"));
    if (counts.ok)      parts.push(pill("ok",      counts.ok,      "ok"));
    if (counts.stale)   parts.push(pill("stale",   counts.stale,   "stale"));
    const other = (counts.no_data || 0) + (counts.no_ranges || 0);
    if (other)          parts.push(pill("stale",   other,          "unconfig"));
  }
  document.getElementById("summary").innerHTML = parts.join("");
}
function pill(cls, n, label) {
  return `<span class="pill ${cls}"><span class="dot"></span>${n} ${label}</span>`;
}

// Big room-status banner: answers "is my husbandry OK?" from across the room.
// Green only when every CONFIGURED enclosure is in range; red/amber the moment
// one is out of range or has lost signal. Un-paired (no-data) enclosures are
// ignored so building out the room doesn't trip a false alarm.
function renderStatusBanner(data) {
  const el = document.getElementById("status-banner");
  if (!el) return;
  const okCount = data.counts.ok || 0;
  const problems = data.enclosures.filter(
    e => e.status === "danger" || e.status === "warning" || e.status === "stale");
  const lowBatt = data.enclosures.filter(e => e.low_battery).map(e => e.name);
  const battNote = lowBatt.length
    ? `<span class="sb-batt">🔋 low: ${lowBatt.map(esc).join(", ")}</span>` : "";

  if (problems.length === 0) {
    if (okCount === 0) {
      el.className = "status-banner idle";
      el.innerHTML = `<span class="sb-text">Waiting for sensors…</span>${battNote}`;
      return;
    }
    el.className = "status-banner good";
    el.innerHTML = `<span class="sb-icon">✓</span><span class="sb-text">All Good</span>
      <span class="sb-sub">${okCount} enclosure${okCount !== 1 ? "s" : ""} in range</span>${battNote}`;
    return;
  }

  const anyDanger = problems.some(e => e.status === "danger");
  el.className = "status-banner " + (anyDanger ? "danger" : "warn");
  const descs = problems.map(e => {
    if (e.status === "stale") return `${esc(e.name)}: no signal`;
    const issues = [];
    if (e.warm_temp_ok === false) issues.push("warm");
    if (e.cool_temp_ok === false) issues.push("cool");
    if (e.humidity_ok === false) issues.push("humidity");
    return `${esc(e.name)}: ${issues.join(" + ") || "out of range"}`;
  });
  el.innerHTML = `<span class="sb-icon">⚠</span>
    <span class="sb-text">Check ${problems.length}</span>
    <span class="sb-sub">${descs.join("  ·  ")}</span>${battNote}`;
}

// Day/night indicator — shows which range set is currently being applied.
function fmtHour(h) {
  const ap = h < 12 ? "a" : "p";
  return (h % 12 || 12) + ap;
}
function renderPeriod(data) {
  const el = document.getElementById("period");
  if (!el || !data.period) return;
  const isDay = data.period === "day";
  const win = `${fmtHour(data.day_start_hour)}–${fmtHour(data.day_end_hour)}`;
  el.className = "period-badge " + (isDay ? "day" : "night");
  el.innerHTML = `<span class="pi-ico">${isDay ? "☀️" : "🌙"}</span>` +
                 `<span class="pi-txt">${isDay ? "Day" : "Night"}</span>`;
  el.title = isDay ? `Day ranges (${win})` : `Night ranges (outside ${win})`;
}

// Compact Herpstat thermostat strip: live probe temp → setpoint, output %, alarms.
function renderThermostats(data) {
  const el = document.getElementById("thermostat-strip");
  if (!el) return;
  const units = data.thermostats || [];
  if (!units.length) { el.style.display = "none"; return; }
  el.style.display = "flex";
  const u = "°" + _tempUnit;
  const chips = [];
  for (const unit of units) {
    if (!unit.reachable) {
      chips.push(`<div class="tchip offline"><span class="tc-name">${esc(unit.name)}</span>` +
                 `<span class="tc-sub">offline</span></div>`);
      continue;
    }
    for (const o of unit.outputs) {
      chips.push(`<div class="tchip ${o.alarm ? "alarm" : "ok"}">
        <span class="tc-name">${esc(o.name)}</span>
        <span class="tc-temp">${o.temp ?? "—"}${u}</span>
        <span class="tc-sub">→ ${o.setpoint ?? "—"}${u} · ${o.output_pct ?? 0}%${o.heating ? " 🔥" : ""}${o.error ? " · " + esc(o.error) : ""}</span>
      </div>`);
    }
  }
  el.innerHTML = `<span class="tstat-label">Thermostats</span>${chips.join("")}`;
}

function renderGrid(data) {
  const cards = [
    ...data.enclosures.map(encCardHTML),
    ...data.ungrouped.map(soloCardHTML),
  ];
  document.getElementById("grid").innerHTML = cards.length
    ? cards.join("")
    : `<div class="empty-grid">No enclosures yet.<br>Tap <b>⚙ Manage</b> to add sensors and enclosures.</div>`;
}

function metric(label, value, unit, bad, cls = "") {
  if (value == null) {
    return `<div class="metric ${cls}"><div class="metric-label">${esc(label)}</div>
            <div class="metric-none">—</div></div>`;
  }
  return `<div class="metric ${cls} ${bad ? "bad" : ""}">
    <div class="metric-label">${esc(label)}</div>
    <div class="metric-value">${value}<span class="metric-unit">${unit}</span></div>
  </div>`;
}

function encCardHTML(e) {
  const flagging = e.status === "warning" || e.status === "danger";
  const bad = ok => flagging && ok === false;
  const warm = e.warm, cool = e.cool;
  const u = "°" + _tempUnit;

  const body = `
    <div class="enc-body">
      ${metric(warm?.position || "Warm", warm ? warm.temp : null, u, bad(e.warm_temp_ok))}
      ${metric("Humidity", cool ? cool.humidity : null, "%", bad(e.humidity_ok), "mid")}
      ${metric(cool?.position || "Cool", cool ? cool.temp : null, u, bad(e.cool_temp_ok))}
    </div>`;

  const flags = [];
  if (e.low_battery) flags.push(`<span class="flag low-batt">🔋 low</span>`);
  if (e.status === "stale" || e.status === "no_data")
    flags.push(`<span class="flag stale-flag">no signal</span>`);

  return `
    <div class="enc-card ${e.status}" onclick="openDetail('${e.id}')">
      <div class="enc-head">
        <div class="enc-title">
          <div class="enc-name">${esc(e.name)}</div>
          ${e.species_name ? `<div class="enc-species">${esc(e.species_name)}</div>` : ""}
        </div>
        <div class="status-badge"><span class="bdot"></span>${STATUS_LABEL[e.status] || e.status}</div>
      </div>
      ${body}
      <div class="enc-foot">
        <span>${fmtAge(e.age_seconds)}</span>
        <span class="foot-flags">${flags.join("")}</span>
      </div>
    </div>`;
}

function soloCardHTML(s) {
  const status = s.temp == null ? "no_data" : s.stale ? "stale" : "ok";
  const u = "°" + _tempUnit;
  return `
    <div class="enc-card solo ${status}" onclick="openDetailSolo('${s.mac}')">
      <div class="enc-head">
        <div class="enc-title">
          <div class="enc-name">${esc(s.name)}</div>
          ${s.species ? `<div class="enc-species">${esc(s.species)}</div>` : ""}
        </div>
        <div class="status-badge"><span class="bdot"></span>${STATUS_LABEL[status]}</div>
      </div>
      <div class="enc-body">
        ${metric("Temp", s.temp, u, false)}
        ${metric("Humidity", s.humidity, "%", false, "mid")}
      </div>
      <div class="enc-foot"><span>${fmtAge(s.age_seconds)}</span>
        <span class="foot-flags">${s.low_battery ? '<span class="flag low-batt">🔋 low</span>' : ""}</span>
      </div>
    </div>`;
}

// ── detail sheet ─────────────────────────────────────────────
function openDetail(encId) {
  const e = _dash?.enclosures.find(x => x.id === encId);
  if (!e) return;
  const sp = _species.find(s => s.id === e.species_id);
  const u = "°" + _tempUnit;
  const isDay = _dash?.period !== "night";
  const hasNight = !!(sp && NIGHT_FIELDS.some(k => sp[k] != null));
  const ar = dk => !sp ? null : ((isDay || !hasNight) ? sp[dk] : (sp["night_" + dk] ?? null));
  const rng = (lo, hi, unit) =>
    (lo == null && hi == null) ? "" :
    `<div class="dm-range">ok ${lo ?? "–"}–${hi ?? "–"}${unit}</div>`;

  const dm = (label, val, unit, bad, range) => `
    <div class="dm ${bad ? "bad" : ""}">
      <div class="dm-label">${esc(label)}</div>
      <div class="dm-value">${val == null ? "—" : val + unit}</div>
      ${range}
    </div>`;

  const flagging = e.status === "warning" || e.status === "danger";
  const bad = ok => flagging && ok === false;

  const rows = e.sensors.map(s => `
    <div class="drow"><span>${esc(s.position || s.name)}</span>
      <span>${s.temp == null ? "—" : s.temp + u} · ${s.humidity == null ? "—" : s.humidity + "%"}
      ${s.battery != null ? ` · 🔋${s.battery}%` : ""} ${s.rssi != null ? ` · ${s.rssi}dBm` : ""}
      · ${fmtAge(s.age_seconds)}</span></div>`).join("");

  document.getElementById("detail-sheet").innerHTML = `
    <div class="sheet-head">
      <div style="flex:1">
        <h2>${esc(e.name)}</h2>
        <div class="sheet-sub">${esc(e.species_name || "No species set")} · ${STATUS_LABEL[e.status]}${sp ? " · " + (isDay ? "☀️ day" : "🌙 night") + " ranges" : ""}</div>
      </div>
      <button class="close-btn" onclick="closeDetail()">✕</button>
    </div>
    <div class="detail-metrics">
      ${dm(e.warm?.position || "Warm", e.warm?.temp ?? null, u, bad(e.warm_temp_ok),
           sp ? rng(ar("warm_temp_min"), ar("warm_temp_max"), u) : "")}
      ${dm("Humidity", e.cool?.humidity ?? null, "%", bad(e.humidity_ok),
           sp ? rng(ar("humidity_min"), ar("humidity_max"), "%") : "")}
      ${dm(e.cool?.position || "Cool", e.cool?.temp ?? null, u, bad(e.cool_temp_ok),
           sp ? rng(ar("cool_temp_min"), ar("cool_temp_max"), u) : "")}
    </div>
    <div class="detail-rows">${rows}</div>
    <div class="form-actions">
      <button class="btn" onclick="closeDetail(); openManage('enclosures'); setTimeout(()=>editEnclosure('${e.id}'),60)">Edit enclosure</button>
    </div>`;
  document.getElementById("detail").classList.add("open");
}
function openDetailSolo(mac) {
  const s = _dash?.ungrouped.find(x => x.mac === mac);
  if (!s) return;
  const u = "°" + _tempUnit;
  document.getElementById("detail-sheet").innerHTML = `
    <div class="sheet-head"><div style="flex:1"><h2>${esc(s.name)}</h2>
      <div class="sheet-sub">Unassigned sensor</div></div>
      <button class="close-btn" onclick="closeDetail()">✕</button></div>
    <div class="detail-metrics">
      <div class="dm"><div class="dm-label">Temp</div><div class="dm-value">${s.temp == null ? "—" : s.temp + u}</div></div>
      <div class="dm"><div class="dm-label">Humidity</div><div class="dm-value">${s.humidity == null ? "—" : s.humidity + "%"}</div></div>
      <div class="dm"><div class="dm-label">Battery</div><div class="dm-value">${s.battery == null ? "—" : s.battery + "%"}</div></div>
    </div>
    <div class="detail-rows"><div class="drow"><span>MAC</span><span>${esc(s.mac)}</span></div>
      <div class="drow"><span>Last seen</span><span>${fmtAge(s.age_seconds)}</span></div></div>`;
  document.getElementById("detail").classList.add("open");
}
function closeDetail() { document.getElementById("detail").classList.remove("open"); }

// ── manage overlay ───────────────────────────────────────────
async function openManage(tab) {
  await loadManageData();
  switchTab(tab || "enclosures");
  document.getElementById("manage").classList.add("open");
}
function closeManage() {
  document.getElementById("manage").classList.remove("open");
  refreshDashboard();
}
function switchTab(name) {
  document.querySelectorAll(".mtab").forEach(b => b.classList.toggle("active", b.dataset.tab === name));
  document.querySelectorAll(".pane").forEach(p => p.classList.toggle("active", p.id === `pane-${name}`));
  if (name === "sensors") startDiscovery();
}

async function loadManageData() {
  const [sres, eres, spres, tres] = await Promise.all([
    api("GET", "/api/sensors"), api("GET", "/api/enclosures"),
    api("GET", "/api/species"), api("GET", "/api/thermostats"),
  ]);
  _sensors = sres.sensors; _enclosures = eres.enclosures; _species = spres.species;
  _thermostats_cfg = tres.thermostats;
  _settings = sres.settings;
  renderEnclosuresPane();
  renderSensorsPane();
  renderSpeciesPane();
  renderThermostatsPane();
  renderSettingsPane();
}
let _settings = {};
let _thermostats_cfg = [];

// ── Enclosures pane ──────────────────────────────────────────
function renderEnclosuresPane() {
  const sName = Object.fromEntries(_sensors.map(s => [s.mac.toUpperCase(), s.name]));
  const spName = Object.fromEntries(_species.map(s => [s.id, s.name]));
  const list = _enclosures.map((e, i) => `
    <div class="row">
      <div class="row-top">
        <div class="row-reorder">
          <button class="btn icon" ${i === 0 ? "disabled" : ""} onclick="moveEnclosure('${e.id}',-1)">▲</button>
          <button class="btn icon" ${i === _enclosures.length - 1 ? "disabled" : ""} onclick="moveEnclosure('${e.id}',1)">▼</button>
        </div>
        <div class="row-info">
          <div class="row-name">${esc(e.name)}</div>
          <div class="row-sub">${esc(spName[e.species_id] || "No species")}</div>
        </div>
        <button class="btn sm" onclick="editEnclosure('${e.id}')">Edit</button>
      </div>
      ${e.sensors.length ? `<div class="row-tags">${e.sensors.map(s =>
        `<span class="tag"><b>${esc(s.position)}</b> · ${esc(sName[s.mac.toUpperCase()] || s.mac)}</span>`).join("")}</div>` : ""}
    </div>`).join("");
  document.getElementById("pane-enclosures").innerHTML = `
    <div class="pane-toolbar"><h2>Enclosures</h2>
      <button class="btn primary" onclick="editEnclosure(null)">+ New</button></div>
    ${list || `<div class="muted-note">No enclosures yet. Add sensors first, then group them here.</div>`}`;
}

async function moveEnclosure(id, dir) {
  const ids = _enclosures.map(e => e.id);
  const i = ids.indexOf(id);
  const j = i + dir;
  if (j < 0 || j >= ids.length) return;
  [ids[i], ids[j]] = [ids[j], ids[i]];
  _enclosures = ids.map(x => _enclosures.find(e => e.id === x));
  renderEnclosuresPane();
  await api("PUT", "/api/enclosures/reorder", { order: ids });
}

function editEnclosure(id) {
  const enc = id ? _enclosures.find(e => e.id === id) : null;
  const slots = enc?.sensors?.length ? enc.sensors : [{ mac: "", position: "" }];
  const spOpts = (sel) => `<option value="">— No species / no ranges —</option>` +
    _species.map(s => `<option value="${s.id}" ${s.id === sel ? "selected" : ""}>${esc(s.name)}</option>`).join("");
  openEditor(`
    <div class="sheet-head"><h2>${enc ? "Edit" : "New"} enclosure</h2>
      <button class="close-btn" onclick="closeEditor()">✕</button></div>
    <div class="field"><label>Name</label>
      <input type="text" id="ef-name" value="${esc(enc?.name || "")}" placeholder="e.g. Achilles"></div>
    <div class="field"><label>Species (sets acceptable ranges)</label>
      <select id="ef-species">${spOpts(enc?.species_id)}</select></div>
    <div class="field"><label>Sensors &amp; positions</label>
      <div id="ef-slots">${slots.map(slotHTML).join("")}</div>
      <button class="btn ghost sm" onclick="addSlot()">+ Add sensor slot</button></div>
    <div class="form-actions">
      ${enc ? `<button class="btn danger" onclick="deleteEnclosure('${enc.id}')">Delete</button>` : ""}
      <button class="btn primary" onclick="saveEnclosure(${enc ? `'${enc.id}'` : "null"})">Save</button>
    </div>`);
}
function slotHTML(slot) {
  const opts = `<option value="">— Select sensor —</option>` + _sensors.map(s =>
    `<option value="${esc(s.mac)}" ${s.mac.toUpperCase() === (slot.mac || "").toUpperCase() ? "selected" : ""}>${esc(s.name)}</option>`).join("");
  return `<div class="slot">
    <select class="ef-mac">${opts}</select>
    <input type="text" class="ef-pos" placeholder="Position (Warm / Cool)" value="${esc(slot.position || "")}">
    <button class="btn icon" onclick="this.closest('.slot').remove()">✕</button>
  </div>`;
}
function addSlot() { document.getElementById("ef-slots").insertAdjacentHTML("beforeend", slotHTML({})); }

async function saveEnclosure(id) {
  const name = document.getElementById("ef-name").value.trim();
  if (!name) return;
  const species_id = document.getElementById("ef-species").value || null;
  const sensors = [...document.querySelectorAll("#ef-slots .slot")].map(r => ({
    mac: r.querySelector(".ef-mac").value,
    position: r.querySelector(".ef-pos").value.trim(),
  })).filter(s => s.mac && s.position);
  const body = { name, species_id, sensors };
  if (id) await api("PUT", `/api/enclosures/${id}`, body);
  else await api("POST", "/api/enclosures", body);
  closeEditor(); await loadManageData();
}
async function deleteEnclosure(id) {
  if (!confirm("Delete this enclosure? Sensors are not deleted.")) return;
  await api("DELETE", `/api/enclosures/${id}`);
  closeEditor(); await loadManageData();
}

// ── Sensors pane (discovery + configured) ────────────────────
let _discTimer = null;
function renderSensorsPane() {
  const rows = _sensors.map(s => `
    <div class="row"><div class="row-top">
      <div class="row-info"><div class="row-name">${esc(s.name)}</div>
        ${s.species ? `<div class="row-sub">${esc(s.species)}</div>` : ""}
        <div class="row-mac">${esc(s.mac)}</div></div>
      <button class="btn sm" onclick="editSensor('${s.mac}')">Edit</button>
    </div></div>`).join("");
  document.getElementById("pane-sensors").innerHTML = `
    <div class="pane-toolbar"><h2>Sensors</h2>
      <button class="btn primary" onclick="openPair()">⌖ Pair by proximity</button></div>
    <div class="scan-hint">Hold a sensor near the Pi and tap <b>⌖ Pair by proximity</b> to assign it to an enclosure,
      or tap <b>Add</b> below to just track one.</div>
    <div id="found-list"><div class="muted-note">Listening…</div></div>
    <div class="pane-toolbar" style="margin-top:18px"><h2>Tracked (${_sensors.length})</h2></div>
    ${rows || `<div class="muted-note">No sensors added yet.</div>`}`;
}
function startDiscovery() {
  pollDiscovery();
  if (_discTimer) clearInterval(_discTimer);
  _discTimer = setInterval(pollDiscovery, 4000);
}
async function pollDiscovery() {
  if (!document.getElementById("pane-sensors").classList.contains("active")) {
    clearInterval(_discTimer); _discTimer = null; return;
  }
  try {
    const { devices } = await api("GET", "/api/discovered");
    const list = document.getElementById("found-list");
    if (!list) return;
    const fresh = devices.filter(d => !d.already_configured);
    _found = fresh;
    list.innerHTML = fresh.length ? fresh.map((d, i) => `
      <div class="found">
        <div class="found-info"><div class="found-name">${esc(d.name)}</div>
          <div class="found-mac">${esc(d.mac)} · <span class="sig">${d.rssi ?? "?"} dBm</span></div></div>
        ${d.temp != null ? `<div class="found-read">${d.temp}°${d.temp_unit} · ${d.humidity}%</div>` : ""}
        <button class="btn primary sm" onclick="addFound(${i})">Add</button>
      </div>`).join("")
      : `<div class="muted-note">No new sensors nearby. Make sure they're powered on and within range.</div>`;
  } catch (e) { /* scanner may be offline; leave hint */ }
}
let _found = [];
async function addFound(i) {
  const d = _found[i];
  if (!d) return;
  const name = prompt("Name this sensor:", d.name || d.mac);
  if (!name) return;
  await api("POST", "/api/sensors", { mac: d.mac, name: name.trim(), species: null });
  await loadManageData();
}
function editSensor(mac) {
  const s = _sensors.find(x => x.mac === mac);
  if (!s) return;
  openEditor(`
    <div class="sheet-head"><h2>Edit sensor</h2><button class="close-btn" onclick="closeEditor()">✕</button></div>
    <div class="field"><label>Name</label><input type="text" id="sf-name" value="${esc(s.name)}"></div>
    <div class="field"><label>Species (optional label)</label><input type="text" id="sf-species" value="${esc(s.species || "")}"></div>
    <div class="row-mac" style="margin-bottom:14px">${esc(s.mac)}</div>
    <div class="form-actions">
      <button class="btn danger" onclick="deleteSensor('${s.mac}')">Delete</button>
      <button class="btn primary" onclick="saveSensor('${s.mac}')">Save</button></div>`);
}
async function saveSensor(mac) {
  const name = document.getElementById("sf-name").value.trim();
  if (!name) return;
  const species = document.getElementById("sf-species").value.trim() || null;
  await api("PUT", `/api/sensors/${mac}`, { name, species });
  closeEditor(); await loadManageData();
}
async function deleteSensor(mac) {
  if (!confirm("Delete this sensor? It will be removed from any enclosure too.")) return;
  await api("DELETE", `/api/sensors/${mac}`);
  closeEditor(); await loadManageData();
}

// ── Pair-by-proximity wizard ─────────────────────────────────
// Hold a sensor near the Pi; the strongest-signal unconfigured device floats to
// the top, then one tap drops it into an enclosure's Warm or Cool slot.
let _pairTimer = null;
let _pairNearest = null;
let _pairEnc = [];        // enclosures (fresh, with filled slots)
let _pairNewOpen = false;

function sigBars(rssi) {
  const lvl = rssi == null ? 0 : rssi >= -55 ? 4 : rssi >= -67 ? 3 : rssi >= -78 ? 2 : 1;
  return `<span class="bars b${lvl}"><i></i><i></i><i></i><i></i></span>`;
}
function isWarmPos(p) { return /warm|hot|bask/i.test(p || ""); }

async function openPair() {
  await pairLoadEnc();
  document.getElementById("manage").classList.remove("open"); // come back to it on close
  document.getElementById("pair").classList.add("open");
  renderPairTargets();
  pairPoll();
  if (_pairTimer) clearInterval(_pairTimer);
  _pairTimer = setInterval(pairPoll, 2000);
}
function closePair() {
  if (_pairTimer) { clearInterval(_pairTimer); _pairTimer = null; }
  document.getElementById("pair").classList.remove("open");
  loadManageData();
  document.getElementById("manage").classList.add("open");
}
async function pairLoadEnc() {
  const [eres, spres] = await Promise.all([
    api("GET", "/api/enclosures"), api("GET", "/api/species"),
  ]);
  _pairEnc = eres.enclosures; _species = spres.species;
}

async function pairPoll() {
  try {
    const { devices } = await api("GET", "/api/discovered");
    const fresh = devices.filter(d => !d.already_configured);
    _pairNearest = fresh.length ? fresh[0] : null;  // API sorts by rssi desc
    renderPairNearest();
  } catch (e) { /* scanner may be briefly offline */ }
}

function renderPairNearest() {
  const el = document.getElementById("pair-nearest");
  const d = _pairNearest;
  if (!d) {
    el.className = "pair-nearest empty";
    el.innerHTML = `<div class="pn-prompt">Hold an unpaired sensor within a few inches of the Pi…</div>`;
    return;
  }
  const close = d.rssi != null && d.rssi >= -60;
  el.className = "pair-nearest" + (close ? " close" : "");
  const reading = d.temp != null
    ? `<span class="pn-read">${d.temp}°${d.temp_unit} · ${d.humidity}%${d.battery != null ? ` · 🔋${d.battery}%` : ""}</span>`
    : `<span class="pn-read muted">reading…</span>`;
  el.innerHTML = `
    <div class="pn-label">Nearest sensor ${close ? "" : "<span class='pn-hint'>(bring it closer)</span>"}</div>
    <div class="pn-main">
      <div class="pn-id">${esc(d.name)}</div>
      ${sigBars(d.rssi)}
    </div>
    <div class="pn-meta">${reading}<span class="pn-rssi">${d.rssi ?? "?"} dBm</span></div>
    <div class="pn-mac">${esc(d.mac)}</div>`;
}

function renderPairTargets() {
  const el = document.getElementById("pair-targets");
  const sName = Object.fromEntries(_sensors.map(s => [s.mac.toUpperCase(), s.name]));
  const cards = _pairEnc.map(e => {
    const warm = e.sensors.find(s => isWarmPos(s.position));
    const cool = e.sensors.find(s => !isWarmPos(s.position));
    return `
      <div class="ptarget">
        <div class="pt-name">${esc(e.name)}</div>
        <div class="pt-sides">
          ${sideBtn(e.id, "warm", warm, sName)}
          ${sideBtn(e.id, "cool", cool, sName)}
        </div>
      </div>`;
  }).join("");
  const newForm = _pairNewOpen ? pairNewForm() : `
    <button class="btn ghost pt-new" onclick="pairToggleNew()">+ New enclosure</button>`;
  el.innerHTML = `<div class="pt-head">Tap a slot to assign the nearest sensor</div>${cards}${newForm}`;
}
function sideBtn(encId, side, slot, sName) {
  const filled = !!slot;
  const who = filled ? esc(sName[slot.mac.toUpperCase()] || slot.mac) : "";
  return `
    <button class="pt-side ${side} ${filled ? "filled" : "empty"}"
            onclick="pairAssign('${encId}','${side}')">
      <span class="pts-label">${side === "warm" ? "🔥 Warm" : "❄ Cool"}</span>
      <span class="pts-who">${filled ? "✓ " + who : "tap to set"}</span>
    </button>
    ${filled ? `<button class="pt-undo" onclick="event.stopPropagation();pairUndo('${encId}','${slot.mac}')" title="Clear">✕</button>` : ""}`;
}

async function pairAssign(encId, side) {
  if (!_pairNearest) { showToast("No sensor nearby — hold one to the Pi"); return; }
  const enc = _pairEnc.find(e => e.id === encId);
  const position = side === "warm" ? "Warm Side" : "Cool Side";
  const mac = _pairNearest.mac, devName = _pairNearest.name;
  try {
    const r = await api("POST", "/api/pair", { mac, enclosure_id: encId, position });
    showToast(`${devName} → ${enc.name} ${side}`);
    await pairLoadEnc();
    _sensors = (await api("GET", "/api/sensors")).sensors;
    _pairNearest = null; renderPairNearest();
    renderPairTargets();
    pairPoll();
  } catch (e) { showToast("Assign failed — try again"); }
}
async function pairUndo(encId, mac) {
  await api("POST", "/api/unpair", { mac, enclosure_id: encId, position: "" });
  await pairLoadEnc();
  _sensors = (await api("GET", "/api/sensors")).sensors;
  renderPairTargets();
}

function pairToggleNew() { _pairNewOpen = !_pairNewOpen; renderPairTargets(); }
function pairNewForm() {
  const spOpts = `<option value="">— No species —</option>` +
    _species.map(s => `<option value="${s.id}">${esc(s.name)}</option>`).join("");
  return `
    <div class="pt-newform">
      <input type="text" id="pn-name" placeholder="New enclosure name">
      <select id="pn-species">${spOpts}</select>
      <button class="btn primary" onclick="pairCreateEnc()">Create</button>
      <button class="btn ghost" onclick="pairToggleNew()">Cancel</button>
    </div>`;
}
async function pairCreateEnc() {
  const name = document.getElementById("pn-name").value.trim();
  if (!name) return;
  const species_id = document.getElementById("pn-species").value || null;
  await api("POST", "/api/enclosures", { name, species_id, sensors: [] });
  _pairNewOpen = false;
  await pairLoadEnc();
  renderPairTargets();
  showToast(`Created "${name}"`);
}

let _toastTimer = null;
function showToast(msg) {
  const t = document.getElementById("toast");
  t.textContent = msg; t.classList.add("show");
  if (_toastTimer) clearTimeout(_toastTimer);
  _toastTimer = setTimeout(() => t.classList.remove("show"), 2600);
}

// ── Species pane (ranges via +/- steppers) ───────────────────
function renderSpeciesPane() {
  const u = "°" + _tempUnit;
  const list = _species.map(s => `
    <div class="row"><div class="row-top">
      <div class="row-info"><div class="row-name">${esc(s.name)}</div>
        <div class="row-sub">Warm ${s.warm_temp_min ?? "–"}–${s.warm_temp_max ?? "–"}${u} ·
          Cool ${s.cool_temp_min ?? "–"}–${s.cool_temp_max ?? "–"}${u} ·
          Hum ${s.humidity_min ?? "–"}–${s.humidity_max ?? "–"}%</div></div>
      <button class="btn sm" onclick="editSpecies('${s.id}')">Edit</button>
    </div></div>`).join("");
  document.getElementById("pane-species").innerHTML = `
    <div class="pane-toolbar"><h2>Species &amp; ranges</h2>
      <button class="btn primary" onclick="editSpecies(null)">+ New</button></div>
    ${list || `<div class="muted-note">No species yet.</div>`}`;
}
function editSpecies(id) {
  const sp = id ? _species.find(s => s.id === id) : null;
  const u = "°" + _tempUnit;
  const nightOn = !!(sp && NIGHT_FIELDS.some(k => sp[k] != null));
  const nv = (nk, dk) => (sp && sp[nk] != null ? sp[nk] : sp?.[dk]);  // night value, default to day
  openEditor(`
    <div class="sheet-head"><h2>${sp ? "Edit" : "New"} species</h2>
      <button class="close-btn" onclick="closeEditor()">✕</button></div>
    <div class="field"><label>Name</label>
      <input type="text" id="spf-name" value="${esc(sp?.name || "")}" placeholder="e.g. Ball Python"></div>

    <div class="range-section">
      <div class="range-section-head">☀️ Day ranges</div>
      <div class="range-grid">
        <h3>Warm side temperature (${u})</h3>
        ${stepper("warm_temp_min", sp?.warm_temp_min, "Min", 1, 80)}
        ${stepper("warm_temp_max", sp?.warm_temp_max, "Max", 1, 95)}
        <h3>Cool side temperature (${u})</h3>
        ${stepper("cool_temp_min", sp?.cool_temp_min, "Min", 1, 75)}
        ${stepper("cool_temp_max", sp?.cool_temp_max, "Max", 1, 85)}
        <h3>Humidity (%)</h3>
        ${stepper("humidity_min", sp?.humidity_min, "Min", 5, 50)}
        ${stepper("humidity_max", sp?.humidity_max, "Max", 5, 70)}
      </div>
    </div>

    <label class="night-toggle">
      <input type="checkbox" id="spf-night-on" ${nightOn ? "checked" : ""}
        onchange="document.getElementById('spf-night-sec').style.display=this.checked?'block':'none'">
      <span>🌙 Different ranges at night</span>
    </label>
    <div class="range-section" id="spf-night-sec" style="display:${nightOn ? "block" : "none"}">
      <div class="range-grid">
        <h3>Warm side temperature (${u})</h3>
        ${stepper("night_warm_temp_min", nv("night_warm_temp_min", "warm_temp_min"), "Min", 1, 72)}
        ${stepper("night_warm_temp_max", nv("night_warm_temp_max", "warm_temp_max"), "Max", 1, 88)}
        <h3>Cool side temperature (${u})</h3>
        ${stepper("night_cool_temp_min", nv("night_cool_temp_min", "cool_temp_min"), "Min", 1, 68)}
        ${stepper("night_cool_temp_max", nv("night_cool_temp_max", "cool_temp_max"), "Max", 1, 80)}
        <h3>Humidity (%)</h3>
        ${stepper("night_humidity_min", nv("night_humidity_min", "humidity_min"), "Min", 5, 50)}
        ${stepper("night_humidity_max", nv("night_humidity_max", "humidity_max"), "Max", 5, 70)}
      </div>
    </div>

    <div class="form-actions">
      ${sp ? `<button class="btn danger" onclick="deleteSpecies('${sp.id}')">Delete</button>` : ""}
      <button class="btn primary" onclick="saveSpecies(${sp ? `'${sp.id}'` : "null"})">Save</button></div>`);
}
function stepper(key, val, label, step, dflt) {
  const has = val != null;
  return `<div class="field">
    <label>${label}</label>
    <div class="stepper" id="st-${key}" data-val="${has ? val : ""}" data-step="${step}" data-default="${dflt}">
      <button class="step-btn" onclick="stepVal('${key}',-1)">−</button>
      <div class="sval ${has ? "" : "unset"}">${has ? val : "off"}</div>
      <button class="step-btn" onclick="stepVal('${key}',1)">+</button>
    </div></div>`;
}
function stepVal(key, dir) {
  const el = document.getElementById("st-" + key);
  const step = Number(el.dataset.step), dflt = Number(el.dataset.default);
  let cur = el.dataset.val === "" ? null : Number(el.dataset.val);
  let next;
  if (cur == null) next = dir > 0 ? dflt : null;
  else { next = cur + dir * step; if (next < 0) next = null; }
  el.dataset.val = next == null ? "" : next;
  const sval = el.querySelector(".sval");
  sval.textContent = next == null ? "off" : next;
  sval.classList.toggle("unset", next == null);
}
function collectStep(key) {
  const v = document.getElementById("st-" + key).dataset.val;
  return v === "" ? null : Number(v);
}
async function saveSpecies(id) {
  const name = document.getElementById("spf-name").value.trim();
  if (!name) return;
  const nightOn = document.getElementById("spf-night-on")?.checked;
  const nightVal = k => (nightOn ? collectStep(k) : null);
  const body = {
    name,
    warm_temp_min: collectStep("warm_temp_min"), warm_temp_max: collectStep("warm_temp_max"),
    cool_temp_min: collectStep("cool_temp_min"), cool_temp_max: collectStep("cool_temp_max"),
    humidity_min: collectStep("humidity_min"), humidity_max: collectStep("humidity_max"),
    night_warm_temp_min: nightVal("night_warm_temp_min"), night_warm_temp_max: nightVal("night_warm_temp_max"),
    night_cool_temp_min: nightVal("night_cool_temp_min"), night_cool_temp_max: nightVal("night_cool_temp_max"),
    night_humidity_min: nightVal("night_humidity_min"), night_humidity_max: nightVal("night_humidity_max"),
  };
  if (id) await api("PUT", `/api/species/${id}`, body);
  else await api("POST", "/api/species", body);
  closeEditor(); await loadManageData();
}
async function deleteSpecies(id) {
  if (!confirm("Delete this species? Enclosures using it will lose their ranges.")) return;
  await api("DELETE", `/api/species/${id}`);
  closeEditor(); await loadManageData();
}

// ── Thermostats pane (optional Herpstat SpyderWeb units) ─────
// Add a unit by LAN IP; the dashboard then shows a compact live strip. The unit
// must have its web status page enabled so http://<ip>/RAWSTATUS responds.
function renderThermostatsPane() {
  const u = "°" + _tempUnit;
  const rows = _thermostats_cfg.map(t => {
    const st = t.status || {};
    const reach = st.reachable;
    const dotCls = t.enabled === false ? "off" : reach ? "ok" : reach === false ? "bad" : "";
    let sub;
    if (t.enabled === false) sub = "Disabled";
    else if (reach) sub = (st.outputs || []).map(o =>
      `${esc(o.name)} ${o.temp ?? "—"}${u}→${o.setpoint ?? "—"}${u}`).join(" · ") || "No outputs";
    else if (reach === false) sub = "Offline — check the IP and that the status page is on";
    else sub = "Connecting…";
    return `
      <div class="row"><div class="row-top">
        <div class="row-info">
          <div class="row-name"><span class="tdot ${dotCls}"></span>${esc(st.name || t.name || t.ip)}</div>
          <div class="row-sub">${sub}</div>
          <div class="row-mac">${esc(t.ip)}</div>
        </div>
        <button class="btn sm" onclick="editThermostat('${esc(t.ip)}')">Edit</button>
      </div></div>`;
  }).join("");
  document.getElementById("pane-thermostats").innerHTML = `
    <div class="pane-toolbar"><h2>Herpstat thermostats</h2>
      <button class="btn primary" onclick="editThermostat(null)">+ Add</button></div>
    <div class="scan-hint">Monitor Herpstat SpyderWeb thermostats on your network. On each unit, enable its
      <b>web status page</b> so <code>http://&lt;ip&gt;/RAWSTATUS</code> responds, then add its IP here.
      The dashboard strip appears once a unit is added.</div>
    ${rows || `<div class="muted-note">No thermostats yet. This feature is optional — add one to show the live strip.</div>`}`;
}

function editThermostat(ip) {
  const t = ip ? _thermostats_cfg.find(x => x.ip === ip) : null;
  openEditor(`
    <div class="sheet-head"><h2>${t ? "Edit" : "Add"} thermostat</h2>
      <button class="close-btn" onclick="closeEditor()">✕</button></div>
    <div class="field"><label>IP address</label>
      <input type="text" id="tf-ip" value="${esc(t?.ip || "")}" placeholder="e.g. 192.168.1.50"
             inputmode="decimal" autocomplete="off"></div>
    <div class="field"><label>Display name (optional — defaults to the unit's own name)</label>
      <input type="text" id="tf-name" value="${esc(t?.name || "")}" placeholder="e.g. Rack 1 Herpstat"></div>
    <label class="night-toggle">
      <input type="checkbox" id="tf-enabled" ${t?.enabled === false ? "" : "checked"}>
      <span>Enabled (poll this unit)</span>
    </label>
    <div class="field"><button class="btn ghost sm" onclick="testThermostat()">⚡ Test connection</button>
      <div id="tf-test" class="test-result"></div></div>
    <div class="form-actions">
      ${t ? `<button class="btn danger" onclick="deleteThermostat('${esc(t.ip)}')">Delete</button>` : ""}
      <button class="btn primary" onclick="saveThermostat(${t ? `'${esc(t.ip)}'` : "null"})">Save</button>
    </div>`);
}

async function testThermostat() {
  const ip = document.getElementById("tf-ip").value.trim();
  const out = document.getElementById("tf-test");
  if (!ip) { out.className = "test-result"; out.textContent = ""; return; }
  out.className = "test-result"; out.textContent = "Testing…";
  try {
    const r = await api("POST", "/api/thermostats/test", { ip });
    if (r.ok) {
      out.className = "test-result ok";
      out.innerHTML = `✓ Connected: <b>${esc(r.name)}</b> · ${r.outputs.length} output` +
        `${r.outputs.length !== 1 ? "s" : ""}${r.outputs.length ? " (" + r.outputs.map(esc).join(", ") + ")" : ""}`;
    } else {
      out.className = "test-result bad";
      out.textContent = "✗ " + r.error;
    }
  } catch (e) {
    out.className = "test-result bad";
    out.textContent = "✗ Test failed — is the server reachable?";
  }
}

async function saveThermostat(ip) {
  const newIp = document.getElementById("tf-ip").value.trim();
  if (!newIp) return;
  const name = document.getElementById("tf-name").value.trim() || null;
  const enabled = document.getElementById("tf-enabled").checked;
  const body = { ip: newIp, name, enabled };
  try {
    if (ip) await api("PUT", `/api/thermostats/${encodeURIComponent(ip)}`, body);
    else await api("POST", "/api/thermostats", body);
  } catch (e) {
    showToast(ip ? "Save failed" : "That IP is already added");
    return;
  }
  closeEditor(); await loadManageData();
}

async function deleteThermostat(ip) {
  if (!confirm("Remove this thermostat from the dashboard?")) return;
  await api("DELETE", `/api/thermostats/${encodeURIComponent(ip)}`);
  closeEditor(); await loadManageData();
}

// ── Settings pane ────────────────────────────────────────────
function renderSettingsPane() {
  const s = _settings;
  const unit = s.temp_unit || "F";
  document.getElementById("pane-settings").innerHTML = `
    <div class="pane-toolbar"><h2>Settings</h2></div>
    <div class="field"><label>Temperature unit</label>
      <div class="toggle-row">
        <button class="btn ${unit === "F" ? "on" : ""}" onclick="setUnit('F')">°F</button>
        <button class="btn ${unit === "C" ? "on" : ""}" onclick="setUnit('C')">°C</button>
      </div></div>
    <div class="field"><label>Mark a sensor "stale" after no signal for</label>
      ${stepperPlain("stale_after_minutes", s.stale_after_minutes ?? 10, 1, "min")}</div>
    <div class="field"><label>Low-battery warning below</label>
      ${stepperPlain("low_battery_pct", s.low_battery_pct ?? 20, 5, "%")}</div>
    <div class="field"><label>Daytime hours — heat on (☀️ day ranges apply; outside = 🌙 night)</label>
      <div class="daywin">
        <div class="dw-cell"><span>From</span>${hourStepper("day_start_hour", s.day_start_hour ?? 8)}</div>
        <div class="dw-cell"><span>To</span>${hourStepper("day_end_hour", s.day_end_hour ?? 20)}</div>
      </div></div>
    <div class="field"><label>📱 Phone alerts — get pinged when an enclosure needs attention</label>
      <div id="alerts-setting"></div></div>
    <div class="field"><label>🔄 Updates</label>
      <div id="update-setting"></div></div>
    <div class="field"><label>💾 Backup — your enclosures, ranges and settings in one file</label>
      <div class="toggle-row">
        <a class="btn" href="/api/config/export" download>⬇ Download backup</a>
        <button class="btn" onclick="document.getElementById('import-file').click()">⬆ Restore from backup</button>
      </div>
      <input type="file" id="import-file" accept="application/json,.json" style="display:none"
             onchange="importSettings(this)"></div>`;
  refreshAlertsUI();
  refreshUpdateUI();
}

// ── Settings restore (import a backup file) ──────────────────
async function importSettings(input) {
  const f = input.files && input.files[0];
  input.value = "";
  if (!f) return;
  let data;
  try { data = JSON.parse(await f.text()); }
  catch (e) { showToast("That file isn't a Bask backup"); return; }
  if (!confirm("Replace ALL current settings with this backup? Your current settings are saved as a restore point first.")) return;
  try {
    const r = await api("POST", "/api/config/import", data);
    showToast(`Restored ${r.enclosures} enclosures, ${r.sensors} sensors`);
    await loadManageData(); refreshDashboard();
  } catch (e) { showToast("Restore failed — not a valid Bask backup"); }
}

// ── In-app updates ───────────────────────────────────────────
let _upd = null;
async function refreshUpdateUI(checking) {
  const el = document.getElementById("update-setting");
  if (!el) return;
  try { _upd = await api("GET", "/api/update/status" + (checking ? "?refresh=1" : "")); }
  catch (e) { el.innerHTML = ""; return; }
  if (!_upd.supported) {
    el.innerHTML = `<div class="muted-note" style="text-align:left;padding:4px 0">Bask ${esc(_upd.version || "")} — in-app updates aren't available for this install.</div>`;
    return;
  }
  const ver = `<span class="upd-ver">Bask <b>${esc(_upd.version)}</b></span>`;
  if (_upd.state === "failed") {
    el.innerHTML = `${ver}<div class="test-result bad">✗ Update failed: ${esc(_upd.error || "unknown error")}</div>
      <button class="btn sm" onclick="refreshUpdateUI(true)">Check again</button>`;
  } else if (_upd.checked && _upd.available) {
    el.innerHTML = `${ver}<div class="test-result ok">✦ ${esc(_upd.latest)} is available</div>
      <button class="btn primary" onclick="startUpdate()">Update now</button>
      <div class="muted-note" style="text-align:left;padding:6px 0 0">Takes about a minute. Your settings are never affected.</div>`;
  } else if (_upd.checked) {
    el.innerHTML = `${ver}<div class="test-result ok">✓ You're up to date</div>`;
  } else if (_upd.check_error) {
    el.innerHTML = `${ver}<div class="test-result bad">✗ Couldn't check (${esc(_upd.check_error)})</div>
      <button class="btn sm" onclick="refreshUpdateUI(true)">Try again</button>`;
  } else {
    el.innerHTML = `${ver}<button class="btn" onclick="updChecking()">Check for updates</button>`;
  }
}
function updChecking() {
  const el = document.getElementById("update-setting");
  if (el) el.innerHTML = `<span class="upd-ver">Checking…</span>`;
  refreshUpdateUI(true);
}
async function startUpdate() {
  const el = document.getElementById("update-setting");
  const oldVer = _upd && _upd.version;
  try { await api("POST", "/api/update", { confirm: true }); }
  catch (e) { showToast("Couldn't start the update"); return; }
  el.innerHTML = `<span class="upd-ver">Updating… the page will refresh itself. 🦎</span>`;
  // Poll until the server comes back on a new version, then hard-reload.
  const poll = setInterval(async () => {
    try {
      const s = await api("GET", "/api/update/status");
      if (s.state === "failed") { clearInterval(poll); refreshUpdateUI(); return; }
      if (s.version && s.version !== oldVer) { clearInterval(poll); location.reload(); }
    } catch (e) { /* server restarting — keep polling */ }
  }, 3000);
}

// ── Opt-in phone alerts (via the ntfy app) ───────────────────
let _ntfy = null;
async function refreshAlertsUI() {
  const el = document.getElementById("alerts-setting");
  if (!el) return;
  try { _ntfy = await api("GET", "/api/ntfy"); } catch (e) { el.innerHTML = ""; return; }
  if (!_ntfy.enabled) {
    el.innerHTML = `<button class="btn primary" onclick="enableAlerts()">Set up phone alerts</button>
      <div class="muted-note" style="padding:6px 0 0;text-align:left">Free, ~1 minute. Uses the ntfy notification app.</div>`;
    return;
  }
  const qr = _ntfy.qr
    ? `<img class="ntfy-qr" src="/api/ntfy/qr?t=${Date.now()}" alt="Subscribe QR code" width="150" height="150">` : "";
  el.innerHTML = `
    <div class="ntfy-setup">
      <ol class="ntfy-steps">
        <li>Install the free <b>ntfy</b> app (App&nbsp;Store or Google&nbsp;Play).</li>
        <li>Open it, tap <b>+</b> to add a subscription, then scan this code — or type the topic:</li>
      </ol>
      <div class="ntfy-row">
        ${qr}
        <div class="ntfy-topic">
          <div class="ntfy-topic-label">Your private topic</div>
          <code>${esc(_ntfy.topic)}</code>
          <button class="btn ghost sm" onclick="copyTopic()">Copy</button>
        </div>
      </div>
      <div class="toggle-row" style="margin-top:14px">
        <button class="btn on" onclick="testAlert()">Send test</button>
        <button class="btn" onclick="disableAlerts()">Turn off</button>
      </div>
      <div class="muted-note" style="padding:8px 0 0;text-align:left">Once subscribed, tap <b>Send test</b> — it should pop up on your phone.</div>
    </div>`;
}
async function enableAlerts() {
  try { await api("POST", "/api/ntfy", { enabled: true }); refreshAlertsUI(); }
  catch (e) { showToast("Couldn't turn on alerts"); }
}
async function disableAlerts() {
  try { await api("POST", "/api/ntfy", { enabled: false }); } catch (e) {}
  showToast("Alerts turned off"); refreshAlertsUI();
}
async function testAlert() {
  try { await api("POST", "/api/ntfy/test"); showToast("Test sent — check your phone"); }
  catch (e) { showToast("Couldn't reach ntfy — check the Pi's internet"); }
}
function copyTopic() {
  navigator.clipboard?.writeText(_ntfy?.topic || "").then(() => showToast("Topic copied"), () => {});
}
function stepperPlain(key, val, step, unit) {
  return `<div class="stepper" id="set-${key}" data-val="${val}" data-step="${step}" data-unit="${unit}">
    <button class="step-btn" onclick="stepSetting('${key}',-1)">−</button>
    <div class="sval">${val} ${unit}</div>
    <button class="step-btn" onclick="stepSetting('${key}',1)">+</button></div>`;
}
async function stepSetting(key, dir) {
  const el = document.getElementById("set-" + key);
  const step = Number(el.dataset.step);
  let v = Number(el.dataset.val) + dir * step;
  if (v < step) v = step;
  el.dataset.val = v;
  el.querySelector(".sval").textContent = `${v} ${el.dataset.unit}`;
  await api("PUT", "/api/settings", { [key]: v });
  _settings[key] = v;
}
function fmtHourLong(h) {
  const ap = h < 12 ? "AM" : "PM";
  return (h % 12 || 12) + " " + ap;
}
function hourStepper(key, val) {
  return `<div class="stepper" id="set-${key}" data-val="${val}">
    <button class="step-btn" onclick="stepHour('${key}',-1)">−</button>
    <div class="sval">${fmtHourLong(val)}</div>
    <button class="step-btn" onclick="stepHour('${key}',1)">+</button></div>`;
}
async function stepHour(key, dir) {
  const el = document.getElementById("set-" + key);
  const v = (Number(el.dataset.val) + dir + 24) % 24;
  el.dataset.val = v;
  el.querySelector(".sval").textContent = fmtHourLong(v);
  await api("PUT", "/api/settings", { [key]: v });
  _settings[key] = v;
}
async function setUnit(u) {
  await api("PUT", "/api/settings", { temp_unit: u });
  _settings.temp_unit = u; _tempUnit = u;
  renderSettingsPane(); renderSpeciesPane();
}

// ── editor sheet plumbing ────────────────────────────────────
function openEditor(html) {
  document.getElementById("editor-sheet").innerHTML = html;
  document.getElementById("editor").classList.add("open");
}
function closeEditor() { document.getElementById("editor").classList.remove("open"); }

// ── init ─────────────────────────────────────────────────────
async function loadSpecies() {
  try { _species = (await api("GET", "/api/species")).species; } catch (e) {}
}
if ("serviceWorker" in navigator) {
  navigator.serviceWorker.register("/sw.js").catch(() => {});   // installable app + push
}
tickClock();
setInterval(tickClock, 10000);
loadSpecies();                  // so the detail sheet can show acceptable ranges
setInterval(loadSpecies, 60000);
refreshDashboard();
setInterval(refreshDashboard, REFRESH_MS);
