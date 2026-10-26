#!/usr/bin/env bash
# Bask one-line installer for 64-bit Raspberry Pi OS / Debian.
set -Eeuo pipefail
umask 077

repo="${BASK_REPO:-https://github.com/jlyfshhh/bask.git}"
branch="${BASK_BRANCH:-main}"
requested_install_dir="${BASK_INSTALL_DIR:-${BASK_DIR:-$HOME/bask}}"
stage_root=""
candidate_dir=""
worktree_added=false
retained_reinstall_active=false
retained_git_placed=false
retained_success=false
retained_tracked_manifest=""

# A directory that exists but is not a Git checkout is normally refused. The one
# supported exception is a retained-data reinstall: a normal animal-room
# uninstall keeps data/, backups/, and .env but removes .git, and leaves a marker
# saying exactly that. These name the marker and the exact first line it must
# carry to be trusted.
RETAINED_DATA_MARKER=.animal-room-retained-data
RETAINED_DATA_MARKER_LINE="animal-room retained-data v1"

say() { printf '\n\033[1;38;5;208m==>\033[0m %s\n' "$1"; }
die() { printf '\n\033[1;31mError:\033[0m %s\n' "$1" >&2; exit 1; }

reject_unsafe_path_text() {
  local label="$1" value="$2"
  case "$value" in
    "") die "$label cannot be blank." ;;
    -*) die "$label cannot begin with a dash." ;;
    *$'\n'*|*$'\r'*) die "$label cannot contain line breaks." ;;
    *\'*|*\"*) die "$label cannot contain quote characters; Docker Compose and this installer do not parse them identically." ;;
    *'$'*|*'`'*|*\\*|*:*|*'#'*) die "$label contains characters that Docker Compose and this installer would interpret differently." ;;
  esac
}

canonical_target_without_creating() {
  local candidate="$1" suffix="" leaf
  case "$candidate" in /*) ;; *) candidate="$PWD/$candidate" ;; esac
  [[ "$candidate" == / ]] || candidate="${candidate%/}"
  while [[ ! -e "$candidate" ]]; do
    leaf="${candidate##*/}"
    [[ -n "$leaf" ]] || return 1
    suffix="/$leaf$suffix"
    candidate="${candidate%/*}"
    [[ -n "$candidate" ]] || candidate=/
  done
  [[ -d "$candidate" ]] || return 1
  printf '%s%s\n' "$(cd -- "$candidate" && pwd -P)" "$suffix"
}

install_path_is_protected() {
  local path="$1"
  case "$path" in
    /|/bin|/bin/*|/boot|/boot/*|/dev|/dev/*|/etc|/etc/*|/lib|/lib/*|/lib64|/lib64/*|/private/etc|/private/etc/*|/private/tmp|/private/tmp/*|/private/var|/private/var/*|/proc|/proc/*|/root|/root/*|/run|/run/*|/sbin|/sbin/*|/sys|/sys/*|/tmp|/tmp/*|/usr|/usr/*|/var|/var/*|/home|/mnt|/opt|/srv|"${HOME:-/nonexistent}") return 0 ;;
  esac
  return 1
}

cleanup_stage() {
  if [[ "$worktree_added" == true && -n "$candidate_dir" ]]; then
    git -C "$install_dir" worktree remove --force "$candidate_dir" >/dev/null 2>&1 || true
  fi
  if [[ "$retained_reinstall_active" == true && "$retained_success" != true &&
        "$retained_git_placed" == true ]]; then
    # A retained-data reinstall was interrupted after the repository metadata was
    # placed. Remove only what the reinstall added — the .git directory and the
    # tracked source files — so the keeper's data/, backups/, and .env are left
    # exactly as they were and the marker stays in place for a clean retry.
    # data/, backups/, and .env are git-ignored and never appear in the manifest.
    rm -rf -- "$install_dir/.git"
    if [[ -n "$retained_tracked_manifest" && -s "$retained_tracked_manifest" ]]; then
      while IFS= read -r -d '' tracked; do
        [[ -n "$tracked" ]] || continue
        rm -f -- "$install_dir/$tracked"
      done < "$retained_tracked_manifest"
    fi
  fi
  [[ -z "$stage_root" || ! -d "$stage_root" ]] || rm -rf -- "$stage_root"
}
trap cleanup_stage EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

if [[ "${EUID:-$(id -u)}" -eq 0 ]]; then
  die "Run this as your normal user, not with sudo."
fi
command -v sudo >/dev/null 2>&1 || die "sudo is required but not installed."

cat <<'BANNER'

  ☀  Bask — at-a-glance temperature & humidity for your animal room
  -----------------------------------------------------------------
  This installs Bask with Docker and preserves data across updates.

BANNER

reject_unsafe_path_text BASK_INSTALL_DIR "$requested_install_dir"
case "$requested_install_dir" in
  ..|../*|*/..|*/../*) die "BASK_INSTALL_DIR cannot contain a '..' path component." ;;
esac

# Resolve through the nearest existing parent before creating anything. A bad
# environment value must not even manufacture an empty directory under an OS
# tree such as /etc or /tmp.
install_dir="$(canonical_target_without_creating "$requested_install_dir")" ||
  die "BASK_INSTALL_DIR has no usable parent directory."
install_name="$(basename -- "$install_dir")"
case "$install_name" in ""|.|..) die "BASK_INSTALL_DIR must name a dedicated application directory." ;; esac
install_path_is_protected "$install_dir" &&
  die "BASK_INSTALL_DIR must be a dedicated application directory, not $install_dir."
install_parent="$(dirname -- "$install_dir")"
mkdir -p -- "$install_parent"
install_parent="$(cd -- "$install_parent" && pwd -P)"
install_dir="$install_parent/$install_name"
install_path_is_protected "$install_dir" &&
  die "BASK_INSTALL_DIR resolves into a protected operating-system directory: $install_dir."

if ! command -v git >/dev/null 2>&1; then
  say "Installing git"
  sudo apt-get update -qq
  sudo apt-get install -y -qq git
fi
git check-ref-format --branch "$branch" >/dev/null 2>&1 ||
  die "BASK_BRANCH is not a safe Git branch name: $branch"

validate_candidate() {
  local candidate="$1" required
  for required in compose.yaml .env.example config.example.json \
                  deploy/install.sh scripts/backup.sh docker-entrypoint.sh \
                  get-bask.sh; do
    [[ -f "$candidate/$required" && ! -L "$candidate/$required" ]] ||
      die "The candidate release is missing a regular $required file."
  done
  bash -n "$candidate/get-bask.sh" "$candidate/deploy/install.sh" \
    "$candidate/scripts/backup.sh" "$candidate/docker-entrypoint.sh" ||
    die "The downloaded Bask shell scripts did not pass syntax validation."
}

# True only for a directory a normal uninstall left behind: the marker's first
# line matches EXACTLY and the artifacts it implies are really present as regular
# paths. A bare marker, a wrong version line, or arbitrary unrelated files all
# return false, so the ordinary refusal still applies to anything else.
retained_data_marker_valid() {
  local dir="$1" marker="$1/$RETAINED_DATA_MARKER" first=""
  [[ -f "$marker" && ! -L "$marker" ]] || return 1
  IFS= read -r first < "$marker" || true
  first="${first%$'\r'}"
  [[ "$first" == "$RETAINED_DATA_MARKER_LINE" ]] || return 1
  # Trust the marker only alongside the preserved records it promises — never
  # arbitrary other files, and never through a symlink.
  [[ -d "$dir/data" && ! -L "$dir/data" ]] || return 1
  [[ -f "$dir/.env" && ! -L "$dir/.env" ]] || return 1
  return 0
}

# Re-establish a Git checkout inside a directory a normal uninstall left behind,
# preserving data/, backups/, and .env. Everything risky happens in a scratch
# clone first; only after it validates is the repository metadata moved into
# place and the tracked tree materialised over it. git never touches ignored
# runtime data, so the records are left untouched. A failure at any point before
# success leaves the marker and data intact for a clean retry (see cleanup_stage).
retained_data_reinstall() {
  local dir="$1"
  say "Re-establishing the Bask checkout in place, preserving your records"
  retained_reinstall_active=true
  stage_root="$(mktemp -d "$install_parent/.bask-reinstall.XXXXXX")"
  candidate_dir="$stage_root/candidate"
  git clone --quiet --depth 1 --branch "$branch" "$repo" "$candidate_dir"
  validate_candidate "$candidate_dir"
  target_commit="$(git -C "$candidate_dir" rev-parse --verify HEAD)"
  # Capture the tracked paths before the clone body is spent, so an interrupted
  # reinstall removes exactly what it added and nothing that was preserved.
  retained_tracked_manifest="$stage_root/tracked"
  git -C "$candidate_dir" ls-files -z > "$retained_tracked_manifest"
  mv -- "$candidate_dir/.git" "$dir/.git"
  retained_git_placed=true
  candidate_dir=""
  git -C "$dir" checkout -f "$branch" >/dev/null
  git -C "$dir" reset --hard "$target_commit" >/dev/null
  # The directory is a valid checkout again. Drop the marker so a future run
  # takes the ordinary update path, and mark success so a later deploy failure
  # keeps this checkout and its data rather than tearing them down.
  rm -f -- "$dir/$RETAINED_DATA_MARKER"
  retained_success=true
  candidate_dir="$dir"
}

existing=false
previous_ref=""
target_commit=""

if [[ -d "$install_dir/.git" ]]; then
  existing=true
  install_dir="$(cd -- "$install_dir" && pwd -P)"
  install_path_is_protected "$install_dir" &&
    die "The existing Bask checkout resolves to an unsafe path: $install_dir."
  [[ -z "$(git -C "$install_dir" status --porcelain)" ]] ||
    die "The Bask checkout has modified or untracked source files. Save or remove them before updating. Ignored runtime data is not affected."

  say "Staging and validating the update for $install_dir"
  git -C "$install_dir" fetch --quiet origin "$branch"
  target_commit="$(git -C "$install_dir" rev-parse --verify FETCH_HEAD)"
  previous_head="$(git -C "$install_dir" rev-parse --verify HEAD)"
  previous_ref="$(git -C "$install_dir" symbolic-ref -q --short HEAD || true)"

  # A depth-one first install can lack the ancestry needed to prove this is a
  # fast-forward. Deepen only when needed; never replace local history.
  if ! git -C "$install_dir" merge-base --is-ancestor "$previous_head" "$target_commit"; then
    if [[ "$(git -C "$install_dir" rev-parse --is-shallow-repository 2>/dev/null || true)" == true ]]; then
      git -C "$install_dir" fetch --quiet --unshallow origin "$branch"
      target_commit="$(git -C "$install_dir" rev-parse --verify FETCH_HEAD)"
    fi
  fi
  git -C "$install_dir" merge-base --is-ancestor "$previous_head" "$target_commit" ||
    die "The requested update is not a fast-forward from the installed Bask revision."

  stage_root="$(mktemp -d "$install_parent/.bask-update.XXXXXX")"
  candidate_dir="$stage_root/candidate"
  git -C "$install_dir" worktree add --quiet --detach "$candidate_dir" "$target_commit"
  worktree_added=true
  validate_candidate "$candidate_dir"
elif [[ -e "$install_dir" ]]; then
  # The directory exists but is not a Git checkout. A normal animal-room
  # uninstall produces exactly this: data/, backups/, and .env kept, .git
  # removed, with a retained-data marker. Honour an explicit, validated marker by
  # rebuilding the checkout in place; refuse anything else as before.
  install_dir="$(cd -- "$install_dir" && pwd -P)"
  install_path_is_protected "$install_dir" &&
    die "The existing Bask directory resolves to an unsafe path: $install_dir."
  if retained_data_marker_valid "$install_dir"; then
    retained_data_reinstall "$install_dir"
  else
    die "$install_dir exists but is not a Bask checkout. Move it or set BASK_INSTALL_DIR."
  fi
else
  say "Downloading and validating Bask for $install_dir"
  stage_root="$(mktemp -d "$install_parent/.bask-install.XXXXXX")"
  candidate_dir="$stage_root/candidate"
  git clone --quiet --depth 1 --branch "$branch" "$repo" "$candidate_dir"
  validate_candidate "$candidate_dir"
  target_commit="$(git -C "$candidate_dir" rev-parse --verify HEAD)"
  mv -- "$candidate_dir" "$install_dir"
  candidate_dir="$install_dir"
fi

say "Preparing Docker, Bluetooth, and persistent storage"
if ! sudo bash "$candidate_dir/deploy/install.sh" "$install_dir" "$(id -un)" "$candidate_dir"; then
  if [[ "$existing" == true ]]; then
    die "The Bask update failed. The previous checkout, service, settings, and data were retained."
  fi
  die "Bask could not finish installing. Its data directory was retained so the installer can be run again."
fi

if [[ "$existing" == true ]]; then
  # Runtime installation succeeded from the already validated worktree. Only
  # now advance the working checkout, so a failed deployment never needs a
  # destructive Git reset.
  if [[ -n "$previous_ref" ]]; then
    git -C "$install_dir" merge --ff-only "$target_commit" >/dev/null
  else
    git -C "$install_dir" checkout -B "$branch" "$target_commit" >/dev/null
  fi
fi
