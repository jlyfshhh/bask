#!/usr/bin/env bash
# Real Docker Compose, exercising the one thing the installer suite cannot.
#
# tests/installer.sh runs against a fake `docker` that echoes what it is asked
# and agrees with everything. That is the right trade for testing rollback and
# ordering, but it means no test ever observed how Compose actually resolves a
# file — and a first-install bug lived behind that blind spot: compose.yaml
# declares `env_file: .env`, Compose resolves it against --project-directory
# rather than against the compose file, and the installer pointed that at an
# install root which has no .env until validation has already passed.
#
# So this asks the real thing, with the real compose.yaml, against a genuinely
# empty install directory — the exact shape of a keeper's first install.
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if ! docker compose version >/dev/null 2>&1; then
  echo "Compose contract tests skipped — no working docker compose on this host." >&2
  exit 0
fi

work="$(mktemp -d "${TMPDIR:-/tmp}/bask-compose-contract.XXXXXX")"
trap 'rm -rf -- "$work"' EXIT
stage="$work/stage"; install_dir="$work/install"
mkdir -p "$stage" "$install_dir"

cp "$root/compose.yaml" "$stage/compose.yaml"
cp "$root/.env.example" "$stage/.env"
# The installer replaces every published placeholder before it validates.
sed -i.bak 's/replace-with-[a-z-]*/contract-test-value-not-a-real-secret/g' "$stage/.env"
rm -f "$stage/.env.bak"

# A first install: the install root exists and is empty. This is the case that
# broke, and it must pass.
if ! (cd "$stage" && docker compose --project-directory "$stage" \
        --env-file "$stage/.env" -f "$stage/compose.yaml" config --quiet); then
  echo "The shipped compose.yaml does not validate against an empty install directory." >&2
  echo "This is the first-install failure keepers hit; see the comment at the top." >&2
  exit 1
fi
echo "  the shipped compose.yaml validates on a first install"

# Validation has to be able to fail, or the check above proves nothing.
printf 'services:\n  broken:\n    image: alpine\n    ports:\n      - "::::"\n' > "$stage/broken.yaml"
if (cd "$stage" && docker compose --project-directory "$stage" \
      --env-file "$stage/.env" -f "$stage/broken.yaml" config --quiet) 2>/dev/null; then
  echo "Malformed Compose was accepted, so this check would never catch anything." >&2
  exit 1
fi
echo "  malformed Compose is still rejected"

# Pin the fix in the installer itself. The behavioural check above passes for a
# copy of the invocation; this asserts the shipped script really does anchor
# validation at the staged bundle rather than at the install root.
if ! grep -q 'docker compose --project-directory "\$runtime"' "$root/deploy/install.sh"; then
  echo "deploy/install.sh no longer validates with the staging directory as its project." >&2
  echo "Compose resolves env_file against --project-directory; the install root has no .env yet." >&2
  exit 1
fi
echo "  the installer still validates against its staged bundle"

# The security audit compares capability names against docker inspect output.
# Docker 29 renders them as CAP_DAC_OVERRIDE where older daemons said
# DAC_OVERRIDE, and comparing the raw string made the audit impossible to pass
# on a current daemon — every fresh install failed after the containers had
# already come up correctly. Check the normaliser against what this host's
# daemon actually reports, rather than against a string in a comment.
probe=""
if docker image inspect alpine:latest >/dev/null 2>&1 || docker pull -q alpine:latest >/dev/null 2>&1; then
  probe="$(docker create --cap-add DAC_OVERRIDE alpine:latest true 2>/dev/null || true)"
fi
if [[ -n "$probe" ]]; then
  capability_report="$(docker inspect --format '{{json .HostConfig.CapAdd}}' "$probe" 2>/dev/null || true)"
  docker rm -f "$probe" >/dev/null 2>&1 || true
  if [[ "${capability_report//CAP_/}" != '["DAC_OVERRIDE"]' ]]; then
    echo "This daemon reports capabilities as $capability_report, which the installer's" >&2
    echo "audit does not normalise to the bare name it compares against." >&2
    exit 1
  fi
  if ! grep -q 'strip_cap_prefix' "$root/deploy/install.sh"; then
    echo "deploy/install.sh no longer normalises capability names before auditing them." >&2
    exit 1
  fi
  echo "  capability names normalise for this daemon ($capability_report)"
else
  echo "  capability check skipped — no probe image available"
fi

echo "Compose contract tests passed."
