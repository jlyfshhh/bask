#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

mkdir -p "$TMP/repo/scripts"
cp "$ROOT/scripts/privacy-scan.sh" "$TMP/repo/scripts/privacy-scan.sh"
cp "$ROOT/.gitignore" "$TMP/repo/.gitignore"
git -C "$TMP/repo" init -q
git -C "$TMP/repo" add scripts/privacy-scan.sh .gitignore

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

# Repository secrets are easiest to maintain as either comma- or newline-
# separated values. Both forms must remain supported in the same value.
comma_term="comma_identity_${RANDOM}_$$"
newline_term="newline_identity_${RANDOM}_$$"
printf '%s\n%s\n' "$comma_term" "$newline_term" >"$TMP/repo/env-forms.txt"
git -C "$TMP/repo" add env-forms.txt
set +e
output="$(cd "$TMP/repo" && PRIVACY_DENYLIST="$comma_term,
$newline_term" bash scripts/privacy-scan.sh 2>&1)"
status=$?
set -e
[[ $status -ne 0 ]] || { echo "comma/newline denylist unexpectedly passed" >&2; exit 1; }
[[ "$output" == *"env-forms.txt:1"* && "$output" == *"env-forms.txt:2"* ]] || { echo "comma/newline denylist did not check both terms" >&2; exit 1; }
[[ "$output" != *"$comma_term"* && "$output" != *"$newline_term"* ]] || { echo "comma/newline denylist leaked a value" >&2; exit 1; }

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

# The same denylist as a local file must be ignored by Git and reach the same
# verdict as the environment form.
printf '%s\n' "$commented_denylist" >"$TMP/repo/.privacy-denylist"
git -C "$TMP/repo" check-ignore -q .privacy-denylist || { echo ".privacy-denylist is not ignored" >&2; exit 1; }
set +e
file_output="$(cd "$TMP/repo" && bash scripts/privacy-scan.sh 2>&1)"
file_status=$?
set -e
[[ $file_status -eq $status ]] || { echo "file and environment denylists disagreed" >&2; exit 1; }
[[ "$file_output" == *"fixture.txt:1"* ]] || { echo "local denylist stopped matching the real term" >&2; exit 1; }
[[ "$file_output" != *"$private_term"* ]] || { echo "local denylist leaked the denylisted value" >&2; exit 1; }

echo "privacy scan behavior: ok"
