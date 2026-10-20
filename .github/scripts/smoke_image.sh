#!/usr/bin/env bash
#
# Start the app image with no database and check that it is healthy.
#
#   .github/scripts/smoke_image.sh IMAGE [PLATFORM]
#
#   IMAGE      a local tag (gym-tracker:ci) or a pushed reference
#              (ghcr.io/owner/gym-tracker@sha256:...)
#   PLATFORM   optional, e.g. linux/arm64. A foreign platform needs QEMU
#              (docker/setup-qemu-action) on the host.
#
# /healthz does not touch Postgres, so the container must become healthy with
# no DATABASE_URL at all. Checks, in order:
#   1. the Dockerfile HEALTHCHECK reports "healthy" (the same probe compose uses)
#   2. the container runs on the requested platform
#   3. GET /healthz through the published port returns {"status": "ok"}
#   4. the process runs as the unprivileged appuser (uid 10001), not root
#   5. .dockerignore worked: no .env, tests/, .git/ or .github/ under /app
#   6. pip check passes and every compiled dependency imports
#
# Exit status is 0 only if all of them pass. On failure the container's logs
# are printed. The container is always removed.
#
# SMOKE_TIMEOUT (seconds, default 120) bounds the wait for "healthy".
# SMOKE_RUN_ARGS adds `docker run` options, split on spaces. Under QEMU the app
# starts several times slower than natively, so CI uses it to give the
# emulated run a longer health-check timeout and start period, e.g.
#   SMOKE_RUN_ARGS="--health-timeout=30s --health-start-period=240s"

set -euo pipefail

image="${1:?usage: smoke_image.sh IMAGE [PLATFORM]}"
platform="${2:-}"
timeout="${SMOKE_TIMEOUT:-120}"
name="gym-smoke-$$-${RANDOM}"
ok=0

fail() {
  echo "::error title=Image smoke test${platform:+ ($platform)}::$*"
  exit 1
}

cleanup() {
  if [ "$ok" -ne 1 ] && docker inspect "$name" >/dev/null 2>&1; then
    echo "----- container logs ($name) -----"
    docker logs "$name" 2>&1 | tail -n 60 || true
    echo "----- health log -----"
    docker inspect -f '{{json .State.Health}}' "$name" 2>/dev/null || true
  fi
  docker rm -f "$name" >/dev/null 2>&1 || true
}
trap cleanup EXIT

run_args=(--detach --name "$name" --publish 127.0.0.1::8000)
if [ -n "$platform" ]; then
  run_args+=(--platform "$platform")
fi
if [ -n "${SMOKE_RUN_ARGS:-}" ]; then
  read -r -a extra_args <<<"$SMOKE_RUN_ARGS"
  run_args+=("${extra_args[@]}")
fi

# Docker's classic image store, which GitHub's runners use, maps a digest
# reference to a single image. Once the amd64 run has pulled
# ghcr.io/...@sha256:<index>, pulling the same reference for arm64 fails with
# "cannot overwrite digest". Dropping the local copy first makes `docker run`
# pull the platform asked for; a local tag such as gym-tracker:ci is untouched.
if [[ "$image" == *@sha256:* ]]; then
  docker image rm "$image" >/dev/null 2>&1 || true
fi

echo "Starting $image${platform:+ for $platform}"
docker run "${run_args[@]}" "$image" >/dev/null

# 1. Docker's own health status.
deadline=$(( $(date +%s) + timeout ))
while :; do
  state="$(docker inspect -f '{{.State.Status}}' "$name")"
  health="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$name")"
  case "$state/$health" in
    running/healthy) break ;;
    running/none) fail "the image has no HEALTHCHECK" ;;
    running/unhealthy) fail "the HEALTHCHECK reports unhealthy" ;;
    running/*) ;;
    *) fail "the container stopped (state=$state, exit code $(docker inspect -f '{{.State.ExitCode}}' "$name"))" ;;
  esac
  [ "$(date +%s)" -lt "$deadline" ] || fail "not healthy after ${timeout}s (health=$health)"
  sleep 2
done
echo "Health: healthy after $(( timeout - (deadline - $(date +%s)) ))s"

# 2. Platform, read from the running process rather than from image metadata:
#    under Docker's containerd image store (Docker 29), `docker image inspect`
#    on a multi-platform digest reports the host's variant, not the one this
#    container runs. qemu-user reports the emulated machine to `uname -m`.
machine="$(docker exec "$name" uname -m)"
case "$machine" in
  x86_64) actual=linux/amd64 ;;
  aarch64) actual=linux/arm64 ;;
  *) actual="linux/$machine" ;;
esac
echo "Running platform: $actual"
if [ -n "$platform" ]; then
  want="${platform%/v*}"          # linux/arm64/v8 -> linux/arm64
  [ "$actual" = "$want" ] || fail "expected platform $want, got $actual"
fi

# 3. /healthz from outside the container, through the published port.
hostport="$(docker port "$name" 8000/tcp | head -n 1)"
[ -n "$hostport" ] || fail "port 8000 is not published"
body="$(curl -fsS --max-time 10 "http://${hostport}/healthz")" || fail "GET /healthz failed"
echo "GET /healthz -> $body"
status="$(printf '%s' "$body" | python3 -c 'import json, sys; print(json.load(sys.stdin).get("status"))')" \
  || fail "/healthz did not return JSON: $body"
[ "$status" = "ok" ] || fail "/healthz status is '$status', expected 'ok' (fault injection armed?)"

# 4. Not root.
uid="$(docker exec "$name" id -u)"
echo "Runs as uid $uid"
[ "$uid" = "10001" ] || fail "expected uid 10001 (appuser), got $uid"

# 5. Nothing .dockerignore is meant to keep out.
leaked="$(docker exec "$name" sh -c 'for p in .env .git .github tests; do [ -e "/app/$p" ] && echo "$p"; done; true')"
[ -z "$leaked" ] || fail "excluded paths are in the image: $(echo "$leaked" | tr '\n' ' ')"
echo "No .env, .git, .github or tests/ in /app"

# 6. Dependencies are consistent, and the compiled ones load on this platform.
docker exec "$name" python -m pip check --disable-pip-version-check \
  || fail "pip check found broken requirements"
docker exec "$name" python -c '
import httptools, psycopg2, pydantic_core, rapidfuzz, sqlalchemy, uvloop, watchfiles, yaml
import platform
print("Compiled dependencies import on", platform.machine())
' || fail "a compiled dependency failed to import"

ok=1
echo "Smoke test passed: $image${platform:+ ($platform)}"
