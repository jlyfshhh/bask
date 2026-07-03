#!/usr/bin/env bash
# ============================================================================
#  Bask one-line installer.
#
#  On a fresh Raspberry Pi, just run:
#
#      curl -fsSL https://raw.githubusercontent.com/jlyfshhh/bask/main/get-bask.sh | bash
#
#  It downloads Bask, installs everything it needs, and starts it on boot.
#  New to all this? Follow the step-by-step guide: docs/SETUP.md
# ============================================================================
set -euo pipefail

REPO="https://github.com/jlyfshhh/bask.git"
BRANCH="${BASK_BRANCH:-main}"
BASK_DIR="${BASK_DIR:-$HOME/bask}"

say()  { printf '\n\033[1;38;5;208m==>\033[0m %s\n' "$1"; }
die()  { printf '\n\033[1;31mError:\033[0m %s\n' "$1" >&2; exit 1; }

# Don't run the bootstrap as root — we want the project in a normal user's home.
# (The installer it calls will ask for sudo on its own.)
if [[ "${EUID:-$(id -u)}" -eq 0 ]]; then
  die "Run this as your normal user, not root or sudo. Just: curl ... | bash"
fi
command -v sudo >/dev/null 2>&1 || die "sudo is required but not installed."

cat <<'BANNER'

  ☀  Bask — at-a-glance temperature & humidity for your animal room
  ----------------------------------------------------------------
  This will install Bask and set it to start automatically on boot.

BANNER

# ── 1. Make sure git is available ───────────────────────────────────────────
if ! command -v git >/dev/null 2>&1; then
  say "Installing git"
  sudo apt-get update -qq
  sudo apt-get install -y -qq git >/dev/null
fi

# ── 2. Download (or update) Bask ────────────────────────────────────────────
if [[ -d "$BASK_DIR/.git" ]]; then
  say "Updating existing install in $BASK_DIR"
  if git -C "$BASK_DIR" symbolic-ref -q HEAD >/dev/null; then
    # On a branch (one-liner install) — fast-forward it.
    git -C "$BASK_DIR" pull --ff-only
  else
    # Detached HEAD — the prebuilt image is cloned at a release tag, where
    # `git pull` silently no-ops. Jump to the newest release tag instead.
    say "Image install detected — moving to the newest release"
    git -C "$BASK_DIR" fetch --tags --quiet origin
    LATEST="$(git -C "$BASK_DIR" -c versionsort.suffix=- tag --list 'v*' --sort=-v:refname | head -1)"
    [[ -n "$LATEST" ]] || die "No release tags found in the repository."
    git -C "$BASK_DIR" checkout --quiet "$LATEST"
    say "Now on Bask $LATEST"
  fi
else
  [[ -e "$BASK_DIR" ]] && die "$BASK_DIR already exists but isn't a Bask checkout. Move it aside or set BASK_DIR=..."
  say "Downloading Bask into $BASK_DIR"
  git clone --depth 1 --branch "$BRANCH" "$REPO" "$BASK_DIR"
fi

# ── 3. Hand off to the full installer (needs root) ──────────────────────────
say "Installing system + Python dependencies and services (you may be asked for your password)"
sudo bash "$BASK_DIR/deploy/install.sh"
