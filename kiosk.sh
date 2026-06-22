#!/usr/bin/env bash
# Launch the dashboard full-screen on the attached touchscreen.
# Model-aware: Chromium on a Pi Zero 2 W (quad-core handles it); the lighter
# WPE/cog renderer on an original single-core Zero if it's installed.
#
# Wire this into autostart (e.g. ~/.config/wayfire.ini, ~/.xinitrc, or a
# systemd --user service) after the web service is up.
set -u
URL="http://localhost:8080"

# Wait for the web server to answer before opening the browser. cog does NOT
# auto-retry a failed load, and uvicorn's cold start on a Pi Zero W is ~30-40s,
# so we wait generously (up to 2 min) for it to come up before launching.
for _ in $(seq 1 120); do
  if curl -sf "$URL/api/dashboard" >/dev/null 2>&1; then break; fi
  sleep 1
done

# Blank the cursor and stop the screen blanking (best-effort; ignore if absent).
command -v unclutter >/dev/null && unclutter -idle 0 &
xset s off    2>/dev/null || true
xset -dpms    2>/dev/null || true
xset s noblank 2>/dev/null || true

if command -v cog >/dev/null 2>&1; then
  # labwc/Wayland on Raspberry Pi OS → cog's Wayland platform module is "wl".
  # The Pi Zero W GPU driver intermittently hands cog a NULL buffer to export,
  # crashing it with "on_export_wl_egl_image: assertion failed
  # (wpe_view_data.buffer)". Forcing the GL stack to software Mesa (llvmpipe)
  # always yields a valid buffer; disabling accelerated compositing keeps the
  # whole render path in software. Proven stable on this board for this UI.
  export LIBGL_ALWAYS_SOFTWARE=1
  export WEBKIT_DISABLE_COMPOSITING_MODE=1
  exec cog -P wl "$URL"
elif command -v chromium-browser >/dev/null 2>&1; then
  BROWSER=chromium-browser
elif command -v chromium >/dev/null 2>&1; then
  BROWSER=chromium
else
  echo "No supported browser found (install chromium-browser or cog)." >&2
  exit 1
fi

exec "$BROWSER" \
  --kiosk \
  --app="$URL" \
  --noerrdialogs \
  --disable-infobars \
  --disable-session-crashed-bubble \
  --disable-restore-session-state \
  --disable-pinch \
  --overscroll-history-navigation=0 \
  --check-for-update-interval=31536000
