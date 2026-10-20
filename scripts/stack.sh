#!/usr/bin/env bash
#
# One entry point for the observability stack: start, check, seed, clean up.
#
#   ./scripts/stack.sh up        build, start, seed demo data and verify everything
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
# python3 on Linux and macOS. On Windows (Git Bash) it is usually `python` or
# `py`, and `python3` can be the Microsoft Store stub, which only prints a hint
# and fails -- so each candidate has to actually run, not merely exist.
if [ -z "${PYTHON:-}" ]; then
  for candidate in python3 python py; do
    if "$candidate" -c 'import sys; sys.exit(sys.version_info < (3, 8))' >/dev/null 2>&1; then
      PYTHON="$candidate"
      break
    fi
  done
fi
: "${PYTHON:?Python 3.8 or newer is needed on PATH, or set PYTHON=/path/to/python}"
BASE_URL="${BASE_URL:-http://localhost:8000}"
PROM_URL="${PROM_URL:-http://localhost:9090}"

log()  { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m!! %s\033[0m\n' "$*"; }

cmd_up() {
  # The cross-platform quick start does the whole sequence -- build, wait for
  # health, seed the demo account and business data, generate traffic, import
  # the Kibana objects -- and then verifies every piece, so `stack.sh up` and
  # `python scripts/quickstart.py` (for PowerShell users) behave identically.
  "$PYTHON" scripts/quickstart.py "$@"
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
    # No single quotes anywhere in this program: the shell passes it inside a
    # single-quoted string, so a quoted dict key would lose its quotes and reach
    # Python as a bare (undefined) name.
    url = target["scrapeUrl"]
    print(f"  [{mark}] {job:<18} {url}{note}")
    error = target.get("lastError")
    if health != "up" and error and not note:
        print(f"         last error: {error}")
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
  up)      shift; cmd_up "$@" ;;
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
