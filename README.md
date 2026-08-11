<p align="center">
  <img src="docs/bask-logo.svg" width="190" alt="Bask">
</p>

<p align="center">
  <b>At-a-glance temperature &amp; humidity monitoring for your animal room.</b><br>
  Reads your Bluetooth thermo-hygrometers, groups them by enclosure, and tells you from across the room whether your husbandry is good.
</p>

<p align="center">
  <a href="#license"><img alt="License: MIT" src="https://img.shields.io/badge/license-MIT-F2A516"></a>
  <img alt="Python" src="https://img.shields.io/badge/python-3.11%2B-blue">
  <img alt="Self-hosted" src="https://img.shields.io/badge/self--hosted-Docker-A8B7A1">
  <a href="https://animalroom.app/bask/"><img alt="Website" src="https://img.shields.io/badge/website-animalroom.app%2Fbask-E39A13"></a>
  <a href="https://ko-fi.com/jlyfshhh"><img alt="Ko-fi" src="https://img.shields.io/badge/Ko--fi-buy%20crickets-FF5E5B?logo=ko-fi&logoColor=white"></a>
</p>

---

Bask is a small, self-hosted dashboard for reptile/amphibian keepers (or anyone with [Govee H5075](https://a.co/d/0f8luxOE) sensors). It listens to your sensors over Bluetooth, checks each enclosure against **per-species day/night ranges**, and shows a big green "all good" — or a red alert that names exactly what's wrong.

It runs as a Docker container on an inexpensive **Raspberry Pi** — a Pi 4,
Pi 3B+, Pi 5, or Zero 2 W all work great. The Pi only scans and serves; any
phone, tablet, or browser displays it. Core sensor monitoring needs no cloud,
account, or ongoing internet connection.

> **The idea:** walk into your animal room and know instantly — *green or not green* — whether everything's okay. Details are a tap away; status is readable from the doorway.

![Bask dashboard with example data](docs/dashboard.svg)

> 🆕 **New to Raspberry Pi?** The **[beginner's setup guide](docs/SETUP.md)** takes you from a working Raspberry Pi OS card to a dashboard in about 20 minutes—and includes fresh-card instructions if you need them. Nothing assumed.

## Features

- 📡 **Passive Bluetooth scanning** — no pairing, no cloud, no Govee account. Reads the sensors' broadcast advertisements locally.
- 🟢 **At-a-glance status banner** — big green "all good", or a red banner that names the out-of-range enclosure and metric.
- 🦎 **Enclosures + per-species ranges** — group a warm-side and cool-side sensor per enclosure; each species has its own acceptable temp/humidity ranges.
- ☀️🌙 **Day / night ranges** — set different ranges for heat-on vs. heat-off (configurable schedule). The dashboard switches automatically and shows which set is active.
- 🔋 **Battery + signal monitoring** — warns before a sensor dies or drops off.
- 🌡️ **Herpstat thermostat monitoring** *(optional)* — add [Herpstat SpyderWeb](https://www.spyderrobotics.com/) thermostats by IP and see each output's live probe temp, setpoint, output %, and alarms in a compact strip. Hidden entirely until you add one.
- ❄️ **Animal-room climate** *(optional)* — connect a Cielo Breez Max and see the room temperature, humidity, mini-split mode, and setpoint in a small dashboard-corner card.
- 💧 **Animal-room humidifier** *(optional)* — connect a Levoit Classic 300S through VeSync and see live humidity, power, mode, target, mist level, and low-water status without giving Bask control of the device.
- 📲 **Phone alerts** *(optional)* — get a notification on your phone when an enclosure goes out of range or a sensor drops off. Two-minute setup with the free [ntfy](https://ntfy.sh) app; the Pi only sends outbound, so nothing is exposed.
- 📱 **Installs like an app** — add Bask to your phone or tablet's home screen and it launches fullscreen with its own icon, like a native app.
- 👆 **Touch-first UI** — built for a wall-mounted touchscreen, with proximity pairing (hold a sensor near the host to add it).
- 🪶 **Tiny footprint** — two small Python processes, a narrowly filtered
  D-Bus proxy, and a vanilla-JS frontend. No build step, no framework, no
  database server.

## How it works

```
  Govee H5075 sensors             Host (e.g. Raspberry Pi)           Any display
  (in your enclosures)     ┌──────────────────────────────────┐    (tablet / browser /
                           │ BlueZ → filtered D-Bus proxy     │     smart display)
   temp / humidity / batt  │              ↓                   │
        │  BLE adverts     │ scanner ──writes──┐              │
        └────────────────▶ │ (unprivileged)    ▼              │
                           │             readings.db          │  HTTP   ┌────────────┐
                           │ web server ──reads─┘              │ ◀───────│  browser   │
                           │ (FastAPI + serves UI)             │  :8080  └────────────┘
                           └──────────────────────────────────┘
```

Two Bask processes share one SQLite file. A third, minimal proxy process keeps
host Bluetooth privileges out of both application processes:

- **`scanner/`** — passively listens for Govee advertisements through the
  filtered proxy, decodes temperature/humidity/battery, and writes them to
  `readings.db`. It runs without root, Linux capabilities, networking, or the
  host system-bus socket.
- **`bask-dbus-proxy`** — authenticates to BlueZ and permits only the object
  discovery, passive advertisement-monitor registration, and related signal
  traffic the scanner needs. It cannot reach Bask data or the network.
- **`server/`** — does no Bluetooth. Reads the database, evaluates each enclosure against its species' (day or night) ranges, and serves the dashboard + JSON API.
- **`frontend/`** — a plain HTML/CSS/JS dashboard served by the web server.

## Hardware

Bask is hardware-agnostic — adapt it to whatever you have:

- **Any current Raspberry Pi running 64-bit Raspberry Pi OS.** A **Pi 4** or **Pi 3B+** is the easy, widely-available pick; a **Pi Zero 2 W** is the most compact and lowest-power; a **Pi 5** works too. Many Pi kits already include a prepared OS card. *(The original ARMv6 Pi Zero W / Pi 1 are too slow and are not supported.)* Any other 64-bit Debian machine with a BLE adapter can work too, and macOS works for development.
- **One or more Govee H5075** sensors (other Govee BLE thermo-hygrometers that broadcast readings may also work).
- **A display** — an old tablet or phone, a monitor on the host, a smart display, or just any browser on your network.

## Install

### Easiest — one line on Raspberry Pi OS

If your Pi already boots into Raspberry Pi OS, connect it to your network, open
Terminal on the Pi (or connect with SSH), and paste:

```bash
curl -fsSL https://animalroom.app/bask/install.sh | bash
```

This installs Docker when needed, enables Bluetooth passive scanning and local
hostname discovery, and runs Bask as restart-safe containers with its settings
and history in `~/bask/data`. When it finishes it prints your dashboard URL —
`http://<hostname>.local:8080`. Run the same command again any time to update.
Updates are staged and validated first, create and verify a private config plus
SQLite backup, and restore the exact prior images, configuration, and running
state if startup or health verification fails.

New to Raspberry Pi or unsure what “open Terminal” means? The
**[beginner's setup guide](docs/SETUP.md)** walks through the supplied-card and
fresh-card paths, enabling SSH, installation, and first sensor setup.

Already running the former systemd/virtualenv version? The installer stops the
old services, takes a timestamped backup (including a consistent SQLite
snapshot), migrates the settings and history, and starts the container.

### Manual Docker install

Prefer to set it up yourself? With Docker Engine and the Compose plugin installed:

```bash
mkdir -p ~/bask && cd ~/bask
curl -fsSLO https://raw.githubusercontent.com/jlyfshhh/bask/main/compose.yaml
curl -fsSL https://raw.githubusercontent.com/jlyfshhh/bask/main/.env.example -o .env
mkdir -p data backups
docker compose up -d
```

Bask is published as a prebuilt multi-architecture image, so this downloads a
container rather than building one. To build from a checkout instead, clone the
repository and use the dev overlay:

```bash
docker compose -f compose.yaml -f compose.dev.yaml up -d --build
```

On Linux, enable BlueZ's reliable passive scanning mode once:

```bash
sudo sed -i 's/^#*Experimental = .*/Experimental = true/' /etc/bluetooth/main.conf
sudo systemctl restart bluetooth
sudo apt install -y avahi-daemon bluez rfkill
```

Then open `http://<hostname>.local:8080` (or `http://<host-ip>:8080`) in any browser, and tap **⚙ Manage → Sensors → Pair by proximity** to add your sensors.

## Updating

Re-run the one-line installer. It validates the new release in a temporary Git
worktree before touching the running service, then pulls the new image while
leaving `~/bask/data` in place:

```bash
curl -fsSL https://animalroom.app/bask/install.sh | bash
```

Settings has a **💾 Download backup** button. It writes an explicit list of
portable settings — sensors, enclosures, species, thermostats, and preferences —
and by construction contains no Head Keeper record, session secret, ntfy topic,
or integration credential. Restoring one never changes authentication: the key
and the private topic on the machine you restore onto are kept as they are.

Before every update, the installer uses `scripts/backup.sh` to create and verify
a private, machine-local archive containing configuration, SQLite history,
integration credentials, and pending alert state. The same script can be run
manually. Keep those archives to yourself. Storage defaults beneath `~/bask`;
external mounts require an explicit opt-in and are never recursively re-owned
by the installer.

## Configuration

Everything lives in the persistent `data/` directory (`config.json` plus
`readings.db`). You don't need to hand-edit it — the **Manage** screen in the UI does it all:

- **Sensors** — discovered Govee devices you've named.
- **Enclosures** — a name + species + which sensor is the warm and cool side.
- **Species** — acceptable ranges, each with a **day** set and an optional **night** set. The day/night schedule (e.g. 8am–8pm) is in **Settings**.
- **Thermostats** *(optional)* — Herpstat SpyderWeb units to monitor, by IP. See below.
- **Settings** — °F/°C, stale-after timeout, low-battery threshold, and the daytime-hours window.

`config.example.json` ships with day/night ranges for eight common species as a starting point — see the disclaimer below.

## Herpstat thermostats (optional)

If you run [Herpstat SpyderWeb](https://www.spyderrobotics.com/) thermostats, Bask can show each unit's outputs — live probe temperature, setpoint, output %, and any alarms — in a compact strip above the enclosure grid. It reads each unit's built-in status page over your LAN; there's no cloud and nothing to install on the thermostat. **If you don't add any, the strip never appears.**

**1. Enable the status page on each thermostat.** Bask reads `http://<unit-ip>/RAWSTATUS`, which is off by default. In the unit's network/web settings (via its touchscreen or the Spyder app), turn on the web **status page** (sometimes labelled "web enabled" / "status"). To confirm it's on, open `http://<unit-ip>/RAWSTATUS` in a browser — you should see a page of JSON. A static IP or DHCP reservation for each unit is recommended so its address doesn't change.

**2. Add it in Bask.** Go to **⚙ Manage → Thermostats → + Add**, enter the unit's IP, and tap **⚡ Test connection** to verify before saving. Bask polls each unit every few seconds and caches the result, so an offline or slow unit never stalls the dashboard. Output names come straight from the thermostat, so naming an output after its enclosure (e.g. "Ball Python") lines the strip up with your cards.

### Cielo Breez Max animal-room climate (optional)

Bask can also show a compact, read-only room-climate card from a Cielo Breez Max. Go to **⚙ Manage → Thermostats → Animal room climate → Connect** and paste a Cielo Connect API key. If the account has multiple controllers, choose which one represents the animal room.

Cielo permits an API key to be used only once and currently limits generation to three keys per month. Bask therefore saves the resulting access token and reuses it across restarts. Generate a fresh key, submit it once, and do not revoke it while Bask is using it. The integration polls Cielo's cloud about every two minutes, so it requires internet access; an outage only marks the card stale and never affects enclosure sensor monitoring.

The integration is deliberately **status-only**: Bask cannot turn the mini-split on/off or change its mode or setpoint. The API key and token are stored in `cielo-secrets.json` inside Bask's private data directory with owner-only file permissions. They are excluded from Git, Docker build context, and portable Manage-page exports. The private `scripts/backup.sh` filesystem archive includes them with owner-only permissions so a full server restore can preserve the integration; protect that archive like any other credential backup.

See Cielo's [Breez Max integration instructions](https://support.cielowigle.com/hc/en-us/articles/40965394269079-How-to-integrate-Home-Assistant-with-Breez-Max) for where to generate a Cielo Connect key.

### Levoit Classic 300S room humidifier (optional)

Go to **⚙ Manage → Thermostats → Animal room humidifier → Connect** and sign in with a regular VeSync email/password account that can access the humidifier. If the account contains more than one supported humidifier, choose the animal-room device after connecting.

**If the owner account uses Sign in with Apple or Google:** third-party VeSync libraries cannot use that social-login flow, and you must never enter your Apple/Google password into Bask. Create a separate VeSync account using an email address and a unique VeSync password, then share the humidifier to it using VeSync's family/friend device-sharing feature. Enter only that dedicated account in Bask.

Bask shows current humidity, power, mode, target humidity, mist level, connectivity, stale data, and a low-water warning. The integration is deliberately **status-only**: it cannot turn the humidifier on/off or change its settings. VeSync does not provide a supported local connection for this model, so status updates use the VeSync cloud about every two minutes and require internet access.

The account login and reusable token are stored in `vesync-secrets.json` and `vesync-token.json` inside Bask's private data directory with owner-only permissions. Both are excluded from Git, Docker build context, and portable Manage-page exports. The private `scripts/backup.sh` archive includes them so a full server restore can preserve the connection; protect that archive like any other credential backup.

## Phone alerts (optional)

Bask can ping your phone when an enclosure goes out of range, loses signal, or recovers. It uses [ntfy](https://ntfy.sh), a free open-source notification service: Bask generates a private, random topic for your install, and the Pi **posts outbound only** — nothing on your network is exposed, and Bask still needs no account.

Setup takes about two minutes: **⚙ Manage → Settings → Set up phone alerts**, install the free ntfy app (App Store / Google Play), and scan the QR code Bask shows you. Tap **Send test** to confirm. Alerts fire only after a condition has stayed changed for two minutes (in-range → out-of-range and back), which filters brief sensor/range flapping. A failed delivery is saved before it is sent and retried with a capped exponential delay, including after a Bask restart; Settings shows the Head Keeper whether anything is pending and the last success/error. Turning alerts off cancels pending sends, and turning them back on quietly establishes a fresh baseline instead of announcing conditions that arose while alerts were off.

> Your topic name is effectively a password — anyone who knows it can see your alerts (enclosure names and readings only). Bask generates a long random one; keep it private. Self-hosting an ntfy server also works — set `ntfy.server` in `config.json`.

The retry outbox and delivery history live in owner-only `alert-state.json` in
Bask's private data directory. They are deliberately excluded from the portable
Manage-page settings export, while `scripts/backup.sh` includes them in the
private machine backup so a restore cannot silently erase an alert awaiting
delivery. Delivery is at-least-once: in the rare case the process stops after
ntfy accepts a message but before Bask records that success, the retry may send
one duplicate rather than lose the alert.

## Displaying it

- **Any tablet / phone / computer** — just open the URL. A cheap wall-mounted tablet makes an excellent always-on display. On a phone or tablet, use your browser's **Add to Home Screen** — Bask installs like an app and launches fullscreen.
- **A monitor on the host** — `kiosk.sh` launches a fullscreen browser (it prefers the lightweight [cog](https://github.com/Igalia/cog) WPE browser, with Chromium as a fallback). Rendering a browser on a very low-power host (e.g. Pi Zero W) is slow, so a separate display device is usually smoother.
- **Smart displays** — anything with a web browser works. (For example, an Amazon Echo Show can open the URL in its Silk browser; `frontend/keep.js` includes a small same-origin keep-alive so Silk-class browsers don't time out — it activates only on that user-agent and is a no-op everywhere else.)

## Security

Bask is built for a **trusted local network**. Reading the dashboard is open to
anyone on that network on purpose — it is a wall display, and everyone in the
house should be able to glance at it. Changing the setup is not.

### Head Keeper key

A fresh install generates a **Head Keeper key** and prints it once. It is needed
to change sensors, enclosures, species ranges, thermostats, cloud integrations,
phone alerts, backups, and updates. It is stored only as a PBKDF2 hash — Bask
never writes it back in the clear and has no endpoint that returns it. Failed
unlock attempts are bounded by source, submitted-key fingerprint, and a loose
global ceiling so the intentionally expensive hash cannot be used as unlimited
CPU work. Those counters are process-local and contain no reusable credential.

- **The display stays open.** Every read the dashboard needs works without the
  key, so the wall display and everyone's phones are unaffected.
- Set, change, or remove it under **Manage → Settings**. Changing it requires
  the current key, and doing so signs out every other device.
- Also running Shed? Set Bask's key to the same Head Keeper code and the
  household has one key to remember. Rotating it in Shed does not rotate it
  here — update both.
- **Upgrading an existing install does not lock you out.** Installs with no key
  keep behaving exactly as before until you set one.

The key limits *changes*, not *viewing*. It is not a substitute for keeping the
port off the internet:

- **Don't expose it to the internet.** Don't port-forward `:8080` or put it on a public network.
- It binds to `0.0.0.0` so your wall display can reach it. Restrict it with a host firewall, an IoT VLAN, or by binding to a specific interface if you want tighter scoping.
- For remote access, use a **VPN** (e.g. WireGuard/Tailscale) or an authenticating reverse proxy — never the raw port.
- A reverse proxy may set `BASK_TRUSTED_PROXY_IP_HEADER` to one of the three
  allowlisted client-address headers in `.env.example`. Bask ignores forwarded
  address headers unless you explicitly choose one that your proxy overwrites.

What Bask does on its side:

- **No account, and no cloud unless you ask for one** — it never touches a Govee account. Bluetooth is receive-only: Bask listens to the advertisements your sensors already broadcast and never connects, pairs, or advertises itself. The optional Cielo and VeSync integrations are the only parts that reach the internet, and each stores its credentials in its own `0600` file inside the private data directory ([details](#levoit-classic-300s-room-humidifier-optional)). Your `config.json` (sensor IDs + enclosure names) is git-ignored.
- **Same-origin only** — the API sends no permissive CORS headers, so other websites can't read it or send it cross-origin writes.
- **Concurrent-edit safe** — every settings change is an atomic, owner-only
  file replacement guarded by a process-wide lock. The web app carries Bask's
  current revision with each change; if two phones edit the same snapshot, the
  stale one gets the latest setup and is asked to retry instead of silently
  overwriting the other person's work. Custom API clients should load
  `GET /api/manage-snapshot` and send its `revision` as `X-Bask-Revision` on
  setup-changing requests (`GET /api/config/revision` is also available).
  Successful writes return their exact new revision; missing and stale
  revisions return HTTP `428` and `409`, respectively.
- **XSS-safe rendering** — all user- and device-provided strings are HTML-escaped, including BLE advertisement names (so a crafted nearby device name can't inject script).
- **Validated input** — request payloads are length- and range-checked.
- **Container-isolated** — Bask uses separate read-only Docker services for the
  dashboard, radio parser, and D-Bus filter. Only the small proxy sees the host
  Bluetooth socket, through a method-level allowlist; the unprivileged parser
  receives only the filtered socket and persistent Bask data. All services
  drop Linux capabilities, disable privilege escalation, and apply memory/PID
  limits. The installer requests `sudo` only for Docker, Bluetooth, and host
  service setup.

## ⚠️ Husbandry disclaimer

The species ranges in `config.example.json` are **starting points compiled from public care resources, not veterinary advice.** Temperature and humidity needs vary by animal, age, and setup, and **sensor placement matters** (a probe at the basking spot reads hotter than the ambient air, which is usually what you want to alert on). **Verify everything against trusted sources and your own animals, and tune the ranges to your room.** Bask is a monitoring aid, not a substitute for proper research and care.

## Project structure

```
scanner/        unprivileged BLE parser — writes readings.db
  scanner.py      main loop: passive scan + batched DB writes
  govee.py        H5075 advertisement decoding
  db.py           shared SQLite layer
server/
  app.py          FastAPI: JSON API, range evaluation, serves the frontend
  alerts.py       durable, debounced phone-alert outbox and retry state machine
frontend/         vanilla HTML/CSS/JS dashboard (+ favicon, keep-alive)
config.example.json   copy to config.json
Dockerfile        portable multi-architecture Bask container
compose.yaml      isolated web, scanner, and filtered D-Bus proxy services
dbus-proxy-entrypoint.sh  exact passive-BlueZ method/signal allowlist
get-bask.sh       one-line installer and safe legacy migration
deploy/install.sh installs Docker/BlueZ/mDNS and starts the container
scripts/backup.sh private settings + SQLite + integration/alert-state backup
start.sh          run scanner + web server together (local/dev)
kiosk.sh          optional fullscreen browser launcher for a host-attached screen
docs/SETUP.md     complete beginner's guide (Pi OS → one-line install → first sensor)
```

## Bask + Shed = Haven

Bask can run by itself, or alongside Shed:

| | Project | What it watches |
|---|---|---|
| ☀️ | **Bask** *(this repo)* | The environment — live temperature & humidity on a wall display |
| 🐍 | **[Shed](https://github.com/jlyfshhh/shed)** | The care — feeding, weights, enclosure work, and history for terrestrial animals |
| 🌿 | **Haven** | The combined room view — enclosure status and today's care in one glance |

Haven is not a third database. It is the secure, read-only room dashboard that
appears when Bask and Shed are installed together. The apps remain separate, so
either can still work independently and each keeps its own portable data.

Choose Bask, Shed, or the combined Haven setup from the
**[Haven installer](https://github.com/jlyfshhh/animal-room)**.

## 🦗 Buy the animals some crickets

Bask is free and always will be, but if it saved one of your animals a rough night, or you just think it's neat, you can chip in a couple bucks toward the cricket fund.

<p align="center">
  <a href="https://ko-fi.com/jlyfshhh"><img height="40" alt="Buy the animals crickets on Ko-fi" src="https://img.shields.io/badge/Ko--fi-Buy%20the%20animals%20crickets-FF5E5B?style=for-the-badge&logo=ko-fi&logoColor=white"></a>
</p>

## License

MIT — see [LICENSE](LICENSE).

---

Built by **[jlyfshhh](https://github.com/jlyfshhh)**. I keep a room full of reptiles and amphibians — follow along on Instagram **[@thebioactivekeeper](https://instagram.com/thebioactivekeeper)** for the animals and bioactive builds behind this project. 🦎

> Built with the help of [Claude](https://www.anthropic.com/claude), Anthropic's AI assistant — from the Bluetooth decoding and the dashboard to this README. Reviewed, tested, and deployed by a human (me).
