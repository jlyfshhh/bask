#!/usr/bin/env bash
# Scan tracked files for private identities without storing or printing those
# identities in this public repository. Configure values through either the
# PRIVACY_DENYLIST environment variable (newline- or comma-separated) or a
# local, git-ignored .privacy-denylist file (one value per line).
set -uo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

collect_terms() {
  if [[ -n "${PRIVACY_DENYLIST:-}" ]]; then
    # Strip comments before splitting on commas. A comment containing a comma
    # would otherwise be cut into fragments that no longer start with '#', and
    # the loop below would take them for terms — a stray "and" from a prose
    # comment matches most of the codebase and fails the build for nothing.
    # The file path never hit this because it does not comma-split.
    printf '%s\n' "$PRIVACY_DENYLIST" | sed 's/#.*$//' | tr ',' '\n'
  elif [[ -f .privacy-denylist ]]; then
    cat .privacy-denylist
    printf '\n'
  fi
}

terms=()
while IFS= read -r term; do
  term="$(printf '%s' "$term" | sed -E 's/^[[:space:]]+|[[:space:]]+$//g')"
  [[ -z "$term" || "$term" == \#* ]] && continue
  if ((${#term} < 3)); then
    echo "Skipping denylist entry shorter than 3 characters." >&2
    continue
  fi
  terms+=("$term")
done < <(collect_terms)

if ((${#terms[@]} == 0)); then
  if [[ -n "${PRIVACY_SCAN_STRICT:-}" && "${PRIVACY_SCAN_STRICT}" != "false" && "${PRIVACY_SCAN_STRICT}" != "0" ]]; then
    echo "privacy-scan: no denylist configured, and this run was expected to have one." >&2
    echo "Nothing was checked. Configure the PRIVACY_DENYLIST repository secret or a local .privacy-denylist." >&2
    exit 1
  fi
  if [[ -n "${GITHUB_ACTIONS:-}" ]]; then
    echo "::warning title=Privacy scan did nothing::No denylist configured, so no private identities were checked."
  fi
  echo "privacy-scan: no denylist configured; skipping."
  exit 0
fi

status=0
for term in "${terms[@]}"; do
  # Search the Git index rather than the whole checkout. Never include the
  # matched text in output because public CI logs must not disclose it.
  while IFS=: read -r file line _; do
    [[ -z "$file" ]] && continue
    echo "PRIVACY: $file:$line contains a denylisted identity (value withheld)."
    status=1
  done < <(git grep -IniwF -- "$term" -- . \
    ':(exclude)package-lock.json' \
    ':(exclude).privacy-denylist' 2>/dev/null)
done

if ((status != 0)); then
  echo >&2
  echo "Remove the identity above and replace it with a generic example." >&2
  exit 1
fi

echo "privacy-scan: ${#terms[@]} term(s) checked, no matches."
