#!/usr/bin/env bash
# Bask one-line installer for 64-bit Raspberry Pi OS / Debian.
set -euo pipefail

repo="https://github.com/jlyfshhh/bask.git"
branch="${BASK_BRANCH:-main}"
install_dir="${BASK_INSTALL_DIR:-${BASK_DIR:-$HOME/bask}}"

say() { printf '\n\033[1;38;5;208m==>\033[0m %s\n' "$1"; }
die() { printf '\n\033[1;31mError:\033[0m %s\n' "$1" >&2; exit 1; }

if [[ "${EUID:-$(id -u)}" -eq 0 ]]; then
  die "Run this as your normal user, not with sudo."
fi
command -v sudo >/dev/null 2>&1 || die "sudo is required but not installed."

cat <<'BANNER'

  ☀  Bask — at-a-glance temperature & humidity for your animal room
  -----------------------------------------------------------------
  This installs Bask with Docker and preserves data across updates.

BANNER

if ! command -v git >/dev/null 2>&1; then
  say "Installing git"
  sudo apt-get update -qq
  sudo apt-get install -y -qq git
fi

if [[ -d "$install_dir/.git" ]]; then
  say "Updating existing install in $install_dir"
  if git -C "$install_dir" symbolic-ref -q HEAD >/dev/null; then
    git -C "$install_dir" pull --ff-only
  else
    # Ready-made images are checked out at a release tag. Move them onto the
    # requested branch before the one-time Docker migration.
    git -C "$install_dir" fetch --quiet origin "$branch"
    git -C "$install_dir" checkout -B "$branch" "origin/$branch"
  fi
else
  [[ ! -e "$install_dir" ]] ||
    die "$install_dir exists but is not a Bask checkout. Move it or set BASK_INSTALL_DIR."
  say "Downloading Bask into $install_dir"
  git clone --depth 1 --branch "$branch" "$repo" "$install_dir"
fi

say "Preparing Docker, Bluetooth, and persistent storage"
sudo bash "$install_dir/deploy/install.sh" "$install_dir" "$(id -un)"
