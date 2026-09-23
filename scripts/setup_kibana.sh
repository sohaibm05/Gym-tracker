#!/usr/bin/env bash
#
# Import the Kibana data view and saved searches.
#
# Run once after the stack is up and Filebeat has shipped at least one document.
# Kibana can only build a data view over fields it can see, so importing before
# any log has been indexed produces a view with no fields.
#
#   ./scripts/setup_kibana.sh
#
# Idempotent: the import overwrites objects with the same id, so running it
# again after editing observability/kibana/saved-objects.ndjson updates them in
# place rather than creating duplicates.

set -euo pipefail

KIBANA_URL="${KIBANA_URL:-http://localhost:5601}"
ES_URL="${ES_URL:-http://localhost:9200}"
BASE_URL="${BASE_URL:-http://localhost:8000}"
OBJECTS="${OBJECTS:-observability/kibana/saved-objects.ndjson}"

log() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }

log "Waiting for Kibana at $KIBANA_URL"
for attempt in $(seq 1 60); do
  # Kibana reports "available" only once it has finished its own migrations;
  # a 200 on the port alone arrives well before it can accept an import.
  if curl -fsS "$KIBANA_URL/api/status" 2>/dev/null | grep -q '"level":"available"'; then
    echo "Kibana is available."
    break
  fi
  if [ "$attempt" -eq 60 ]; then
    echo "Kibana did not become available in time." >&2
    echo "Check: docker compose logs kibana" >&2
    exit 1
  fi
  sleep 5
done

log "Checking that logs have actually been indexed"
COUNT="$(curl -fsS "$ES_URL/filebeat-gym-tracker-*/_count" 2>/dev/null \
  | sed -n 's/.*"count":\([0-9]*\).*/\1/p' || echo 0)"
COUNT="${COUNT:-0}"

if [ "$COUNT" = "0" ]; then
  echo
  echo "WARNING: no documents in filebeat-gym-tracker-*."
  echo "The data view will import but will have no fields to offer."
  echo "Generate some traffic first, then re-run this script:"
  echo "    python scripts/load_generator.py --requests 50 --rps 10"
  echo
else
  echo "$COUNT documents indexed."
fi

log "Importing saved objects from $OBJECTS"
# kbn-xsrf is required on every Kibana write; without it the API returns 400.
RESPONSE="$(curl -fsS -X POST "$KIBANA_URL/api/saved_objects/_import?overwrite=true" \
  -H "kbn-xsrf: true" \
  --form file=@"$OBJECTS")"

echo "$RESPONSE"

if echo "$RESPONSE" | grep -q '"success":true'; then
  log "Imported."
  cat <<EOF

Open Kibana:            $KIBANA_URL
Saved searches:         Analytics -> Discover -> Open
Data view:              filebeat-gym-tracker-*

The searches that were installed:

  01 - All gym-tracker logs
  02 - Errors and warnings
  03 - Slow requests (over 500ms)
  04 - Trace one request by id      <- paste an x-request-id into the query bar
  05 - Workout entry events
  06 - Authentication failures
  07 - Injected faults (Part E)
  08 - LLM extraction failures

To trace one request end to end:

  curl -si $BASE_URL/healthz | grep -i x-request-id
  then search   http.request.id: "<that value>"   in Discover.

EOF
else
  echo
  echo "Import reported a problem. The saved-object schema is version-specific," >&2
  echo "so if this fails on a different Kibana version, create the data view by" >&2
  echo "hand (Stack Management -> Data Views -> filebeat-gym-tracker-*, time" >&2
  echo "field @timestamp) and type the queries from the report directly." >&2
  exit 1
fi
