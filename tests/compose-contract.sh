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

# The installer's boundary audit compares an exact capability set per service.
# Confirm the real shipped Compose, resolved by real Docker Compose, declares
# the sets that audit expects: the scanner adds CHOWN and DAC_OVERRIDE, the
# one-shot init adds CHOWN and DAC_READ_SEARCH (and no more), and the init
# service exists at all. A drift here is exactly the F03/F06 break where the
# installer and the shipped Compose disagreed and every real install rolled back.
#
# The assertions live in a standalone helper rather than a `python3 - <<EOF`
# heredoc: `python3 -` reads its program from stdin, which would collide with the
# resolved JSON we pipe in (json.load(sys.stdin) would hit EOF). With the program
# in a file, stdin is free to carry the JSON — and the same helper can be fed a
# crafted graph to prove the check actually fails, with or without Docker.
capcheck="$work/capcheck.py"
cat > "$capcheck" <<'PY'
import json, sys
d = json.load(sys.stdin)
svc = d["services"]
missing = {"bask", "bask-scanner", "bask-init"} - set(svc)
assert not missing, "the shipped compose is missing services: %s" % sorted(missing)
def caps(name):
    return {c.replace("CAP_", "") for c in (svc[name].get("cap_add") or [])}
assert caps("bask-scanner") == {"CHOWN", "DAC_OVERRIDE"}, \
    "scanner cap_add is %s, not the audited {CHOWN, DAC_OVERRIDE}" % sorted(caps("bask-scanner"))
assert caps("bask-init") == {"CHOWN", "DAC_READ_SEARCH"}, \
    "init cap_add is %s, not the audited {CHOWN, DAC_READ_SEARCH}" % sorted(caps("bask-init"))
assert (svc["bask"].get("cap_add") or []) == [], "the web service must add no capabilities"
for name in ("bask", "bask-scanner", "bask-init"):
    assert "ALL" in (svc[name].get("cap_drop") or []), "%s must drop ALL capabilities" % name
assert svc["bask-init"].get("network_mode") == "none", "the init service must have no network"
PY

# Negative case, independent of Docker: a graph with the scanner missing CHOWN
# must be rejected, or the real-Compose assertion below proves nothing.
if printf '%s' '{"services":{"bask":{"cap_drop":["ALL"]},"bask-scanner":{"cap_add":["DAC_OVERRIDE"],"cap_drop":["ALL"]},"bask-init":{"cap_add":["CHOWN","DAC_READ_SEARCH"],"cap_drop":["ALL"],"network_mode":"none"}}}' \
     | python3 "$capcheck" 2>/dev/null; then
  echo "The capability check accepted a scanner missing CHOWN, so it could never catch drift." >&2
  exit 1
fi
echo "  the capability check rejects a malformed capability set"

if resolved_json="$(cd "$stage" && docker compose --project-directory "$stage" \
      --env-file "$stage/.env" -f "$stage/compose.yaml" config --format json 2>/dev/null)" \
   && [[ -n "$resolved_json" ]]; then
  if ! printf '%s' "$resolved_json" | python3 "$capcheck"; then
    echo "The shipped compose.yaml does not declare the capability sets the installer audits." >&2
    exit 1
  fi
  echo "  the shipped compose declares the audited capability sets for all three services"
else
  echo "  resolved-capability check skipped — compose config --format json unavailable"
fi

echo "Compose contract tests passed."
