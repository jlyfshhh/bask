#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

mkdir -p "$TMP/repo/scripts"
cp "$ROOT/scripts/privacy-scan.sh" "$TMP/repo/scripts/privacy-scan.sh"
git -C "$TMP/repo" init -q
git -C "$TMP/repo" add scripts/privacy-scan.sh

unset PRIVACY_DENYLIST PRIVACY_SCAN_STRICT GITHUB_ACTIONS
output="$(cd "$TMP/repo" && bash scripts/privacy-scan.sh 2>&1)"
[[ "$output" == *"no denylist configured"* ]] || { echo "non-strict scan did not explain its skip" >&2; exit 1; }

if (cd "$TMP/repo" && PRIVACY_SCAN_STRICT=1 bash scripts/privacy-scan.sh >/dev/null 2>&1); then
  echo "strict scan passed without a denylist" >&2
  exit 1
fi

private_term="private_identity_${RANDOM}_$$"
printf 'owner=%s\n' "$private_term" >"$TMP/repo/fixture.txt"
git -C "$TMP/repo" add fixture.txt
set +e
output="$(cd "$TMP/repo" && PRIVACY_DENYLIST="$private_term" bash scripts/privacy-scan.sh 2>&1)"
status=$?
set -e
[[ $status -ne 0 ]] || { echo "matching scan unexpectedly passed" >&2; exit 1; }
[[ "$output" == *"fixture.txt:1"* ]] || { echo "matching scan omitted the source location" >&2; exit 1; }
[[ "$output" != *"$private_term"* ]] || { echo "matching scan leaked the denylisted value" >&2; exit 1; }

# A denylist passed through the environment is comma-split, so a comment
# containing a comma used to be cut into fragments that no longer began with
# '#'. One of those fragments was the bare word "and", which matched most of
# the codebase and failed the build while no real identity was present. The
# file path never had this because it does not comma-split, so the two inputs
# disagreed about the same denylist.
printf 'the quick brown fox and the lazy dog\n' >"$TMP/repo/prose.txt"
git -C "$TMP/repo" add prose.txt
commented_denylist="# real names, household names, and
$private_term"
set +e
output="$(cd "$TMP/repo" && PRIVACY_DENYLIST="$commented_denylist" bash scripts/privacy-scan.sh 2>&1)"
status=$?
set -e
[[ "$output" != *"prose.txt"* ]] || { echo "a comma in a comment became a search term" >&2; exit 1; }
[[ $status -ne 0 ]] || { echo "commented denylist lost its real term" >&2; exit 1; }
[[ "$output" == *"fixture.txt:1"* ]] || { echo "commented denylist stopped matching the real term" >&2; exit 1; }

# The same denylist as a file must reach the same verdict as the environment.
printf '%s\n' "$commented_denylist" >"$TMP/repo/.privacy-denylist"
set +e
file_output="$(cd "$TMP/repo" && bash scripts/privacy-scan.sh 2>&1)"
file_status=$?
set -e
[[ $file_status -eq $status ]] || { echo "file and environment denylists disagreed" >&2; exit 1; }

echo "privacy scan behavior: ok"
