# Prebuilt image build

This directory builds the **ready-to-flash Bask image** — Raspberry Pi OS Lite
(64-bit) with Bask preinstalled and set to start on boot. End users never touch
this; they just flash the finished `.img` (see [../docs/SETUP.md](../docs/SETUP.md)).

## How it's built

A GitHub Actions workflow ([`.github/workflows/build-image.yml`](../.github/workflows/build-image.yml))
runs [`usimd/pi-gen-action`](https://github.com/usimd/pi-gen-action), which wraps
the official [pi-gen](https://github.com/RPi-Distro/pi-gen) tool. It builds the
standard Raspberry Pi OS Lite stages, then applies the custom **`stage-bask`**
stage in this directory.

`stage-bask/` contains:

| File | Role |
|---|---|
| `00-packages` | apt packages baked into the image (git, python venv, avahi, bluez) |
| `01-run-chroot.sh` | runs in the image: clones Bask, builds the venv, installs + enables the `bask-scanner` / `bask-web` services, enables BlueZ passive scanning and mDNS |
| `prerun.sh` | standard pi-gen stage bootstrap |
| `EXPORT_IMAGE` | marks this as the stage pi-gen exports the final image from |

The result works on any 64-bit Pi (Pi 3/3B+, 4, 400, 5, Zero 2 W).

## Running a build

- **On demand:** Actions tab → *Build SD-card image* → **Run workflow**. The
  image is uploaded as a workflow artifact.
- **For a release:** push a tag like `v1.0.0`. The image is built from that tag
  and attached to the GitHub Release automatically.

## Before publishing a release to users

CI proves the image *builds*. Always do a one-time **hardware smoke test** of a
fresh build — flash it, boot a real Pi, confirm `http://bask.local:8080` loads
and sensors pair — before announcing a release.
