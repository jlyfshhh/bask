const $ = (selector) => document.querySelector(selector);
let latest = null;

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (char) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;",
  }[char]));
}

function timeGreeting(hour) {
  if (hour < 12) return "Good morning";
  if (hour < 17) return "Good afternoon";
  return "Good evening";
}

function updateClock() {
  const now = new Date();
  $("#clock").textContent = now.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  $("#date").textContent = now.toLocaleDateString([], { weekday: "long", month: "long", day: "numeric" });
  $("#greeting").textContent = `${timeGreeting(now.getHours())} in the animal room.`;
}

function formatReading(sensor, kind) {
  if (!sensor || sensor[kind] == null) return "—";
  if (kind === "temp") return `${Math.round(sensor.temp)}°`;
  return `${Math.round(sensor.humidity)}%`;
}

function renderBask(bask) {
  const counts = bask.counts || {};
  const attention = (counts.warning || 0) + (counts.danger || 0) + (counts.stale || 0);
  $("#bask-counts").innerHTML = `
    <div class="metric good"><strong>${counts.ok || 0}</strong><span>Green</span></div>
    <div class="metric attention"><strong>${attention}</strong><span>Attention</span></div>
    <div class="metric offline"><strong>${counts.no_data || 0}</strong><span>No data</span></div>`;

  $("#enclosures").innerHTML = (bask.enclosures || []).map((enc) => {
    const warmBad = enc.warm && !enc.warm_temp_ok;
    const coolBad = enc.cool && !enc.cool_temp_ok;
    const humiditySensor = enc.cool || enc.warm;
    return `<article class="enclosure ${escapeHtml(enc.status)}">
      <div>
        <div class="enc-head"><h3>${escapeHtml(enc.name)}</h3><span class="status-dot"></span></div>
        <p class="species">${escapeHtml(enc.species_name || "No species profile")}</p>
      </div>
      <div class="readings">
        <div class="reading ${enc.warm ? (warmBad ? "bad" : "") : "missing"}"><strong>${formatReading(enc.warm, "temp")}</strong><span>Warm</span></div>
        <div class="reading ${enc.cool ? (coolBad ? "bad" : "") : "missing"}"><strong>${formatReading(enc.cool, "temp")}</strong><span>Cool</span></div>
        <div class="reading ${humiditySensor ? (!enc.humidity_ok ? "bad" : "") : "missing"}"><strong>${formatReading(humiditySensor, "humidity")}</strong><span>Humidity</span></div>
      </div>
    </article>`;
  }).join("") || `<div class="empty">No enclosures are configured in Bask.</div>`;
}

function renderShed(shed) {
  if (!shed.configured) {
    $("#task-list").innerHTML = `<div class="empty">Connect Shed to show today’s care.</div>`;
    return;
  }
  if (!shed.data) {
    $("#task-list").innerHTML = `<div class="empty">Shed is temporarily unavailable.<br>Sensor monitoring is still live.</div>`;
    return;
  }

  const data = shed.data;
  const summary = data.summary;
  const percent = summary.total ? Math.round((summary.completed / summary.total) * 100) : 100;
  $("#task-ring strong").textContent = `${percent}%`;
  $("#task-progress").style.width = `${percent}%`;
  $("#task-metrics").innerHTML = `
    <div class="task-metric"><strong>${summary.completed}</strong><span>Complete</span></div>
    <div class="task-metric"><strong>${summary.remaining}</strong><span>Remaining</span></div>
    <div class="task-metric overdue"><strong>${summary.overdue}</strong><span>Overdue</span></div>`;

  const combined = [
    ...data.overdue.map((task) => ({ ...task, overdue: true })),
    ...data.tasks.map((task) => ({ ...task, overdue: false })),
  ];
  const visible = combined.slice(0, 9);
  $("#task-list").innerHTML = visible.length
    ? visible.map((task) => {
        // Lead with the animal (like Shed's own list) so the panel doesn't read as a
        // stack of identical task titles; show the specific task + guidance beneath.
        const title = (task.title || "").trim();
        const detail = (task.details || "").trim();
        const lower = (value) => value.toLowerCase();
        const sub = title && detail && !lower(detail).includes(lower(title)) && !lower(title).includes(lower(detail))
          ? `${title} · ${detail}`
          : (detail || title || task.taskType || "");
        const right = task.overdue ? `overdue · ${task.dueDate}` : (task.species || "");
        return `<article class="task-item ${task.overdue ? "overdue" : ""}">
          <span class="bar"></span>
          <div class="task-copy">
            <h3>${escapeHtml(task.animalName)}</h3>
            <p>${escapeHtml(sub)}</p>
          </div>
          <span class="task-animal">${escapeHtml(right)}</span>
        </article>`;
      }).join("") + (combined.length > visible.length
        ? `<div class="more-tasks">+ ${combined.length - visible.length} more in Shed</div>` : "")
    : `<div class="empty">Everything scheduled for today is complete.</div>`;
}

function buildMessages(data) {
  const bask = data.bask;
  const counts = bask.counts || {};
  const messages = [];
  const trouble = (counts.warning || 0) + (counts.danger || 0);
  const waiting = (counts.stale || 0) + (counts.no_data || 0);

  if (!trouble && !waiting) {
    messages.push("All monitored enclosures are currently green.");
  } else {
    if (trouble) messages.push(`${trouble} enclosure${trouble === 1 ? "" : "s"} need climate attention.`);
    if (waiting) messages.push(`${waiting} enclosure${waiting === 1 ? "" : "s"} are waiting for fresh sensor data.`);
  }
  if (counts.danger) messages.push(`${counts.danger} enclosure${counts.danger === 1 ? "" : "s"} have multiple readings outside their target range.`);

  const shed = data.shed?.data;
  if (shed) {
    const { remaining, overdue, completed, total } = shed.summary;
    messages.push(remaining ? `${remaining} care task${remaining === 1 ? "" : "s"} remain today.` : "Today’s scheduled care is complete.");
    if (overdue) messages.push(`${overdue} earlier task${overdue === 1 ? "" : "s"} still need attention.`);
    if (total) messages.push(`${completed} of ${total} scheduled tasks have been completed today.`);
    const feeding = shed.tasks.filter((task) => /feed|salad|insect|cgd|smoothie/i.test(`${task.taskType} ${task.title}`)).length;
    if (feeding) messages.push(`${feeding} feeding task${feeding === 1 ? "" : "s"} are still on today’s list.`);
  } else if (data.shed?.configured) {
    messages.push("Shed is offline; Bask sensor monitoring is still working.");
  }

  if (bask.room_climate?.configured && bask.room_climate?.available) {
    const room = bask.room_climate;
    messages.push(`Room climate is ${Math.round(room.temperature)}° with ${Math.round(room.humidity)}% humidity.`);
  }
  return messages;
}

function render(data) {
  latest = data;
  renderBask(data.bask);
  renderShed(data.shed);
  const messages = buildMessages(data);
  $("#ticker-track").innerHTML = messages.map((message) => `<span>${escapeHtml(message)}</span>`).join("");
  const counts = data.bask.counts || {};
  const attention = (counts.warning || 0) + (counts.danger || 0);
  const remaining = data.shed?.data?.summary?.remaining;
  const climatePart = attention ? `${attention} enclosure${attention === 1 ? "" : "s"} need attention` : "Climate targets look good";
  const carePart = Number.isFinite(remaining) ? `${remaining} care task${remaining === 1 ? "" : "s"} remaining` : "Shed status unavailable";
  $("#summary").textContent = `${climatePart} · ${carePart}`;
  $("#connection").classList.remove("offline");
  $("#connection").innerHTML = "<span></span> Live";
  $("#last-updated").textContent = `Updated ${new Date(data.generated_at * 1000).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}`;
}

async function refresh() {
  try {
    const response = await fetch("/api/room-dashboard", { cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    render(await response.json());
  } catch (error) {
    console.error(error);
    $("#connection").classList.add("offline");
    $("#connection").innerHTML = "<span></span> Reconnecting";
    if (!latest) $("#summary").textContent = "The dashboard is reconnecting…";
  }
}

updateClock();
refresh();
setInterval(updateClock, 1000);
setInterval(refresh, 15000);
