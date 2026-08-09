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

function listNames(names) {
  const unique = [...new Set(names.filter(Boolean))];
  if (!unique.length) return "";
  if (unique.length === 1) return unique[0];
  if (unique.length === 2) return `${unique[0]} and ${unique[1]}`;
  if (unique.length <= 4) return `${unique.slice(0, -1).join(", ")}, and ${unique.at(-1)}`;
  return `${unique.slice(0, 3).join(", ")}, and ${unique.length - 3} more`;
}

// Rotate flavor lines slowly (~20 min buckets) so the ticker text stays put long
// enough to scroll all the way through instead of flickering every refresh.
function rotate(options) {
  return options[Math.floor(Date.now() / 1200000) % options.length];
}

const lowerFirst = (value) => (value ? value.charAt(0).toLowerCase() + value.slice(1) : value);

function buildMessages(data) {
  const bask = data.bask || {};
  const enclosures = bask.enclosures || [];
  const counts = bask.counts || {};
  const messages = [];
  const hour = new Date().getHours();

  const attention = enclosures.filter((enc) => enc.status === "warning" || enc.status === "danger");
  const waiting = enclosures.filter((enc) => enc.status === "stale" || enc.status === "no_data");
  const okCount = counts.ok || 0;

  // ── How the room's feeling ──
  if (!attention.length && !waiting.length && okCount) {
    messages.push(rotate([
      `Everything's cozy — all ${okCount} enclosures are right where they should be. 🌿`,
      `The whole room's dialed in. Every habitat is sitting in its happy range.`,
      `All green in here. Temps and humidity are on point across the board.`,
      `Nothing to fuss over — every enclosure is comfortable right now.`,
    ]));
  } else {
    if (attention.length) {
      const names = listNames(attention.map((enc) => enc.name));
      messages.push(attention.length === 1
        ? `${names}'s enclosure could use a peek — it's drifted out of range.`
        : `A few want a look: ${names} have wandered out of range.`);
    }
    if (waiting.length) {
      const names = listNames(waiting.map((enc) => enc.name));
      messages.push(waiting.length === 1
        ? `Still waiting to hear from ${names}'s sensor.`
        : `Waiting on fresh readings from ${names}.`);
    }
    if (okCount) messages.push(`The other ${okCount} are sitting pretty. 🌿`);
  }

  // ── Care, in plain language ──
  const shed = data.shed?.data;
  if (shed) {
    const { remaining, overdue, completed, total } = shed.summary;
    const tasks = shed.tasks || [];
    const overdueTasks = shed.overdue || [];

    if (!remaining && !overdue) {
      messages.push(rotate([
        `Every bit of today's care is done — nicely handled. 🎉`,
        `Today's list is all checked off. The critters are set.`,
        `Care's all caught up. Everybody's been looked after today.`,
      ]));
    } else {
      if (remaining) {
        const next = tasks[0];
        messages.push(next
          ? `${remaining} to go today — next up is ${lowerFirst(next.title)} for ${next.animalName}.`
          : `${remaining} more ${remaining === 1 ? "thing" : "things"} on today's care list.`);
      }
      if (overdue) {
        const who = listNames(overdueTasks.map((task) => task.animalName));
        messages.push(who
          ? `Don't forget ${who} from earlier — still waiting on you.`
          : `${overdue} ${overdue === 1 ? "task is" : "tasks are"} carried over from earlier.`);
      }
    }
    if (total && completed) messages.push(`${completed} of ${total} knocked out so far today.`);

    const hungry = listNames(tasks
      .filter((task) => /feed|salad|insect|cgd|smoothie|rat|mouse|dubia|worm/i.test(`${task.taskType} ${task.title}`))
      .map((task) => task.animalName));
    if (hungry) messages.push(`Still owe dinner to ${hungry}.`);
  } else if (data.shed?.configured) {
    messages.push(`Shed's catching its breath — the sensors are still keeping watch, though.`);
  }

  // ── The room itself (Cielo, when connected) ──
  const climate = bask.room_climate;
  // `available` is not a field Cielo's public_status has ever emitted — it
  // publishes configured/selected/stale/error plus the device state, where
  // reachability is `online`. This condition was therefore never true, and the
  // room-climate line never appeared.
  if (climate?.configured && climate.online !== false && !climate.stale && climate.temperature != null) {
    messages.push(`The room itself is a comfy ${Math.round(climate.temperature)}°${climate.humidity != null ? ` at ${Math.round(climate.humidity)}% humidity` : ""}.`);
  }

  const humidifier = bask.humidifier;
  if (humidifier?.configured) {
    const lowWater = humidifier.water_lacks === true || String(humidifier.water_lacks).toLowerCase() === "on";
    if (lowWater) {
      messages.push(`The animal-room humidifier needs a water refill. 💧`);
    } else if (humidifier.error || humidifier.stale || humidifier.online === false) {
      messages.push(`The animal-room humidifier status needs a quick check.`);
    } else if (humidifier.humidity != null) {
      const mode = humidifier.power ? ` and running${humidifier.mode ? ` in ${humidifier.mode} mode` : ""}` : " and currently off";
      messages.push(`Room humidity is ${Math.round(humidifier.humidity)}%${mode}.`);
    }
  }

  // ── A warm sign-off so it never reads purely as a status printout ──
  messages.push(rotate([
    hour < 12 ? `Morning, keepers — hope everybody slept well. 🦎`
      : hour < 17 ? `Hope the afternoon's treating the crew well. 🐢`
      : `Winding down for the evening — sweet dreams, critters. 🌙`,
    `Thanks for keeping this little room running. 💚`,
  ]));

  return messages;
}

function render(data) {
  latest = data;
  renderBask(data.bask);
  renderShed(data.shed);
  const messages = buildMessages(data);
  const tickerHtml = messages.map((message) => `<span>${escapeHtml(message)}</span>`).join("");
  const track = $("#ticker-track");
  // Only rebuild when the text actually changes — otherwise every 15s refresh
  // restarts the scroll animation and it never gets past the first message.
  if (track.dataset.content !== tickerHtml) {
    track.innerHTML = tickerHtml;
    track.dataset.content = tickerHtml;
  }
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
