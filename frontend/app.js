// ============================================================
//  Bask — dashboard front-end (vanilla JS, no build step)
// ============================================================
const REFRESH_MS = 15000;

let _dash = null;
let _species = [];
let _sensors = [];
let _lastData = null;
let _enclosures = [];
let _tempUnit = "F";
let _configRevision = null;
let _conflictRecovery = null;
let _settingsWriteTail = Promise.resolve();
let _settingsWriteEpoch = 0;
const NIGHT_FIELDS = ["night_warm_temp_min", "night_warm_temp_max", "night_cool_temp_min",
                      "night_cool_temp_max", "night_humidity_min", "night_humidity_max"];

// ── helpers ──────────────────────────────────────────────────
const CONFIG_REVISION_HEADER = "X-Bask-Revision";
const CONFIG_REVISION_APPLIED_HEADER = "X-Bask-Revision-Applied";

class ConfigConflictError extends Error {
  constructor(message) { super(message); this.name = "ConfigConflictError"; this.conflict = true; }
}

function configRevisionFrom(response) {
  const raw = response.headers.get(CONFIG_REVISION_HEADER);
  if (raw == null) return null;
  const revision = Number(raw);
  return Number.isInteger(revision) && revision >= 0 ? revision : null;
}

async function ensureConfigRevision() {
  if (_configRevision != null) return;
  const response = await fetch("/api/config/revision", { cache: "no-store" });
  if (!response.ok) throw new Error("Could not read the current Bask setup version");
  const payload = await response.json();
  if (Number.isInteger(payload.revision) && payload.revision >= 0) {
    _configRevision = payload.revision;
  } else {
    throw new Error("Bask returned an invalid setup version");
  }
}

async function recoverConfigConflict() {
  if (_conflictRecovery) return _conflictRecovery;
  _conflictRecovery = (async () => {
    try {
      // Cancel any settings clicks that were queued against the stale form.
      _settingsWriteEpoch += 1;
      closeEditor();
      await refreshKeeperState();
      const manage = document.getElementById("manage");
      if (manage?.classList.contains("open")) await loadManageData();
      else await refreshDashboard();
      showToast("Bask changed on another device. Latest setup loaded — try again.");
    } finally {
      _conflictRecovery = null;
    }
  })();
  return _conflictRecovery;
}

async function api(method, url, body, _retried) {
  method = method.toUpperCase();
  if (!["GET", "HEAD", "OPTIONS"].includes(method)) await ensureConfigRevision();
  const opt = { method, headers: { "Content-Type": "application/json" } };
  if (!["GET", "HEAD", "OPTIONS"].includes(method) && _configRevision != null) {
    opt.headers[CONFIG_REVISION_HEADER] = String(_configRevision);
  }
  const sentRevision = _configRevision;
  if (body !== undefined) opt.body = JSON.stringify(body);
  const res = await fetch(url, opt);
  if (!res.ok) {
    // A session can die under you — the Head Keeper key is rotated on another
    // device, which invalidates every cookie. Rather than a dead-end toast,
    // ask for the key and replay what you were doing. Once only, so a genuinely
    // wrong key surfaces its error instead of looping.
    if (res.status === 401 && !_retried && !url.startsWith("/api/keeper/")) {
      const unlocked = await promptForKeeperKey();
      if (unlocked) return api(method, url, body, true);
    }
    let message = `${method} ${url} -> ${res.status}`;
    try { const payload = await res.json(); message = payload.error || payload.detail || message; } catch (_) {}
    if (res.status === 409) {
      const current = configRevisionFrom(res);
      if (current != null) _configRevision = current;
      await recoverConfigConflict();
      throw new ConfigConflictError(message);
    }
    throw new Error(message);
  }
  if (res.headers.get(CONFIG_REVISION_APPLIED_HEADER) === "true") {
    const applied = configRevisionFrom(res);
    // This header is produced only by the strict config-write dependency and
    // names the transaction just completed, not an arbitrary later GET.
    if (applied == null || sentRevision == null || applied !== sentRevision + 1) {
      _configRevision = null;
      throw new Error("Bask could not confirm the saved setup version");
    }
    _configRevision = applied;
  }
  return res.status === 204 ? null : res.json();
}

/** Show the unlock sheet and resolve true once the key is accepted. */
function promptForKeeperKey() {
  return new Promise((resolve) => {
    let settled = false;
    const done = (ok) => { if (!settled) { settled = true; resolve(ok); } };
    openKeeper(async () => done(true));
    const sheet = document.getElementById("keeper");
    const observer = new MutationObserver(() => {
      if (!sheet.classList.contains("open")) { observer.disconnect(); done(false); }
    });
    observer.observe(sheet, { attributes: true, attributeFilter: ["class"] });
  });
}
function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, c =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
// Values that are supposed to be numbers. Integrations are third-party services
// and a firmware quirk or a hostile response can put a string where a reading
// belongs — and readings are interpolated into markup without esc(), on the
// grounds that "it is a number". This makes that true instead of assumed.
function num(value, fallback = "—") {
  const parsed = typeof value === "number" ? value : Number(value);
  return Number.isFinite(parsed) ? String(parsed) : fallback;
}
// An identifier going into an inline handler sits inside a JavaScript string
// inside an HTML attribute. esc() is not enough there: the browser decodes
// entities before the handler runs, so &#39; becomes a quote again and closes
// the string early. Identifiers are machine-generated, so anything outside a
// safe alphabet means something is wrong and is dropped rather than encoded.
function idAttr(value) {
  return String(value ?? "").replace(/[^A-Za-z0-9:._-]/g, "");
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

// ── dialog accessibility ───────────────────────────
// Bask's overlays are deliberately plain DOM rather than a framework widget.
// Keep their modal semantics in one place: only the top dialog is interactive,
// focus cannot escape it with Tab, Escape closes it, and closing restores the
// control that opened it. `inert` also prevents pointer/AT interaction with the
// dashboard behind the visible sheet.
const _dialogStack = [];
const _dialogOpeners = new WeakMap();
const _dialogBackground = new Map();

function dialogFocusable(dialog) {
  return [...dialog.querySelectorAll(
    'a[href],button:not([disabled]),input:not([disabled]),select:not([disabled]),' +
    'textarea:not([disabled]),[tabindex]:not([tabindex="-1"])'
  )].filter(node => !node.closest("[inert]") &&
    node.getAttribute("aria-hidden") !== "true" && node.getClientRects().length > 0);
}

function restoreDialogBackground() {
  for (const [node, previous] of _dialogBackground) {
    node.inert = previous.inert;
    if (previous.ariaHidden == null) node.removeAttribute("aria-hidden");
    else node.setAttribute("aria-hidden", previous.ariaHidden);
  }
  _dialogBackground.clear();
}

function syncDialogBackground() {
  restoreDialogBackground();
  const top = _dialogStack.at(-1);
  if (!top) return;
  // A static, closed overlay may have been part of the previous dialog's
  // saved background. Restore happens first, so explicitly re-open the new top
  // after that restoration rather than leaving a nested editor inert.
  top.inert = false;
  top.setAttribute("aria-hidden", "false");
  for (const node of document.body.children) {
    if (!(node instanceof HTMLElement) || node === top || node.id === "toast" ||
        ["SCRIPT", "STYLE", "LINK"].includes(node.tagName)) continue;
    _dialogBackground.set(node, {
      inert: node.inert,
      ariaHidden: node.getAttribute("aria-hidden"),
    });
    node.inert = true;
    node.setAttribute("aria-hidden", "true");
  }
}

function openDialog(id, focusSelector) {
  const dialog = document.getElementById(id);
  if (!dialog) return;
  const active = document.activeElement;
  if (active instanceof HTMLElement && active !== dialog && !_dialogStack.includes(dialog)) {
    _dialogOpeners.set(dialog, active);
  }
  const oldIndex = _dialogStack.indexOf(dialog);
  if (oldIndex >= 0) _dialogStack.splice(oldIndex, 1);
  _dialogStack.push(dialog);
  dialog.inert = false;
  dialog.setAttribute("aria-hidden", "false");
  dialog.classList.add("open");
  syncDialogBackground();
  const requested = focusSelector ? dialog.querySelector(focusSelector) : null;
  const target = requested || dialogFocusable(dialog)[0] || dialog;
  if (target instanceof HTMLElement) target.focus({ preventScroll: true });
}

function closeDialog(id) {
  const dialog = document.getElementById(id);
  if (!dialog) return;
  dialog.classList.remove("open");
  dialog.setAttribute("aria-hidden", "true");
  dialog.inert = true;
  const index = _dialogStack.indexOf(dialog);
  if (index >= 0) _dialogStack.splice(index, 1);
  syncDialogBackground();
  const opener = _dialogOpeners.get(dialog);
  requestAnimationFrame(() => {
    if (opener instanceof HTMLElement && opener.isConnected && !opener.closest("[inert]")) {
      opener.focus({ preventScroll: true });
    } else {
      _dialogStack.at(-1)?.focus({ preventScroll: true });
    }
  });
}

function dialogKeyAction(key, shiftKey, currentIndex, focusableCount) {
  if (key === "Escape") return "close";
  if (key !== "Tab") return "none";
  if (focusableCount === 0) return "dialog";
  if (shiftKey && currentIndex <= 0) return "last";
  if (!shiftKey && (currentIndex < 0 || currentIndex === focusableCount - 1)) return "first";
  return "none";
}

document.addEventListener("keydown", event => {
  const top = _dialogStack.at(-1);
  if (!top) return;
  const focusable = dialogFocusable(top);
  const action = dialogKeyAction(
    event.key, event.shiftKey, focusable.indexOf(document.activeElement), focusable.length,
  );
  if (action === "close") {
    event.preventDefault();
    ({ detail: closeDetail, manage: closeManage, keeper: closeKeeper,
       editor: closeEditor, pair: closePair })[top.id]?.();
    return;
  }
  if (action === "dialog") {
    event.preventDefault();
    top.focus();
    return;
  }
  if (action === "last") {
    event.preventDefault();
    focusable.at(-1).focus();
  } else if (action === "first") {
    event.preventDefault();
    focusable[0].focus();
  }
});

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
    // Kept so the banner can re-render itself when expanded without waiting
    // for the next poll.
    _lastData = data;
    renderSummary(data.counts);
    renderStatusBanner(data);
    renderPeriod(data);
    renderRoomClimate(data.room_climate);
    renderHumidifier(data.humidifier);
    renderOutdoor(data);
    renderThermostats(data);
    renderGrid(data);
    const t = new Date(data.updated_at * 1000);
    document.getElementById("updated").textContent =
      "Updated " + t.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  } catch (e) {
    document.getElementById("updated").textContent = "⚠ connection lost — retrying";
  }
}

// Small, read-only Cielo Breez card in the top-right. It stays completely
// hidden until configured and never affects enclosure status calculations.
function renderRoomClimate(climate) {
  const el = document.getElementById("room-climate");
  if (!el) return;
  if (!climate?.configured) { el.style.display = "none"; return; }
  el.style.display = "grid";
  const unit = climate.temp_unit === "C" ? "°C" : "°F";
  const temp = climate.temperature == null ? "—" : `${climate.temperature}${unit}`;
  const humidity = climate.humidity == null ? "—" : `${climate.humidity}%`;
  const unavailable = climate.error || climate.stale || climate.online === false;
  el.className = "room-climate" + (unavailable ? " unavailable" : "");
  const state = climate.error ? climate.error :
    climate.stale ? "Status is stale" :
    climate.online === false ? "Controller offline" :
    climate.power ? `${climate.mode || "on"}${climate.target == null ? "" : ` → ${climate.target}${unit}`}` : "off";
  el.innerHTML = `
    <span class="rc-label">${esc(climate.name || "Animal Room")}</span>
    <span class="rc-reading">${temp}<small>${humidity}</small></span>
    <span class="rc-state">${esc(state)}</span>`;
  el.title = "Cielo Breez room climate — tap to manage";
}

// Outdoor reference reading. Not a habitat and never judged against a range —
// it is the number that explains what the room is fighting. The difference is
// shown alongside it because that gap, not the absolute, is what predicts
// whether the mini-split can hold its setpoint tonight.
function renderOutdoor(data) {
  const el = document.getElementById("room-outdoor");
  if (!el) return;
  const reading = (data.outdoor || [])[0];
  if (!reading || reading.temp == null) { el.style.display = "none"; return; }
  el.style.display = "grid";
  el.className = "room-climate outdoor" + (reading.stale ? " unavailable" : "");

  const u = "°" + _tempUnit;
  const humidity = reading.humidity == null ? "" : `${reading.humidity}%`;
  // Compare against the mini-split's own thermometer when there is one, since
  // that is the sensor the unit actually acts on.
  const inside = data.room_climate?.configured && data.room_climate.temperature != null
    ? data.room_climate.temperature : null;
  let gap = "";
  if (inside != null && !reading.stale) {
    const delta = Math.round((reading.temp - inside) * 10) / 10;
    gap = delta === 0 ? "same as inside"
      : `${Math.abs(delta)}${u} ${delta > 0 ? "warmer" : "colder"} out`;
  }
  el.innerHTML = `
    <span class="rc-label">${esc(reading.name || "Outside")}</span>
    <span class="rc-reading">${reading.temp}${u}<small>${humidity}</small></span>
    <span class="rc-state">${esc(reading.stale ? fmtAge(reading.age_seconds) : gap)}</span>`;
  el.title = "Outdoor reference sensor";
}

// Read-only Levoit/VeSync humidifier card. Low water gets an explicit warning;
// the device never affects enclosure range calculations.
function renderHumidifier(device) {
  const el = document.getElementById("room-humidifier");
  if (!el) return;
  if (!device?.configured) { el.style.display = "none"; return; }
  el.style.display = "grid";
  const unavailable = device.error || device.stale || device.online === false;
  const lowWater = device.water_lacks === true || String(device.water_lacks).toLowerCase() === "on";
  el.className = "room-climate humidifier" + (unavailable || lowWater ? " unavailable" : "");
  const humidity = device.humidity == null ? "—" : `${device.humidity}%`;
  const state = device.error ? device.error :
    device.stale ? "Status is stale" :
    device.online === false ? "Humidifier offline" :
    lowWater ? "Refill water" :
    device.power ? `${device.mode || "on"}${device.target_humidity == null ? "" : ` → ${device.target_humidity}%`}` : "off";
  const level = device.mist_level == null ? "" : `Mist ${device.mist_level}`;
  el.innerHTML = `
    <span class="rc-label">${esc(device.name || "Room humidifier")}</span>
    <span class="rc-reading">${humidity}<small>${esc(level)}</small></span>
    <span class="rc-state">${esc(state)}</span>`;
  el.title = "Levoit room humidifier — tap to manage";
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

  // One chip per enclosure rather than a run-on sentence.
  //
  // The old banner concatenated every problem into a single red paragraph, so
  // six marginal humidity readings looked identical to an animal in real
  // trouble, and the only way to read it was to parse a wall of text broken by
  // interpuncts. Chips are scannable, carry their own severity, and each one
  // is a tap to the enclosure it names.
  const anyDanger = problems.some(e => e.status === "danger");
  el.className = "status-banner " + (anyDanger ? "danger" : "warn");

  const rank = { danger: 0, warning: 1, stale: 2 };
  const chips = [...problems]
    .sort((a, b) => (rank[a.status] ?? 3) - (rank[b.status] ?? 3))
    .map(e => {
      let why;
      if (e.status === "stale") {
        why = "no signal";
      } else {
        const issues = [];
        if (e.warm_temp_ok === false) issues.push("warm");
        if (e.cool_temp_ok === false) issues.push("cool");
        if (e.humidity_ok === false) issues.push("humidity");
        why = issues.join(" · ") || "out of range";
      }
      return `<button type="button" class="sb-chip ${e.status}"
                onclick="openDetail('${idAttr(e.id)}')"
                aria-label="${esc(e.name)}: ${esc(why)}">
        <span class="sbc-name">${esc(e.name)}</span>
        <span class="sbc-why">${esc(why)}</span>
      </button>`;
    });

  // Counted separately so "two need attention" is not buried among six that
  // are merely drifting.
  const danger = problems.filter(e => e.status === "danger").length;
  const rest = problems.length - danger;
  const summary = danger && rest ? `${danger} out of range · ${rest} drifting`
    : danger ? `${danger} out of range`
    : `${rest} drifting`;

  // Chips are more scannable than a paragraph but they cost vertical space,
  // and eleven of them push the enclosures off a phone screen entirely. Show
  // the worst few — they are sorted worst-first — and let the rest be asked
  // for. The count in the headline never hides anything.
  const CAP = 6;
  const hidden = Math.max(0, chips.length - CAP);
  const shown = _bannerExpanded ? chips : chips.slice(0, CAP);
  const more = hidden && !_bannerExpanded
    ? `<button type="button" class="sb-chip more" onclick="expandBanner()"
         aria-label="Show ${hidden} more">
         <span class="sbc-name">+${hidden}</span><span class="sbc-why">more</span></button>`
    : "";

  el.innerHTML = `
    <div class="sb-head">
      <span class="sb-icon">${anyDanger ? "⚠" : "•"}</span>
      <span class="sb-text">Check ${problems.length}</span>
      <span class="sb-sub">${summary}</span>
      ${battNote}
    </div>
    <div class="sb-chips">${shown.join("")}${more}</div>`;
}

let _bannerExpanded = false;

function expandBanner() {
  _bannerExpanded = true;
  if (_lastData) renderStatusBanner(_lastData);
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

// Herpstat outputs, laid out as a grid rather than a single scrolling row.
//
// A twelve-output rack in one horizontal strip means the eleventh and twelfth
// are never looked at, which defeats the purpose of showing them at all. Each
// output gets a tile with the three numbers that matter — where the probe is,
// where it is aiming, and how hard it is working — and the power level is drawn
// as a bar because "is this element straining" is a shape, not a figure to read.
function renderThermostats(data) {
  const el = document.getElementById("thermostat-strip");
  if (!el) return;
  const units = data.thermostats || [];
  if (!units.length) { el.style.display = "none"; return; }
  el.style.display = "block";
  const u = "°" + _tempUnit;
  const tiles = [];
  let heating = 0;
  let total = 0;

  for (const unit of units) {
    if (!unit.reachable) {
      tiles.push(`<div class="tstat offline">
        <span class="ts-name">${esc(unit.name)}</span>
        <span class="ts-temp">—</span>
        <span class="ts-sub">unreachable</span></div>`);
      continue;
    }
    for (const o of unit.outputs) {
      total += 1;
      if (o.heating) heating += 1;
      const pct = Math.max(0, Math.min(100, Number(o.output_pct) || 0));
      const state = o.alarm ? "alarm" : o.heating ? "heating" : "idle";
      tiles.push(`<div class="tstat ${state}">
        <span class="ts-name">${esc(o.name)}</span>
        <span class="ts-temp">${num(o.temp)}<small>${u}</small></span>
        <span class="ts-sub">aiming ${num(o.setpoint)}${u}${o.error ? " · " + esc(o.error) : ""}</span>
        <span class="ts-bar" role="img" aria-label="output ${pct} percent">
          <span class="ts-fill" style="width:${pct}%"></span></span>
        <span class="ts-pct">${pct}%</span>
      </div>`);
    }
  }

  const summary = total
    ? `${heating} of ${total} heating`
    : "no outputs";
  el.innerHTML = `
    <button type="button" class="tstat-head" onclick="toggleThermostats()"
            aria-expanded="true" aria-controls="tstat-grid">
      <span class="tstat-label">Thermostats</span>
      <span class="tstat-summary">${summary}</span>
      <span class="tstat-caret" aria-hidden="true">▾</span>
    </button>
    <div class="tstat-grid" id="tstat-grid">${tiles.join("")}</div>`;
  if (_thermostatsCollapsed) el.classList.add("collapsed");
}

// Collapsible because twelve tiles is a lot of screen on a phone when the
// answer is "they are all fine". The preference sticks so the choice is made
// once rather than on every glance.
// Read defensively: this runs at module scope, and localStorage throws rather
// than returning null in a restricted context (private browsing, an embedded
// webview with storage disabled). An exception here would take the whole
// dashboard down to remember whether a panel was folded.
let _thermostatsCollapsed = (() => {
  try { return localStorage.getItem("bask.tstats.collapsed") === "1"; }
  catch (e) { return false; }
})();

function toggleThermostats() {
  const el = document.getElementById("thermostat-strip");
  if (!el) return;
  _thermostatsCollapsed = !_thermostatsCollapsed;
  el.classList.toggle("collapsed", _thermostatsCollapsed);
  const head = el.querySelector(".tstat-head");
  if (head) head.setAttribute("aria-expanded", String(!_thermostatsCollapsed));
  try { localStorage.setItem("bask.tstats.collapsed", _thermostatsCollapsed ? "1" : "0"); } catch (e) {}
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
    return `<span class="metric ${cls}"><span class="metric-label">${esc(label)}</span>
            <span class="metric-none">—</span></span>`;
  }
  return `<span class="metric ${cls} ${bad ? "bad" : ""}">
    <span class="metric-label">${esc(label)}</span>
    <span class="metric-value">${value}<span class="metric-unit">${unit}</span></span>
  </span>`;
}

function encCardHTML(e) {
  const flagging = e.status === "warning" || e.status === "danger";
  const bad = ok => flagging && ok === false;
  const warm = e.warm, cool = e.cool;
  const u = "°" + _tempUnit;

  const body = `
    <span class="enc-body">
      ${metric(warm?.position || "Warm", warm ? warm.temp : null, u, bad(e.warm_temp_ok))}
      ${metric("Humidity", cool ? cool.humidity : null, "%", bad(e.humidity_ok), "mid")}
      ${metric(cool?.position || "Cool", cool ? cool.temp : null, u, bad(e.cool_temp_ok))}
    </span>`;

  const flags = [];
  if (e.low_battery) flags.push(`<span class="flag low-batt">🔋 low</span>`);
  if (e.status === "stale" || e.status === "no_data")
    flags.push(`<span class="flag stale-flag">no signal</span>`);

  return `
    <button type="button" class="enc-card ${e.status}" onclick="openDetail('${idAttr(e.id)}')"
            aria-label="Open ${esc(e.name)} enclosure details">
      <span class="enc-head">
        <span class="enc-title">
          <span class="enc-name">${esc(e.name)}</span>
          ${e.species_name ? `<span class="enc-species">${esc(e.species_name)}</span>` : ""}
        </span>
        <span class="status-badge"><span class="bdot"></span>${STATUS_LABEL[e.status] || e.status}</span>
      </span>
      ${body}
      <span class="enc-foot">
        <span>${fmtAge(e.age_seconds)}</span>
        <span class="foot-flags">${flags.join("")}</span>
      </span>
    </button>`;
}

function soloCardHTML(s) {
  const status = s.temp == null ? "no_data" : s.stale ? "stale" : "ok";
  const u = "°" + _tempUnit;
  return `
    <button type="button" class="enc-card solo ${status}" onclick="openDetailSolo('${idAttr(s.mac)}')"
            aria-label="Open ${esc(s.name)} sensor details">
      <span class="enc-head">
        <span class="enc-title">
          <span class="enc-name">${esc(s.name)}</span>
          ${s.species ? `<span class="enc-species">${esc(s.species)}</span>` : ""}
        </span>
        <span class="status-badge"><span class="bdot"></span>${STATUS_LABEL[status]}</span>
      </span>
      <span class="enc-body">
        ${metric("Temp", s.temp, u, false)}
        ${metric("Humidity", s.humidity, "%", false, "mid")}
      </span>
      <span class="enc-foot"><span>${fmtAge(s.age_seconds)}</span>
        <span class="foot-flags">${s.low_battery ? '<span class="flag low-batt">🔋 low</span>' : ""}</span>
      </span>
    </button>`;
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
        <h2 id="detail-title">${esc(e.name)}</h2>
        <div class="sheet-sub">${esc(e.species_name || "No species set")} · ${STATUS_LABEL[e.status]}${sp ? " · " + (isDay ? "☀️ day" : "🌙 night") + " ranges" : ""}</div>
      </div>
      <button class="close-btn" onclick="closeDetail()" aria-label="Close details">✕</button>
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
      <button class="btn" onclick="closeDetail(); openManage('enclosures'); setTimeout(()=>editEnclosure('${idAttr(e.id)}'),60)">Edit enclosure</button>
    </div>`;
  openDialog("detail");
}
function openDetailSolo(mac) {
  const s = _dash?.ungrouped.find(x => x.mac === mac);
  if (!s) return;
  const u = "°" + _tempUnit;
  document.getElementById("detail-sheet").innerHTML = `
    <div class="sheet-head"><div style="flex:1"><h2 id="detail-title">${esc(s.name)}</h2>
      <div class="sheet-sub">Unassigned sensor</div></div>
      <button class="close-btn" onclick="closeDetail()" aria-label="Close details">✕</button></div>
    <div class="detail-metrics">
      <div class="dm"><div class="dm-label">Temp</div><div class="dm-value">${s.temp == null ? "—" : s.temp + u}</div></div>
      <div class="dm"><div class="dm-label">Humidity</div><div class="dm-value">${s.humidity == null ? "—" : s.humidity + "%"}</div></div>
      <div class="dm"><div class="dm-label">Battery</div><div class="dm-value">${s.battery == null ? "—" : s.battery + "%"}</div></div>
    </div>
    <div class="detail-rows"><div class="drow"><span>MAC</span><span>${esc(s.mac)}</span></div>
      <div class="drow"><span>Last seen</span><span>${fmtAge(s.age_seconds)}</span></div></div>`;
  openDialog("detail");
}
function closeDetail() { closeDialog("detail"); }

// ── manage overlay ───────────────────────────────────────────
// ── Head Keeper lock ────────────────────────────────────────────────────────
// The server enforces this on every setup route regardless of what happens
// here; the UI just avoids presenting controls that would only 401, and offers
// somewhere to type the key. With no key configured this is all inert.

let _keeper = { configured: false, unlocked: true };
let _afterUnlock = null;

async function refreshKeeperState() {
  try {
    const response = await fetch("/api/keeper", { cache: "no-store" });
    _keeper = await response.json();
  } catch {
    _keeper = { configured: false, unlocked: true };
  }
  return _keeper;
}

function openKeeper(afterUnlock) {
  _afterUnlock = afterUnlock || null;
  document.getElementById("keeper-error").textContent = "";
  const field = document.getElementById("keeper-key");
  field.value = "";
  openDialog("keeper", "#keeper-key");
}

function closeKeeper() {
  closeDialog("keeper");
  _afterUnlock = null;
}

async function submitKeeperUnlock(event) {
  event.preventDefault();
  const button = document.getElementById("keeper-submit");
  const error = document.getElementById("keeper-error");
  const key = document.getElementById("keeper-key").value;
  button.disabled = true;
  error.textContent = "";
  try {
    const response = await fetch("/api/keeper/unlock", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ key }),
    });
    if (!response.ok) {
      error.textContent = response.status === 401
        ? "That key doesn't match."
        : "Couldn't check that key.";
      return;
    }
    await refreshKeeperState();
    const next = _afterUnlock;
    closeKeeper();
    if (next) await next();
  } finally {
    button.disabled = false;
  }
}

async function lockKeeper() {
  await fetch("/api/keeper/lock", { method: "POST" });
  await refreshKeeperState();
  closeManage();
}

async function openManage(tab) {
  await refreshKeeperState();
  // Locked: ask for the key first, then carry on into the panel they wanted.
  if (_keeper.configured && !_keeper.unlocked) {
    openKeeper(() => openManage(tab));
    return;
  }
  await loadManageData();
  switchTab(tab || "enclosures");
  document.documentElement.classList.add("modal-open");
  document.body.classList.add("modal-open");
  openDialog("manage");
}
function closeManage() {
  closeDialog("manage");
  document.documentElement.classList.remove("modal-open");
  document.body.classList.remove("modal-open");
  refreshDashboard();
}
function switchTab(name) {
  document.querySelectorAll(".mtab").forEach(b => b.classList.toggle("active", b.dataset.tab === name));
  document.querySelectorAll(".pane").forEach(p => p.classList.toggle("active", p.id === `pane-${name}`));
  if (name === "sensors") startDiscovery();
}

async function loadManageData() {
  const [snapshot, cres, vres] = await Promise.all([
    api("GET", "/api/manage-snapshot"), api("GET", "/api/cielo"), api("GET", "/api/vesync"),
  ]);
  applyManageSnapshot(snapshot);
  _cielo = cres;
  _vesync = vres;
  renderEnclosuresPane();
  renderSensorsPane();
  renderSpeciesPane();
  renderThermostatsPane();
  renderSettingsPane();
}

function applyManageSnapshot(snapshot) {
  if (!Number.isInteger(snapshot.revision) || snapshot.revision < 0) {
    throw new Error("Bask returned an invalid setup snapshot");
  }
  // This token belongs to these exact arrays/forms. Background dashboard and
  // species reads deliberately never advance it while an editor is open.
  _configRevision = snapshot.revision;
  _sensors = snapshot.sensors; _enclosures = snapshot.enclosures; _species = snapshot.species;
  _thermostats_cfg = snapshot.thermostats;
  _settings = snapshot.settings;
}
let _settings = {};
let _thermostats_cfg = [];
let _cielo = {};
let _vesync = {};

// ── Enclosures pane ──────────────────────────────────────────
function renderEnclosuresPane() {
  const sName = Object.fromEntries(_sensors.map(s => [s.mac.toUpperCase(), s.name]));
  const spName = Object.fromEntries(_species.map(s => [s.id, s.name]));
  const list = _enclosures.map((e, i) => `
    <div class="row">
      <div class="row-top">
        <div class="row-reorder">
          <button class="btn icon" ${i === 0 ? "disabled" : ""} onclick="moveEnclosure('${idAttr(e.id)}',-1)">▲</button>
          <button class="btn icon" ${i === _enclosures.length - 1 ? "disabled" : ""} onclick="moveEnclosure('${idAttr(e.id)}',1)">▼</button>
        </div>
        <div class="row-info">
          <div class="row-name">${esc(e.name)}</div>
          <div class="row-sub">${esc(spName[e.species_id] || "No species")}</div>
        </div>
        <button class="btn sm" onclick="editEnclosure('${idAttr(e.id)}')">Edit</button>
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
      <button class="close-btn" onclick="closeEditor()" aria-label="Close editor">✕</button></div>
    <div class="field"><label>Name</label>
      <input type="text" id="ef-name" value="${esc(enc?.name || "")}" placeholder="e.g. Achilles"></div>
    <div class="field"><label>Species (sets acceptable ranges)</label>
      <select id="ef-species">${spOpts(enc?.species_id)}</select></div>
    <div class="field"><label>Sensors &amp; positions</label>
      <div id="ef-slots">${slots.map(slotHTML).join("")}</div>
      <button class="btn ghost sm" onclick="addSlot()">+ Add sensor slot</button></div>
    <div class="form-actions">
      ${enc ? `<button class="btn danger" onclick="deleteEnclosure('${idAttr(enc.id)}')">Delete</button>` : ""}
      <button class="btn primary" onclick="saveEnclosure(${enc ? `'${idAttr(enc.id)}'` : "null"})">Save</button>
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
      <button class="btn sm" onclick="editSensor('${idAttr(s.mac)}')">Edit</button>
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
    <div class="sheet-head"><h2>Edit sensor</h2><button class="close-btn" onclick="closeEditor()" aria-label="Close editor">✕</button></div>
    <div class="field"><label>Name</label><input type="text" id="sf-name" value="${esc(s.name)}"></div>
    <div class="field"><label>Species (optional label)</label><input type="text" id="sf-species" value="${esc(s.species || "")}"></div>
    <div class="row-mac" style="margin-bottom:14px">${esc(s.mac)}</div>
    <div class="form-actions">
      <button class="btn danger" onclick="deleteSensor('${idAttr(s.mac)}')">Delete</button>
      <button class="btn primary" onclick="saveSensor('${idAttr(s.mac)}')">Save</button></div>`);
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
  openDialog("pair");
  renderPairTargets();
  pairPoll();
  if (_pairTimer) clearInterval(_pairTimer);
  _pairTimer = setInterval(pairPoll, 2000);
}
function closePair() {
  if (_pairTimer) { clearInterval(_pairTimer); _pairTimer = null; }
  closeDialog("pair");
  loadManageData();
}
async function pairLoadEnc() {
  const snapshot = await api("GET", "/api/manage-snapshot");
  if (!Number.isInteger(snapshot.revision) || snapshot.revision < 0) {
    throw new Error("Bask returned an invalid setup snapshot");
  }
  _configRevision = snapshot.revision;
  _pairEnc = snapshot.enclosures;
  _species = snapshot.species;
  // The pairing sheet resolves each filled slot to a sensor name through
  // _sensors. Loading the snapshot without setting it left that map empty
  // whenever pairing was opened before Manage, so every slot silently fell
  // back to its MAC address — the one screen where knowing which sensor is
  // which is the entire point.
  _sensors = snapshot.sensors;
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
            onclick="pairAssign('${idAttr(encId)}','${idAttr(side)}')">
      <span class="pts-label">${side === "warm" ? "🔥 Warm" : "❄ Cool"}</span>
      <span class="pts-who">${filled ? "✓ " + who : "tap to set"}</span>
    </button>
    ${filled ? `<button class="pt-undo" onclick="event.stopPropagation();pairUndo('${idAttr(encId)}','${idAttr(slot.mac)}')" title="Clear">✕</button>` : ""}`;
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
  } catch (e) { if (!e.conflict) showToast("Assign failed — try again"); }
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
      <button class="btn sm" onclick="editSpecies('${idAttr(s.id)}')">Edit</button>
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
      <button class="close-btn" onclick="closeEditor()" aria-label="Close editor">✕</button></div>
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
      ${sp ? `<button class="btn danger" onclick="deleteSpecies('${idAttr(sp.id)}')">Delete</button>` : ""}
      <button class="btn primary" onclick="saveSpecies(${sp ? `'${idAttr(sp.id)}'` : "null"})">Save</button></div>`);
}
function stepper(key, val, label, step, dflt) {
  const has = val != null;
  return `<div class="field">
    <label>${label}</label>
    <div class="stepper" id="st-${key}" data-val="${has ? val : ""}" data-step="${step}" data-default="${dflt}">
      <button class="step-btn" onclick="stepVal('${idAttr(key)}',-1)">−</button>
      <div class="sval ${has ? "" : "unset"}">${has ? val : "off"}</div>
      <button class="step-btn" onclick="stepVal('${idAttr(key)}',1)">+</button>
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
      `${esc(o.name)} ${num(o.temp)}${u}→${num(o.setpoint)}${u}`).join(" · ") || "No outputs";
    else if (reach === false) sub = "Offline — check the IP and that the status page is on";
    else sub = "Connecting…";
    return `
      <div class="row"><div class="row-top">
        <div class="row-info">
          <div class="row-name"><span class="tdot ${dotCls}"></span>${esc(st.name || t.name || t.ip)}</div>
          <div class="row-sub">${sub}</div>
          <div class="row-mac">${esc(t.ip)}</div>
        </div>
        <button class="btn sm" onclick="editThermostat('${idAttr(t.ip)}')">Edit</button>
      </div></div>`;
  }).join("");
  document.getElementById("pane-thermostats").innerHTML = `
    <div class="pane-toolbar"><h2>Animal room devices</h2></div>
    ${renderHumidifierSettings()}
    ${renderCieloSettings()}
    <div class="pane-toolbar"><h2>Herpstat thermostats</h2>
      <button class="btn primary" onclick="editThermostat(null)">+ Add</button></div>
    <div class="scan-hint">Monitor Herpstat SpyderWeb thermostats on your network. On each unit, enable its
      <b>web status page</b> so <code>http://&lt;ip&gt;/RAWSTATUS</code> responds, then add its IP here.
      The dashboard strip appears once a unit is added.</div>
    ${rows || `<div class="muted-note">No thermostats yet. This feature is optional — add one to show the live strip.</div>`}`;
}

function renderCieloSettings() {
  if (!_cielo.configured) {
    return `<div class="integration-card">
      <div class="integration-head"><div><h2>Animal room climate</h2>
        <div class="row-sub">Cielo Breez Max · optional</div></div>
        <button class="btn primary" onclick="connectCielo()">Connect</button></div>
      <p>Show the mini-split controller's temperature, humidity, mode, and setpoint in the dashboard corner.
        Bask only reads its status; it cannot change the AC.</p>
      <p class="cloud-note">Cloud connection · updates about every 2 minutes</p>
    </div>`;
  }
  const unit = _cielo.temp_unit === "C" ? "°C" : "°F";
  const devices = _cielo.devices || [];
  const noDevices = devices.length === 0;
  const deviceOptions = devices.map(d =>
    `<option value="${esc(d.id)}" ${d.id === _cielo.selected_device_id ? "selected" : ""}>${esc(d.name)}</option>`).join("");
  // Distinguish "connected but Cielo lists no controller" (e.g. the device was
  // unlinked, or the key is from another account) from a genuine first-poll wait.
  const reading = _cielo.temperature != null
    ? `${_cielo.temperature}${unit} · ${_cielo.humidity ?? "—"}% · ${_cielo.power ? esc(_cielo.mode || "on") : "off"}`
    : (noDevices ? "No controller found on this Cielo account" : "Waiting for first update…");
  const statusBad = _cielo.error || _cielo.online === false || noDevices;
  const note = _cielo.error
    ? _cielo.error
    : (noDevices
      ? "Bask connected, but Cielo reports no controllers for this account. If your Breez Max was unlinked or is on a different Cielo login, add it back to this account — Bask re-checks about every 2 minutes and it will appear here automatically."
      : "Read-only Cielo cloud connection · updates about every 2 minutes");
  return `<div class="integration-card">
    <div class="integration-head"><div><h2>Animal room climate</h2>
      <div class="row-sub"><span class="tdot ${statusBad ? "bad" : "ok"}"></span>
        ${esc(_cielo.name || "Cielo Breez Max")}</div></div>
      <button class="btn danger sm" onclick="disconnectCielo()">Disconnect</button></div>
    <p>${reading}</p>
    ${devices.length > 1 ? `<div class="field compact"><label>Controller</label>
      <select id="cielo-device" onchange="selectCieloDevice(this.value)">
        <option value="">Choose a controller…</option>${deviceOptions}</select></div>` : ""}
    <p class="cloud-note">${esc(note)}</p>
  </div>`;
}

function connectCielo() {
  openEditor(`
    <div class="sheet-head"><h2>Connect Cielo Breez</h2>
      <button class="close-btn" onclick="closeEditor()" aria-label="Close editor">✕</button></div>
    <div class="scan-hint"><b>Important:</b> Cielo permits each API key to be used only once and limits
      generation to three keys per month. Generate a fresh key in the Cielo app, paste it once here,
      and let Bask preserve the resulting token.</div>
    <div class="field"><label>Cielo Connect API key</label>
      <input type="password" id="cielo-key" autocomplete="off" spellcheck="false"
             placeholder="Paste API key"></div>
    <div id="cielo-result" class="test-result"></div>
    <div class="form-actions"><button class="btn primary" id="cielo-connect-btn"
      onclick="saveCielo()">Connect once</button></div>`);
}

async function saveCielo() {
  const keyEl = document.getElementById("cielo-key");
  const out = document.getElementById("cielo-result");
  const btn = document.getElementById("cielo-connect-btn");
  const apiKey = keyEl.value.trim();
  if (!apiKey) return;
  btn.disabled = true;
  out.className = "test-result"; out.textContent = "Connecting…";
  try {
    await api("POST", "/api/cielo/connect", { api_key: apiKey });
    keyEl.value = "";
    closeEditor();
    await loadManageData();
    refreshDashboard();
    showToast("Cielo Breez connected");
  } catch (e) {
    keyEl.value = "";
    out.className = "test-result bad";
    out.textContent = e.message;
    btn.disabled = false;
  }
}

async function selectCieloDevice(deviceId) {
  if (!deviceId) return;
  try {
    await api("PUT", "/api/cielo/device", { device_id: deviceId });
    await loadManageData();
    refreshDashboard();
  } catch (e) { showToast(e.message); }
}

async function disconnectCielo() {
  if (!confirm("Disconnect Cielo and remove its saved API credentials from this Bask server?")) return;
  await api("DELETE", "/api/cielo");
  await loadManageData();
  refreshDashboard();
  showToast("Cielo disconnected");
}

function renderHumidifierSettings() {
  if (!_vesync.configured) {
    return `<div class="integration-card">
      <div class="integration-head"><div><h2>Animal room humidifier</h2>
        <div class="row-sub">Levoit Classic 300S / VeSync · optional</div></div>
        <button class="btn primary" onclick="connectVeSync()">Connect</button></div>
      <p>Show current humidity, power, mode, target, mist level, and low-water status on the dashboard.
        Bask only reads status; it cannot operate the humidifier.</p>
      <p class="cloud-note">VeSync cloud connection · updates about every 2 minutes</p>
    </div>`;
  }
  const devices = _vesync.devices || [];
  const noDevices = devices.length === 0;
  const options = devices.map(d =>
    `<option value="${esc(d.id)}" ${d.id === _vesync.selected_device_id ? "selected" : ""}>${esc(d.name)}${d.model ? ` (${esc(d.model)})` : ""}</option>`).join("");
  const lowWater = _vesync.water_lacks === true || String(_vesync.water_lacks).toLowerCase() === "on";
  const reading = _vesync.humidity != null
    ? `${_vesync.humidity}% · ${_vesync.power ? esc(_vesync.mode || "on") : "off"}${lowWater ? " · refill water" : ""}`
    : (noDevices ? "No supported humidifier found on this VeSync account" : "Waiting for first update…");
  const statusBad = _vesync.error || _vesync.online === false || noDevices || lowWater;
  const note = _vesync.error || (noDevices
    ? "Bask connected, but VeSync did not return a supported humidifier. Confirm the Classic 300S is online in the VeSync app."
    : "Read-only VeSync cloud connection · updates about every 2 minutes");
  return `<div class="integration-card">
    <div class="integration-head"><div><h2>Animal room humidifier</h2>
      <div class="row-sub"><span class="tdot ${statusBad ? "bad" : "ok"}"></span>
        ${esc(_vesync.name || "Levoit humidifier")}</div></div>
      <button class="btn danger sm" onclick="disconnectVeSync()">Disconnect</button></div>
    <p>${reading}</p>
    ${devices.length > 1 ? `<div class="field compact"><label>Humidifier</label>
      <select onchange="selectVeSyncDevice(this.value)"><option value="">Choose a humidifier…</option>${options}</select></div>` : ""}
    <p class="cloud-note">${esc(note)}</p>
  </div>`;
}

function connectVeSync() {
  openEditor(`
    <div class="sheet-head"><h2>Connect VeSync humidifier</h2>
      <button class="close-btn" onclick="closeEditor()" aria-label="Close editor">✕</button></div>
    <div class="scan-hint"><b>Apple/Google sign-in?</b> Third-party VeSync integrations cannot use
      social sign-in. Create a separate VeSync account with an email and its own password, then use
      VeSync's device-sharing feature to share the humidifier with it. Never enter your Apple password here.</div>
    <div class="scan-hint"><b>Private credential:</b> Bask stores this dedicated VeSync login only in its
      private data directory with owner-only permissions. It is excluded from Git and portable exports.</div>
    <div class="field"><label>VeSync email</label><input type="email" id="vesync-user" autocomplete="username"></div>
    <div class="field"><label>VeSync password</label><input type="password" id="vesync-pass" autocomplete="current-password"></div>
    <div class="field"><label>Country code</label><input id="vesync-country" value="US" maxlength="2" autocapitalize="characters"></div>
    <div id="vesync-result" class="test-result"></div>
    <div class="form-actions"><button class="btn primary" id="vesync-connect-btn" onclick="saveVeSync()">Connect</button></div>`);
}

async function saveVeSync() {
  const user = document.getElementById("vesync-user");
  const pass = document.getElementById("vesync-pass");
  const out = document.getElementById("vesync-result");
  const btn = document.getElementById("vesync-connect-btn");
  if (!user.value.trim() || !pass.value) return;
  btn.disabled = true; out.className = "test-result"; out.textContent = "Connecting…";
  try {
    await api("POST", "/api/vesync/connect", {
      username: user.value.trim(), password: pass.value,
      country_code: document.getElementById("vesync-country").value.trim() || "US",
    });
    pass.value = ""; closeEditor(); await loadManageData(); refreshDashboard();
    showToast("Levoit humidifier connected");
  } catch (e) {
    pass.value = ""; out.className = "test-result bad"; out.textContent = e.message; btn.disabled = false;
  }
}

async function selectVeSyncDevice(deviceId) {
  if (!deviceId) return;
  try {
    await api("PUT", "/api/vesync/device", { device_id: deviceId });
    await loadManageData(); refreshDashboard();
  } catch (e) { showToast(e.message); }
}

async function disconnectVeSync() {
  if (!confirm("Disconnect VeSync and remove its saved login and token from this Bask server?")) return;
  await api("DELETE", "/api/vesync");
  await loadManageData(); refreshDashboard(); showToast("VeSync humidifier disconnected");
}

function editThermostat(ip) {
  const t = ip ? _thermostats_cfg.find(x => x.ip === ip) : null;
  openEditor(`
    <div class="sheet-head"><h2>${t ? "Edit" : "Add"} thermostat</h2>
      <button class="close-btn" onclick="closeEditor()" aria-label="Close editor">✕</button></div>
    <div class="field"><label>IP address</label>
      <input type="text" id="tf-ip" value="${esc(t?.ip || "")}" placeholder="e.g. 192.168.1.50"
             inputmode="decimal" autocomplete="off"></div>
    <div class="field"><label>Display name (optional — defaults to the unit's own name)</label>
      <input type="text" id="tf-name" value="${esc(t?.name || "")}" placeholder="e.g. Rack 1 Herpstat"></div>
    <div class="field"><label>Temperature unit configured on this Herpstat</label>
      <select id="tf-unit">
        <option value="F" ${(t?.temp_unit || _tempUnit) === "F" ? "selected" : ""}>Fahrenheit (°F)</option>
        <option value="C" ${(t?.temp_unit || _tempUnit) === "C" ? "selected" : ""}>Celsius (°C)</option>
      </select>
      <div class="scan-hint">This can differ from Bask's display unit. It keeps historical readings accurate.</div>
    </div>
    <label class="night-toggle">
      <input type="checkbox" id="tf-enabled" ${t?.enabled === false ? "" : "checked"}>
      <span>Enabled (poll this unit)</span>
    </label>
    <div class="field"><button class="btn ghost sm" onclick="testThermostat()">⚡ Test connection</button>
      <div id="tf-test" class="test-result"></div></div>
    <div class="form-actions">
      ${t ? `<button class="btn danger" onclick="deleteThermostat('${idAttr(t.ip)}')">Delete</button>` : ""}
      <button class="btn primary" onclick="saveThermostat(${t ? `'${idAttr(t.ip)}'` : "null"})">Save</button>
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
  const temp_unit = document.getElementById("tf-unit").value;
  const body = { ip: newIp, name, enabled, temp_unit };
  try {
    if (ip) await api("PUT", `/api/thermostats/${encodeURIComponent(ip)}`, body);
    else await api("POST", "/api/thermostats", body);
  } catch (e) {
    if (!e.conflict) showToast(ip ? "Save failed" : "That IP is already added");
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
    <div class="field"><label>Day and night — when ☀️ day ranges apply (outside = 🌙 night)</label>
      <div class="toggle-row">
        <button class="btn ${(s.day_mode || "fixed") === "fixed" ? "on" : ""}" onclick="setDayMode('fixed')">Set hours</button>
        <button class="btn ${s.day_mode === "solar" ? "on" : ""}" onclick="setDayMode('solar')">Sunrise &amp; sunset</button>
      </div>
      ${s.day_mode === "solar" ? solarDayFields(s) : `
      <div class="daywin">
        <div class="dw-cell"><span>From</span>${hourStepper("day_start_hour", s.day_start_hour ?? 8)}</div>
        <div class="dw-cell"><span>To</span>${hourStepper("day_end_hour", s.day_end_hour ?? 20)}</div>
      </div>`}</div>
    <div class="field"><label>📱 Phone alerts — get pinged when an enclosure needs attention</label>
      <div id="alerts-setting"></div></div>
    <div class="field"><label>🔄 Updates</label>
      <div id="update-setting"></div></div>
    <div class="field"><label>🔑 Head Keeper key — who can change this setup</label>
      <div id="keeper-setting"></div></div>
    <div class="field"><label>💾 Backup — your enclosures, ranges and settings in one file</label>
      <div class="toggle-row">
        <a class="btn" href="/api/config/export" download>⬇ Download backup</a>
        <button class="btn" onclick="document.getElementById('import-file').click()">⬆ Restore from backup</button>
      </div>
      <input type="file" id="import-file" accept="application/json,.json" style="display:none"
             onchange="importSettings(this)"></div>`;
  refreshAlertsUI();
  refreshUpdateUI();
  renderKeeperSetting();
}

// ── Head Keeper key management ───────────────────────────────
function renderKeeperSetting() {
  const el = document.getElementById("keeper-setting");
  if (!el) return;
  if (!_keeper.configured) {
    el.innerHTML = `
      <div class="muted-note" style="text-align:left;padding:4px 0">
        No key set — anyone on your network can change this setup. Setting one
        keeps the dashboard readable by everyone while limiting changes to you.
      </div>
      <div class="keeper-row">
        <input id="keeper-new" type="password" autocomplete="new-password" placeholder="New Head Keeper key">
        <button class="btn on" onclick="saveKeeperKey()">Set key</button>
      </div>
      <div class="muted-note" style="text-align:left">
        Using Shed too? Enter the same Head Keeper code and you only have one to remember.
      </div>
      <p id="keeper-set-error" class="keeper-error" role="alert"></p>`;
    return;
  }
  el.innerHTML = `
    <div class="muted-note" style="text-align:left;padding:4px 0">
      Setup is protected. The dashboard stays open to everyone on your network.
    </div>
    <div class="keeper-row">
      <input id="keeper-new" type="password" autocomplete="new-password" placeholder="New key">
      <button class="btn" onclick="saveKeeperKey()">Change key</button>
    </div>
    <div class="toggle-row" style="margin-top:8px">
      <button class="btn" onclick="lockKeeper()">🔒 Lock this device</button>
      <button class="btn danger" onclick="removeKeeperKey()">Remove key</button>
    </div>
    <p id="keeper-set-error" class="keeper-error" role="alert"></p>`;
}

async function saveKeeperKey() {
  const field = document.getElementById("keeper-new");
  const error = document.getElementById("keeper-set-error");
  error.textContent = "";
  try {
    await api("POST", "/api/keeper/key", { key: field.value });
  } catch (e) {
    if (!e.conflict) error.textContent = e.message || "Couldn't save that key.";
    return;
  }
  field.value = "";
  await refreshKeeperState();
  renderKeeperSetting();
  showToast("Head Keeper key saved");
}

async function removeKeeperKey() {
  if (!confirm("Remove the Head Keeper key?\n\nAnyone on your network will be able to change this setup again.")) return;
  try {
    await api("DELETE", "/api/keeper/key");
  } catch (e) {
    if (!e.conflict) showToast("Couldn't remove the key");
    return;
  }
  await refreshKeeperState();
  renderKeeperSetting();
  showToast("Head Keeper key removed");
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
  } catch (e) { if (!e.conflict) showToast("Restore failed — not a valid Bask backup"); }
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
let _ntfyDelivery = null;

function alertStatusTime(epoch) {
  const value = Number(epoch);
  if (epoch === null || epoch === undefined || !Number.isFinite(value) || value <= 0) {
    return "not yet";
  }
  return new Date(value * 1000).toLocaleString([], {
    month: "short", day: "numeric", hour: "numeric", minute: "2-digit"
  });
}

function alertDeliveryMarkup(status) {
  if (!status || typeof status !== "object") return "";
  const pending = Number.isInteger(status.pending) && status.pending > 0 ? status.pending : 0;
  let summary;
  if (!status.enabled) {
    summary = "Phone alerts are turned off.";
  } else if (pending) {
    summary = `<b>${pending}</b> alert${pending === 1 ? " is" : "s are"} waiting to send${status.retrying ? " and will retry automatically" : ""}.`;
  } else {
    summary = "No alerts are waiting to send.";
  }
  const success = `Last alert delivered: <b>${esc(alertStatusTime(status.last_success_at))}</b>`;
  const retry = status.retrying && status.next_retry_at
    ? `<div>Next automatic retry: <b>${esc(alertStatusTime(status.next_retry_at))}</b></div>` : "";
  const error = status.last_error_at
    ? `<div>Last delivery issue: <b>${esc(alertStatusTime(status.last_error_at))}</b>${status.last_error ? ` — ${esc(status.last_error)}` : ""}</div>`
    : `<div>No delivery issues recorded.</div>`;
  return `<div class="ntfy-delivery" role="status" aria-live="polite" aria-atomic="true"
               aria-label="Phone alert delivery status">
    <div>${summary}</div><div>${success}</div>${retry}${error}
  </div>`;
}

async function refreshAlertsUI() {
  const el = document.getElementById("alerts-setting");
  if (!el) return;
  try { _ntfy = await api("GET", "/api/ntfy"); }
  catch (e) { el.innerHTML = ""; return; }
  try { _ntfyDelivery = await api("GET", "/api/ntfy/delivery"); }
  catch (e) { _ntfyDelivery = null; }
  if (!_ntfy.enabled) {
    el.innerHTML = `<button class="btn primary" onclick="enableAlerts()">Set up phone alerts</button>
      <div class="muted-note" style="padding:6px 0 0;text-align:left">Free, ~1 minute. Uses the ntfy notification app.</div>
      ${alertDeliveryMarkup(_ntfyDelivery)}`;
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
      ${alertDeliveryMarkup(_ntfyDelivery)}
      <div class="muted-note" style="padding:8px 0 0;text-align:left">Once subscribed, tap <b>Send test</b> — it should pop up on your phone.</div>
    </div>`;
}
async function enableAlerts() {
  try { await api("POST", "/api/ntfy", { enabled: true }); refreshAlertsUI(); }
  catch (e) { if (!e.conflict) showToast("Couldn't turn on alerts"); }
}
async function disableAlerts() {
  try { await api("POST", "/api/ntfy", { enabled: false }); }
  catch (e) { if (e.conflict) return; }
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
    <button class="step-btn" onclick="stepSetting('${idAttr(key)}',-1)">−</button>
    <div class="sval">${val} ${unit}</div>
    <button class="step-btn" onclick="stepSetting('${idAttr(key)}',1)">+</button></div>`;
}

function queueSettingWrite(body, applySavedValue) {
  const epoch = _settingsWriteEpoch;
  const run = async () => {
    if (epoch !== _settingsWriteEpoch) return;
    try {
      // The response carries the settings as saved, which matters when the
      // server derived something the client could not — resolving a ZIP to
      // coordinates, for one. Existing callers simply ignore it.
      const saved = await api("PUT", "/api/settings", body);
      if (epoch === _settingsWriteEpoch) applySavedValue(saved && saved.settings);
    } catch (error) {
      if (error.conflict) return; // global recovery already reloaded the form
      _settingsWriteEpoch += 1;   // cancel later clicks based on this failed form
      showToast("Couldn't save that setting — latest setup reloaded");
      try { await loadManageData(); } catch (_) {}
    }
  };
  // Both branches keep the queue live after a failed operation.
  _settingsWriteTail = _settingsWriteTail.then(run, run);
  return _settingsWriteTail;
}

async function stepSetting(key, dir) {
  const el = document.getElementById("set-" + key);
  const step = Number(el.dataset.step);
  let v = Number(el.dataset.val) + dir * step;
  if (v < step) v = step;
  el.dataset.val = v;
  el.querySelector(".sval").textContent = `${v} ${el.dataset.unit}`;
  await queueSettingWrite({ [key]: v }, () => { _settings[key] = v; });
}
function fmtHourLong(h) {
  const ap = h < 12 ? "AM" : "PM";
  return (h % 12 || 12) + " " + ap;
}
function hourStepper(key, val) {
  return `<div class="stepper" id="set-${key}" data-val="${val}">
    <button class="step-btn" onclick="stepHour('${idAttr(key)}',-1)">−</button>
    <div class="sval">${fmtHourLong(val)}</div>
    <button class="step-btn" onclick="stepHour('${idAttr(key)}',1)">+</button></div>`;
}
async function stepHour(key, dir) {
  const el = document.getElementById("set-" + key);
  const v = (Number(el.dataset.val) + dir + 24) % 24;
  el.dataset.val = v;
  el.querySelector(".sval").textContent = fmtHourLong(v);
  await queueSettingWrite({ [key]: v }, () => { _settings[key] = v; });
}
async function setUnit(u) {
  await queueSettingWrite({ temp_unit: u }, () => {
    _settings.temp_unit = u; _tempUnit = u;
    renderSettingsPane(); renderSpeciesPane();
  });
}

// ── Day and night from the sun ───────────────────────────────
// A ZIP is what most keepers know offhand; it is resolved to coordinates on the
// server from a bundled table, so nothing about where they live leaves the box.
// Coordinates stay editable directly, which is the only option outside the US.
function solarDayFields(s) {
  const placed = typeof s.latitude === "number" && typeof s.longitude === "number";
  const where = placed
    ? `${s.location_label ? s.location_label + " · " : ""}${s.latitude.toFixed(2)}, ${s.longitude.toFixed(2)}`
    : "";
  return `
    <div class="daywin-solar">
      <div class="dw-cell"><span>ZIP code</span>
        <input id="solar-zip" class="zip-input" inputmode="numeric" maxlength="10"
               placeholder="${placed ? (s.location_label || "set") : "e.g. 10001"}"
               onkeydown="if(event.key==='Enter')saveSolarZip()">
        <button class="btn" onclick="saveSolarZip()">Set</button>
      </div>
      ${placed ? `<div class="muted-note" style="text-align:left">Using ${where}</div>`
               : `<div class="muted-note" style="text-align:left">Set a location, or day and night fall back to the set hours.</div>`}
      <div class="daywin">
        <div class="dw-cell"><span>Day starts</span>${offsetStepper("sunrise_offset_minutes", s.sunrise_offset_minutes ?? 0)}</div>
        <div class="dw-cell"><span>Day ends</span>${offsetStepper("sunset_offset_minutes", s.sunset_offset_minutes ?? 0)}</div>
      </div>
      <div class="muted-note" style="text-align:left">Offsets shift each edge, the way a light timer is usually set a little inside first and last light.</div>
    </div>`;
}

function fmtOffset(v) {
  if (!v) return "at sunrise/sunset";
  return `${v > 0 ? "+" : "−"}${Math.abs(v)} min`;
}

function offsetStepper(key, val) {
  return `<div class="stepper" id="set-${key}" data-val="${val}">
    <button class="step-btn" onclick="stepOffset('${idAttr(key)}',-15)">−</button>
    <div class="sval">${fmtOffset(val)}</div>
    <button class="step-btn" onclick="stepOffset('${idAttr(key)}',15)">+</button></div>`;
}

async function stepOffset(key, dir) {
  const el = document.getElementById("set-" + key);
  // Clamped to the range the API accepts, so the control cannot ask for a
  // setting that will be refused.
  const v = Math.max(-180, Math.min(180, Number(el.dataset.val) + dir));
  el.dataset.val = v;
  el.querySelector(".sval").textContent = fmtOffset(v);
  await queueSettingWrite({ [key]: v }, () => { _settings[key] = v; });
}

async function setDayMode(mode) {
  await queueSettingWrite({ day_mode: mode }, () => {
    _settings.day_mode = mode;
    renderSettingsPane();
  });
}

async function saveSolarZip() {
  const input = document.getElementById("solar-zip");
  const zip = (input.value || "").trim();
  if (!zip) return;
  input.value = "";
  await queueSettingWrite({ zip_code: zip }, (settings) => {
    // The server resolves the ZIP, so take the coordinates back from it rather
    // than guessing them here.
    if (settings) Object.assign(_settings, settings);
    renderSettingsPane();
  });
}

// ── editor sheet plumbing ────────────────────────────────────
function openEditor(html) {
  document.getElementById("editor-sheet").innerHTML = html;
  openDialog("editor");
}
function closeEditor() { closeDialog("editor"); }

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
