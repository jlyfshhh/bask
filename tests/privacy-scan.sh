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

echo "privacy scan behavior: ok"
