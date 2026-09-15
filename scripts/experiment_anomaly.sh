#!/usr/bin/env bash
#
# Part E1 — reproduce a problem, measure it, and prove the recovery.
#
# Three stages, the same load test in each, one variable changed between them:
#
#   1. BASELINE   healthy app, record normal behaviour
#   2. FAULT      restart with a 500ms delay on every Nth request, record again
#   3. RECOVERY   restart clean, record a third time
#
# Only the fault changes between stages. The load is identical and
# deterministic (scripts/load_generator.py issues a fixed sequence), so any
# difference in the numbers is caused by the fault and not by how hard the app
# happened to be driven.
#
# Each stage runs long enough for several Prometheus scrapes. At a 15s scrape
# interval a 120s stage gives 8 of them — enough for a rate() over [1m] to be
# meaningful rather than interpolated from two points.
#
# Usage:
#   ./scripts/experiment_anomaly.sh                    # defaults below
#   STAGE_SECONDS=180 FAULT_LATENCY_MS=800 ./scripts/experiment_anomaly.sh
#
# Results are written to observability/results/ as JSON, one file per stage,
# plus a scrape of /metrics taken at the end of each stage.

set -euo pipefail

BASE_URL="${BASE_URL:-http://localhost:8000}"
PROM_URL="${PROM_URL:-http://localhost:9090}"
STAGE_SECONDS="${STAGE_SECONDS:-120}"
RPS="${RPS:-5}"
FAULT_LATENCY_MS="${FAULT_LATENCY_MS:-500}"
FAULT_LATENCY_EVERY_N="${FAULT_LATENCY_EVERY_N:-5}"
RESULTS_DIR="${RESULTS_DIR:-observability/results}"
COMPOSE="${COMPOSE:-docker compose}"
PYTHON="${PYTHON:-python3}"

mkdir -p "$RESULTS_DIR"

log() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }

wait_for_app() {
  local tries=0
  until curl -fsS "$BASE_URL/healthz" >/dev/null 2>&1; do
    tries=$((tries + 1))
    if [ "$tries" -gt 60 ]; then
      echo "app did not come up at $BASE_URL" >&2
      exit 1
    fi
    sleep 1
  done
}

# Record the wall-clock window of each stage. These timestamps are what you
# paste into Grafana's and Kibana's time pickers to look at one stage in
# isolation — without them the three stages blur into one chart.
stage_start() { date -u +"%Y-%m-%dT%H:%M:%SZ"; }

run_stage() {
  local label="$1"
  local started ended
  started="$(stage_start)"

  log "Stage: $label — ${STAGE_SECONDS}s at ${RPS} rps"
  "$PYTHON" scripts/load_generator.py \
    --base-url "$BASE_URL" \
    --duration "$STAGE_SECONDS" \
    --rps "$RPS" \
    --label "$label" \
    --json "$RESULTS_DIR/${label}.json"

  ended="$(stage_start)"

  # A raw scrape, kept alongside the client-side numbers. The two measure the
  # same requests from opposite ends: the load generator times them from
  # outside, the histogram from inside the app. They will not match exactly —
  # the difference is connection setup and the network — and the report says so.
  curl -fsS "$BASE_URL/metrics" > "$RESULTS_DIR/${label}-metrics.txt" || true

  "$PYTHON" - "$RESULTS_DIR/${label}.json" "$started" "$ended" <<'PY'
import json, sys
path, started, ended = sys.argv[1], sys.argv[2], sys.argv[3]
with open(path) as handle:
    data = json.load(handle)
data["window"] = {"from": started, "to": ended}
with open(path, "w") as handle:
    json.dump(data, handle, indent=2)
print(f"  window: {started} .. {ended}")
print(f"  p50={data['latency_ms']['p50']}ms  p95={data['latency_ms']['p95']}ms  "
      f"p99={data['latency_ms']['p99']}ms  errors={data['errors']}")
PY
}

# ---------------------------------------------------------------------------
# Predictions, stated BEFORE the fault is introduced.
#
# Writing these down first is the point of the exercise: an explanation
# invented after seeing the graph will fit any graph.
# ---------------------------------------------------------------------------
cat > "$RESULTS_DIR/predictions.md" <<EOF
# Predictions (written before the fault stage ran)

Fault: ${FAULT_LATENCY_MS}ms delay on every ${FAULT_LATENCY_EVERY_N}th request,
i.e. $(awk "BEGIN{printf \"%.0f\", 100/${FAULT_LATENCY_EVERY_N}}")% of requests.

## Metrics I expect to change

| Metric | Prediction | Reasoning |
|---|---|---|
| \`gym_http_request_duration_seconds\` p95 | rises to ~${FAULT_LATENCY_MS}ms | $(awk "BEGIN{printf \"%.0f\", 100/${FAULT_LATENCY_EVERY_N}}")% of requests are delayed, so the 95th percentile lands inside the delayed group |
| \`gym_http_request_duration_seconds\` p50 | barely moves | the median request is not one of the delayed ones |
| mean latency | rises by ~${FAULT_LATENCY_MS}/${FAULT_LATENCY_EVERY_N}ms | the delay averaged over all requests |
| \`gym_http_requests_in_flight\` | rises slightly | delayed requests overlap with new arrivals |
| \`gym_http_requests_total\` rate | falls, or holds | a closed-loop client issues fewer requests when each takes longer |
| \`gym_faults_injected_total{kind="latency"}\` | 0 -> non-zero | proves the fault was actually live |
| error rate | unchanged at 0 | a delay is not a failure; every request still returns 200 |

## Logs I expect to change

- \`event.duration_ms\` above ${FAULT_LATENCY_MS} on the delayed requests only.
- A \`warning\` line per delayed request: "injected fault: delaying this request
  deliberately", carrying \`fault.sequence\` and \`fault.latency_ms\`.
- No \`error\` lines: this fault degrades, it does not break.

## Effect on users

Roughly one request in ${FAULT_LATENCY_EVERY_N} takes half a second longer. A
page load usually involves several requests, so in practice most page loads feel
slow rather than one in ${FAULT_LATENCY_EVERY_N} — this is why a p95 matters
more than an average when judging how an app *feels*.
EOF

log "Predictions written to $RESULTS_DIR/predictions.md"

# ---------------------------------------------------------------------------
# Stage 1: baseline
# ---------------------------------------------------------------------------
log "Ensuring the app is running WITHOUT fault injection"
FAULT_INJECTION_ENABLED=false $COMPOSE up -d app
wait_for_app
# Let the process settle so the first scrape after start is not part of the
# measurement window.
sleep 20
run_stage "01-baseline"

# ---------------------------------------------------------------------------
# Stage 2: fault
# ---------------------------------------------------------------------------
log "Restarting the app WITH ${FAULT_LATENCY_MS}ms on every ${FAULT_LATENCY_EVERY_N}th request"
FAULT_INJECTION_ENABLED=true \
FAULT_LATENCY_MS="$FAULT_LATENCY_MS" \
FAULT_LATENCY_EVERY_N="$FAULT_LATENCY_EVERY_N" \
  $COMPOSE up -d --force-recreate app
wait_for_app

# Confirm from the app itself that the fault is armed, rather than assuming the
# environment variable took effect.
log "Confirming the fault is armed"
curl -fsS "$BASE_URL/healthz" | tee "$RESULTS_DIR/02-fault-healthz.json"
echo
sleep 20
run_stage "02-fault"

# ---------------------------------------------------------------------------
# Stage 3: recovery
# ---------------------------------------------------------------------------
log "Removing the fault and restarting"
FAULT_INJECTION_ENABLED=false $COMPOSE up -d --force-recreate app
wait_for_app
log "Confirming the fault is gone (status should be 'ok', not 'degraded')"
curl -fsS "$BASE_URL/healthz" | tee "$RESULTS_DIR/03-recovery-healthz.json"
echo
sleep 20
run_stage "03-recovery"

# ---------------------------------------------------------------------------
# Comparison
# ---------------------------------------------------------------------------
log "Results"
"$PYTHON" - "$RESULTS_DIR" <<'PY'
import json, pathlib, sys

results_dir = pathlib.Path(sys.argv[1])
rows = []
for name in ("01-baseline", "02-fault", "03-recovery"):
    path = results_dir / f"{name}.json"
    if not path.exists():
        continue
    data = json.loads(path.read_text())
    latency = data["latency_ms"]
    rows.append((
        name, data["requests"], data["achieved_rps"],
        latency["p50"], latency["p95"], latency["p99"], latency["mean"],
        data["error_rate"],
        data.get("window", {}).get("from", ""), data.get("window", {}).get("to", ""),
    ))

header = f"| {'stage':<12} | {'reqs':>5} | {'rps':>6} | {'p50':>8} | {'p95':>8} | {'p99':>8} | {'mean':>8} | {'errors':>7} |"
print(header)
print("|" + "-" * (len(header) - 2) + "|")
for r in rows:
    print(f"| {r[0]:<12} | {r[1]:>5} | {r[2]:>6.2f} | {r[3]:>7.1f}ms | {r[4]:>7.1f}ms "
          f"| {r[5]:>7.1f}ms | {r[6]:>7.1f}ms | {r[7]:>7.2%} |")

print("\nTime windows to paste into Grafana / Kibana:")
for r in rows:
    print(f"  {r[0]:<12} {r[8]} .. {r[9]}")

markdown = results_dir / "summary.md"
with markdown.open("w") as handle:
    handle.write("# Anomaly experiment results\n\n")
    handle.write("| Stage | Requests | Achieved rps | p50 | p95 | p99 | Mean | Error rate |\n")
    handle.write("|---|---|---|---|---|---|---|---|\n")
    for r in rows:
        handle.write(f"| {r[0]} | {r[1]} | {r[2]:.2f} | {r[3]:.1f}ms | {r[4]:.1f}ms "
                     f"| {r[5]:.1f}ms | {r[6]:.1f}ms | {r[7]:.2%} |\n")
    handle.write("\n## Time windows\n\n")
    for r in rows:
        handle.write(f"- **{r[0]}**: `{r[8]}` .. `{r[9]}`\n")
print(f"\nWrote {markdown}")
PY

log "Done. The app is back to normal — confirm with: curl -s $BASE_URL/healthz"
echo "PromQL for the same view, over the whole experiment:"
echo "  histogram_quantile(0.95, sum by (le) (rate(gym_http_request_duration_seconds_bucket[1m])))"
echo "  sum by (kind) (increase(gym_faults_injected_total[1m]))"
