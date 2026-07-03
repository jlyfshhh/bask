# Bask setup — the complete beginner's guide

Never touched a Raspberry Pi before? That's fine. This guide takes you from a box of parts to a working dashboard on your wall, step by step. No prior experience needed — **no terminal, no commands, no code.** Set aside about **20 minutes**.

![Flash → boot → it runs itself → open bask.local](setup-flow.svg)

> **The short version:** download the ready-made Bask image, write it to an SD card with a free app (typing in your Wi‑Fi as you do), plug it into the Pi, then open **http://bask.local:8080**. The rest of this page is just that — slowly, with nothing assumed.

---

## 1. What to buy

You need a small always-on computer (a Raspberry Pi) and an SD card for it. Here's a complete, no-guesswork list:

| Item | What it's for | Notes |
|---|---|---|
| **A Raspberry Pi** | The little computer that runs Bask | Any current model: a **Pi 4** or **Pi 3B+** is the easiest to buy; a **Zero 2 W** is the tiniest. All have built-in Wi‑Fi **and** Bluetooth. *(Avoid the old single-core **Pi Zero W** / Pi 1 — too slow.)* |
| **microSD card, 16 GB+** | Its hard drive | Any decent brand (SanDisk, Samsung). "Class 10 / A1" is plenty. |
| **A USB power supply** | Power | Whatever matches your Pi: **Pi 4** uses **USB‑C**; **Pi 3 / Zero 2 W** use **micro‑USB**. A good phone charger (5V, 2.5A+) works. |
| **A computer** | To set up the SD card once | Windows, Mac, or Linux — anything. You only need it for setup. |
| **Your Govee H5075 sensors** | The thermometers Bask reads | One or more. Fresh batteries help. |
| *(optional)* A small case | Keeps the Pi tidy | Nice to have, not required. |

> 💡 **Which Pi should I get?** If you're buying one, a **Pi 4 (2GB)** or **Pi 3B+** is the most foolproof and the easiest to find in stock. The **Zero 2 W** is great if you want the smallest, lowest-power option (but it's often out of stock — [rpilocator.com](https://rpilocator.com) tracks who has Pis available). They all run the exact same Bask setup, so just grab whichever you can get.

---

## 2. Flash the Bask card

"Flashing" just means writing Bask onto the SD card. A free official app does it all — and Bask comes as a **ready-made image**, so there's nothing to install afterwards.

1. **[Download the latest Bask image](https://github.com/jlyfshhh/bask/releases/latest)** — the file ending in **`.img.xz`** (about 600 MB). No need to unzip it.
2. On your computer, download **Raspberry Pi Imager** from **[raspberrypi.com/software](https://www.raspberrypi.com/software/)** and install it.
3. Put your microSD card into your computer (use a USB adapter if needed).
4. Open Raspberry Pi Imager and set the three buttons:
   - **Choose Device** → the Pi model you have (e.g. *Raspberry Pi 4* or *Raspberry Pi Zero 2 W*).
   - **Choose OS** → scroll to the bottom → **Use custom** → pick the **Bask `.img.xz`** you downloaded.
   - **Choose Storage** → your SD card. **Double‑check you picked the card and not your hard drive.**
5. Click **Next**. When it asks *"Would you like to apply OS customisation settings?"*, click **Edit Settings** and fill in just two things:
   - **Configure wireless LAN:** your Wi‑Fi network name and password, and your country.
   - **Set locale / time zone** to yours.

   That's all you need — the hostname is already `bask`, and there's no account to create. *(Leave "Set username and password" alone unless you're technical and want SSH access later.)*
6. **Save**, then **Yes** to apply the settings, then **Yes** to write. It copies and verifies — a few minutes. When it says you can remove the card, do so.

---

## 3. Plug it in

1. Put the SD card into the Pi.
2. Plug the **power** into the Pi's power port (labelled `PWR` — USB‑C on a Pi 4, micro‑USB on a Pi 3 or Zero 2 W).
3. Wait **2–3 minutes** for its first start‑up. There's no screen and no lights to watch — that's normal. It's quietly joining your Wi‑Fi and starting Bask. It will do this automatically every time it has power, forever.

---

## 4. Open the dashboard

1. On your **phone, tablet, or computer** (connected to the same Wi‑Fi), open a web browser and go to:

   **http://bask.local:8080**

2. You'll see the Bask dashboard. Tap **⚙ Manage → Sensors → Pair by proximity**, then hold a Govee sensor a few inches from the Pi. When it pops up, tap to assign it to an enclosure's warm or cool side. Repeat for each sensor.
3. Set up your **enclosures** and **species ranges** under **⚙ Manage** so Bask knows what "good" looks like.

**Nice extras once you're up:**

- 📱 **Make it an app:** on your phone or tablet, use the browser's **Add to Home Screen** — Bask gets its own icon and opens fullscreen like a native app.
- 📲 **Phone alerts:** in **⚙ Manage → Settings**, tap **Set up phone alerts** — install the free **ntfy** app and scan the QR code Bask shows you. Your phone then gets a ping whenever an enclosure needs attention. (~2 minutes, optional.)

That's it. Leave the Pi plugged in — it starts Bask automatically every time it powers on. For wall‑mounting and always‑on display ideas, see **[Displaying it](../README.md#displaying-it)** in the main README.

Running Herpstat thermostats too? See **[Herpstat thermostats](../README.md#herpstat-thermostats-optional)**.

---

## Alternative: install on an existing Raspberry Pi

Already have a Pi running Raspberry Pi OS (with SSH)? You don't need the image — connect to it and paste one line:

```bash
curl -fsSL https://raw.githubusercontent.com/jlyfshhh/bask/main/get-bask.sh | bash
```

It downloads Bask, installs what it needs, and sets it to start automatically on boot. Run the same line again any time to update.

---

## Troubleshooting

**`bask.local` won't load.**
Give the Pi a full 3 minutes on first boot, then try again. Still nothing? Your network may not support `.local` names — find the Pi's IP address instead:
- Open your Wi‑Fi router's admin page and look in its list of connected devices for **`bask`** — note its IP (looks like `192.168.1.42`).
- Then open `http://192.168.1.42:8080` (your number) instead.
- **Windows only:** `.local` names need Apple's *Bonjour* service. If you have iTunes installed you already have it; otherwise the IP‑address method above always works.

**It's still not on the network at all.**
Almost always a Wi‑Fi typo. Re‑flash the card (step 2) and re‑enter the Wi‑Fi name, password, and **country** in the **Edit Settings** screen — it only takes a few minutes and can't hurt anything.

**Dashboard loads but no sensors appear.**
Make sure the sensors have good batteries and are within a few feet of the Pi while pairing. Give the scanner a minute to hear them. The Govee Home app can confirm a sensor is alive.

**How do I update Bask later?**
Right on the dashboard: **⚙ Manage → Settings → Check for updates → Update now**. It takes about a minute and **never touches your settings or sensor pairings**. (While you're there, tap **💾 Download backup** occasionally — one file restores everything if an SD card ever dies.)

**How do I see what it's doing / read logs?** *(technical users, over SSH)*
`journalctl -u bask-scanner -f` (Bluetooth scanner) or `journalctl -u bask-web -f` (dashboard). Press `Ctrl+C` to stop watching.
