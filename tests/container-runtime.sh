#!/usr/bin/env bash
# Cheap runtime proof for the image's non-root/read-only application boundary.
set -euo pipefail

image="${1:-bask:test}"

[[ "$(docker image inspect "$image" --format '{{.Config.User}}')" == "bask:bask" ]]

docker run --rm \
  --read-only \
  --user 10001:10001 \
  --cap-drop ALL \
  --security-opt no-new-privileges \
  --network none \
  --tmpfs /tmp:rw,nosuid,nodev,noexec,size=8m,mode=0700,uid=10001,gid=10001 \
  --tmpfs /data:rw,nosuid,nodev,noexec,size=8m,mode=0700,uid=10001,gid=10001 \
  --entrypoint sh \
  "$image" -ec '
    test "$(id -u)" = 10001
    test "$(id -g)" = 10001
    command -v xdg-dbus-proxy >/dev/null
    if python -m pip --version >/dev/null 2>&1; then
      echo "The runtime image still contains pip" >&2
      exit 1
    fi
    touch /tmp/runtime-write-probe /data/runtime-write-probe
    if touch /app/rootfs-must-stay-read-only 2>/dev/null; then
      echo "Image root filesystem was writable" >&2
      exit 1
    fi
  '

echo "Container runtime boundary test passed."
