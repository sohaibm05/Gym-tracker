#!/usr/bin/env bash
#
# Part E2 — cardinality explosion, demonstrated and then undone.
#
#   1. Run a counter labelled with a request id. Stop at 100 unique ids.
#   2. Show the series count growing: count(demo_requests_total).
#   3. Remove the label, restart, repeat. Compare after another scrape.
#
# Two ways to run this:
#
#   ./scripts/experiment_cardinality.sh           in-process, no stack needed
#   ./scripts/experiment_cardinality.sh --stack   through Prometheus in compose
#
# The in-process mode counts the series in the exposition text directly, which
# is the same number Prometheus would store. The --stack mode goes the whole
# way round so you can run the PromQL yourself and see it in Grafana.
#
# Nothing here is capable of stressing a real Prometheus: the demo refuses to
# mint more than 100 unique ids. The lesson is the shape of the growth, not the
# damage.

set -euo pipefail

PYTHON="${PYTHON:-python3}"
PROM_URL="${PROM_URL:-http://localhost:9090}"
DEMO_URL="${DEMO_URL:-http://localhost:8001}"
REQUESTS="${REQUESTS:-100}"
RESULTS_DIR="${RESULTS_DIR:-observability/results}"
COMPOSE="${COMPOSE:-docker compose}"
USE_STACK=0

[ "${1:-}" = "--stack" ] && USE_STACK=1

mkdir -p "$RESULTS_DIR"
log() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }

promql() {
  # URL-encodes the query and returns the first sample's value, or "none".
  local query="$1"
  curl -fsS --get "$PROM_URL/api/v1/query" --data-urlencode "query=${query}" \
    | "$PYTHON" -c 'import json,sys; r=json.load(sys.stdin)["data"]["result"]; print(r[0]["value"][1] if r else "none")'
}

# ---------------------------------------------------------------------------
# In-process mode
# ---------------------------------------------------------------------------
if [ "$USE_STACK" -eq 0 ]; then
  log "Step 1-2: WITH the request_id label — $REQUESTS requests"
  $PYTHON observability/cardinality_demo.py --drive "$REQUESTS" 2>&1 | tee "$RESULTS_DIR/cardinality-unsafe.txt"

  log "Step 3: WITHOUT the label — the same $REQUESTS requests"
  $PYTHON observability/cardinality_demo.py --drive "$REQUESTS" --safe-only 2>&1 \
    | tee "$RESULTS_DIR/cardinality-safe.txt"

  log "Step 4: growth curve — series created per N requests, with the label"
  {
    echo "| Requests | demo_requests_total series | demo_requests_safe_total series |"
    echo "|---|---|---|"
  } > "$RESULTS_DIR/cardinality-growth.md"

  for n in 1 10 25 50 100; do
    # A fresh process each time, so each row is an independent measurement
    # rather than a running total.
    line="$($PYTHON observability/cardinality_demo.py --drive "$n" 2>&1 >/dev/null | tail -1)"
    unsafe="$(echo "$line" | sed -n 's/.*demo_requests_total series=\([0-9]*\).*/\1/p')"
    safe="$(echo "$line" | sed -n 's/.*demo_requests_safe_total series=\([0-9]*\).*/\1/p')"
    printf '| %s | %s | %s |\n' "$n" "$unsafe" "$safe" | tee -a "$RESULTS_DIR/cardinality-growth.md"
  done

  log "Result"
  cat "$RESULTS_DIR/cardinality-growth.md"
  echo
  echo "One series per request against one series in total. At 1000 requests/second"
  echo "the labelled counter would create 86.4 million series in a day."
  exit 0
fi

# ---------------------------------------------------------------------------
# Full-stack mode
# ---------------------------------------------------------------------------
log "Starting the cardinality demo container (unsafe: request_id label present)"
CARDINALITY_SAFE_ONLY=false $COMPOSE --profile experiment up -d --force-recreate cardinality-demo

log "Waiting for Prometheus to discover the target"
for _ in $(seq 1 30); do
  if [ "$(promql 'up{job="cardinality-demo"}')" = "1" ]; then break; fi
  sleep 2
done

log "Generating $REQUESTS requests, each with a unique request_id"
for _ in $(seq 1 "$REQUESTS"); do curl -fsS "$DEMO_URL/demo" >/dev/null || true; done

log "Waiting two scrape intervals so Prometheus has the new series"
sleep 35

UNSAFE_SERIES="$(promql 'count(demo_requests_total)')"
SAFE_SERIES="$(promql 'count(demo_requests_safe_total)')"
log "WITH the label:    count(demo_requests_total)      = $UNSAFE_SERIES"
echo    "WITHOUT the label: count(demo_requests_safe_total) = $SAFE_SERIES"

log "Step 3: removing the label and restarting the demo app"
CARDINALITY_SAFE_ONLY=true $COMPOSE --profile experiment up -d --force-recreate cardinality-demo
sleep 5
for _ in $(seq 1 "$REQUESTS"); do curl -fsS "$DEMO_URL/demo" >/dev/null || true; done

log "Waiting for another scrape"
sleep 35

UNSAFE_AFTER="$(promql 'count(demo_requests_total)')"
SAFE_AFTER="$(promql 'count(demo_requests_safe_total)')"

{
  echo "# Cardinality experiment (through Prometheus)"
  echo
  echo "| Stage | count(demo_requests_total) | count(demo_requests_safe_total) |"
  echo "|---|---|---|"
  echo "| With request_id label | $UNSAFE_SERIES | $SAFE_SERIES |"
  echo "| Label removed, app restarted | $UNSAFE_AFTER | $SAFE_AFTER |"
  echo
  echo "## The part people get wrong"
  echo
  echo "The second row usually reads 'none', and that is NOT the same as the data"
  echo "having been deleted. When the app stops exporting the labelled counter,"
  echo "Prometheus writes a stale marker and the series vanishes from queries"
  echo "evaluated NOW. The samples already written are untouched: query the same"
  echo "expression at a timestamp inside the first stage and the 100 series are"
  echo "still there, until the 15-day retention window drops them."
  echo
  echo "  # instant, now:      no data"
  echo "  # instant, --time=<a timestamp during stage 1>:  100"
  echo
  echo "So you cannot verify a cardinality fix by watching count() fall: it falls"
  echo "either way, whether you removed the label or merely stopped the app. And"
  echo "the memory those series consumed while they were active is never given"
  echo "back by removing the label afterwards. That is why cardinality is a"
  echo "code-review question, not an incident-response one."
} | tee "$RESULTS_DIR/cardinality-stack.md"

log "Cleaning up the demo container"
$COMPOSE --profile experiment rm -sf cardinality-demo

log "Done. Results in $RESULTS_DIR/"
