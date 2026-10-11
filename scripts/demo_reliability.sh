#!/usr/bin/env bash
# The HTTP caller's reliability demo (Assignment 2, Part A).
#
#   ./scripts/demo_reliability.sh            output also saved to
#                                            observability/results-a2/reliability.txt
#
# One run of clients/http_caller.py against a deliberately unreliable app:
#
#   1. The app is restarted with fault injection: every 4th request under
#      /api/sessions/ is held for 3s. The caller's timeout is 2s, so those
#      attempts time out and are RETRIED - with the same Idempotency-Key, so a
#      set the server finished writing after the client gave up is replayed,
#      not written twice.
#   2. A few seconds in, the app container is STOPPED. Connections are refused,
#      three attempts fail in a row, and the circuit breaker OPENS: the next
#      calls are blocked without touching the network.
#   3. The container is started again. After the 10s recovery period the
#      breaker lets one trial call through, it succeeds, and the breaker CLOSES.
#
# Finally the app is restarted without the fault, so nothing is left degraded.
# Needs the stack up (python scripts/quickstart.py) and the demo account.

set -euo pipefail
cd "$(dirname "$0")/.."

PY="${PYTHON:-python}"
OUT_DIR=observability/results-a2
mkdir -p "$OUT_DIR"
OUT="$OUT_DIR/reliability.txt"

wait_healthy() {
  for _ in $(seq 1 60); do
    status=$(docker inspect -f '{{.State.Health.Status}}' gym-tracker 2>/dev/null || echo none)
    [ "$status" = "healthy" ] && return 0
    sleep 1
  done
  echo "gym-tracker did not become healthy" >&2
  return 1
}

restore() {
  echo "restoring the app without fault injection"
  docker compose up -d app >/dev/null 2>&1 || true
}
trap restore EXIT

echo "restarting the app with injected latency (3s on every 4th /api/sessions/ request)"
FAULT_INJECTION_ENABLED=true FAULT_LATENCY_MS=3000 FAULT_LATENCY_EVERY_N=4 \
  FAULT_PATH_PREFIX=/api/sessions/ docker compose up -d app >/dev/null
wait_healthy

"$PY" clients/http_caller.py --calls 18 --interval 1 | tee "$OUT" &
caller=$!

sleep 9
echo ">>> stopping the app container" | tee -a "$OUT"
docker stop -t 2 gym-tracker >/dev/null
sleep 12
echo ">>> starting the app container" | tee -a "$OUT"
docker start gym-tracker >/dev/null

wait "$caller"
echo "saved to $OUT"
