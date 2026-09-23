#!/usr/bin/env bash
#
# One entry point for the observability stack: start, check, seed, clean up.
#
#   ./scripts/stack.sh up        build and start everything, then wait for health
#   ./scripts/stack.sh status    what is running, and whether Prometheus sees it
#   ./scripts/stack.sh urls      where everything is
#   ./scripts/stack.sh logs      follow the app's logs
#   ./scripts/stack.sh load      generate traffic so the dashboards have data
#   ./scripts/stack.sh down      stop, KEEPING the data
#   ./scripts/stack.sh destroy   stop and DELETE every volume
#
# `down` and `destroy` are deliberately separate. Losing an experiment's data to
# a habit of typing `down -v` is a bad afternoon.

set -euo pipefail

COMPOSE="${COMPOSE:-docker compose}"
PYTHON="${PYTHON:-python3}"
BASE_URL="${BASE_URL:-http://localhost:8000}"
PROM_URL="${PROM_URL:-http://localhost:9090}"

log()  { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m!! %s\033[0m\n' "$*"; }

wait_for() {
  local name="$1" url="$2" tries="${3:-60}"
  printf 'waiting for %-16s' "$name"
  for _ in $(seq 1 "$tries"); do
    if curl -fsS -o /dev/null "$url" 2>/dev/null; then
      printf ' ready\n'
      return 0
    fi
    printf '.'
    sleep 2
  done
  printf ' TIMED OUT\n'
  return 1
}

cmd_up() {
  log "Building and starting the stack"
  $COMPOSE up -d --build

  log "Waiting for services"
  wait_for "app"           "$BASE_URL/healthz"                || warn "app not ready"
  wait_for "prometheus"    "$PROM_URL/-/ready"                || warn "prometheus not ready"
  wait_for "grafana"       "http://localhost:3000/api/health" || warn "grafana not ready"
  wait_for "elasticsearch" "http://localhost:9200/_cluster/health" 90 \
    || warn "elasticsearch not ready (it is the slowest to start)"
  wait_for "kibana"        "http://localhost:5601/api/status" 90 || warn "kibana not ready"

  log "Generating a little traffic so nothing is empty"
  $PYTHON scripts/load_generator.py --requests 40 --rps 10 --label warmup >/dev/null 2>&1 || true

  log "Importing Kibana saved objects"
  ./scripts/setup_kibana.sh || warn "Kibana import failed — see the message above"

  cmd_urls
}

cmd_status() {
  log "Containers"
  $COMPOSE ps

  log "Prometheus targets"
  curl -fsS "$PROM_URL/api/v1/targets" 2>/dev/null | "$PYTHON" -c '
import json, sys
try:
    data = json.load(sys.stdin)["data"]["activeTargets"]
except Exception:
    print("  Prometheus is not answering."); raise SystemExit
for target in data:
    job = target["labels"].get("job", "?")
    health = target["health"]
    mark = "OK  " if health == "up" else "DOWN"
    note = ""
    if job == "cardinality-demo" and health != "up":
        note = "  (expected: only runs under the experiment profile)"
    print(f"  [{mark}] {job:<18} {target['scrapeUrl']}{note}")
    if health != "up" and target.get("lastError") and not note:
        print(f"         last error: {target['lastError']}")
' || warn "could not read targets"

  log "Documents indexed"
  curl -fsS "http://localhost:9200/filebeat-gym-tracker-*/_count" 2>/dev/null \
    || echo "  Elasticsearch is not answering."
  echo

  log "Fault injection"
  curl -fsS "$BASE_URL/healthz" 2>/dev/null || echo "  app not answering"
  echo
}

cmd_urls() {
  cat <<EOF

  App              $BASE_URL
  Metrics          $BASE_URL/metrics
  Prometheus       $PROM_URL           (targets: $PROM_URL/targets)
  Alerts           $PROM_URL/alerts
  Grafana          http://localhost:3000   (admin / admin)
                     - Gym Tracker / Application performance
                     - Gym Tracker / Business metrics
                     - Gym Tracker / Host (node-exporter)
  Kibana           http://localhost:5601    (Discover -> Open -> saved searches)
  Elasticsearch    http://localhost:9200
  node-exporter    not reachable at localhost:9100 from Windows. It runs in
                   the WSL2 VM's network namespace, so it listens on the VM,
                   not on this host. Prometheus scrapes it at 172.28.77.1:9100
                   -- see that target on $PROM_URL/targets, or the
                   Gym Tracker / Host dashboard in Grafana.
  Cardinality demo http://localhost:8001    (profile "experiment" only, start with:
                     docker compose --profile experiment up -d cardinality-demo)

EOF
}

cmd_load() {
  log "Generating load (Ctrl-C to stop early)"
  $PYTHON scripts/load_generator.py --duration "${DURATION:-120}" --rps "${RPS:-5}" --label manual
}

case "${1:-}" in
  up)      cmd_up ;;
  status)  cmd_status ;;
  urls)    cmd_urls ;;
  logs)    $COMPOSE logs -f app ;;
  load)    cmd_load ;;
  down)
    log "Stopping. Volumes are kept — use 'destroy' to delete them."
    $COMPOSE --profile experiment down
    ;;
  destroy)
    warn "This deletes the Postgres data, the metric history and every indexed log."
    read -r -p "Type 'destroy' to confirm: " reply
    if [ "$reply" = "destroy" ]; then
      $COMPOSE --profile experiment down -v
      log "Gone."
    else
      log "Cancelled; nothing was deleted."
    fi
    ;;
  *)
    sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'
    exit 1
    ;;
esac
