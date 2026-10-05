// Curated explanations for the viva tutor.
//
// Every claim here was checked against the code. Line numbers are never typed
// by hand: [[name]] is replaced at render time with the file:line that
// build_snippets.py located in the real source, and snip('name') shows a code
// excerpt cut from the real file. `q:` + `src:` pairs are queries that
// build_snippets.py --check confirms appear verbatim in that file.

const snip = (id) => ({ snippet: id });

// ---------------------------------------------------------------------------
// Architecture
// ---------------------------------------------------------------------------

window.COMPONENTS = [
  {
    id: 'app', name: 'App (FastAPI)', x: 290, y: 150, group: 'app',
    what: 'The gym tracker itself: a FastAPI web app run by uvicorn with <b>one worker</b>. It serves the pages, exposes <code>/metrics</code>, and prints one JSON log line per event to stdout.',
    where: ['app.py — routes and the <code>log_requests</code> middleware at [[mw_def]]', 'metrics.py — every metric, singleton <code>METRICS</code> at [[m_singleton]]', 'logging_setup.py — JSON formatter, request id, redaction', 'faults.py — the Part E fault switch', 'Dockerfile — image, <code>--workers 1</code> at [[df_workers]]', 'docker-compose.yml service <code>app</code> at [[dc_app]]'],
    why: 'It is the thing being observed. All HTTP telemetry comes from one middleware, so a route cannot disagree with the request log about what happened.',
    talks: ['Postgres — SQL over the gym-net bridge (<code>DATABASE_URL=postgresql://gym:gym@postgres:5432/gymtracker</code>)', 'Groq — HTTPS, only on <code>POST /log</code>', 'Prometheus — is <i>scraped by</i> it (pull) on <code>GET /metrics</code>', 'Docker — writes stdout, which the json-file driver saves'],
    stores: 'Nothing persistent. Metric values live <b>in process memory</b> (prometheus_client), so a restart resets every counter to 0. The open-drafts dict (<code>DraftTracker._open</code>) is also in memory.',
    dies: 'The site is down. Prometheus records <code>up{job="gym-tracker"} = 0</code> and the <code>TargetDown</code> alert ([[alert_target_down]]) fires after 1 minute. Counters restart from 0 when it comes back — <code>rate()</code>/<code>increase()</code> handle that as a counter reset.',
    viva: [
      { q: 'Why does the app run with only one uvicorn worker?', a: 'prometheus_client keeps counters in process memory. With several workers each holds its own copy, and Prometheus scraping one port would hit a different worker each time, so counters would appear to jump backwards. One worker avoids needing the multiprocess collector. The comment is right above the CMD in the Dockerfile.' },
      { q: 'Does the app depend on Prometheus or Elasticsearch to serve requests?', a: 'No. It only increments in-memory objects and writes to stdout. If the monitoring stack dies, requests are still served; we just lose visibility.' },
      { q: 'Where is HTTP telemetry produced?', a: 'In one place: the <code>log_requests</code> middleware in app.py. It sets the request id, increments in-flight, starts the timer, observes the histogram, counts the response and writes the "request served" log.' },
      { q: 'What does /healthz check?', a: 'Only that the process is up — it does NOT query Postgres. It reports "degraded" if fault injection is armed. So a database outage would not show on /healthz; it shows as 500s in metrics and "request failed" logs.' },
    ],
  },
  {
    id: 'postgres', name: 'Postgres', x: 60, y: 150, group: 'app',
    what: 'The system of record: users, sessions, workout_logs, bodyweight, etc. Image <code>postgres:16-alpine</code>.',
    where: ['docker-compose.yml service <code>postgres</code> at [[dc_postgres]]', 'schema.sql — mounted into <code>/docker-entrypoint-initdb.d</code>, run once on an empty volume', 'pipeline.py <code>commit_draft</code> — the write at <code>POST /save</code> ([[commit_call]])'],
    why: 'The product data has to survive restarts. It is the only component whose loss loses user data.',
    talks: ['App — answers SQL queries'],
    stores: 'Workouts, users, sessions — on the named volume <code>postgres-data</code>.',
    dies: 'Saving and charts fail: routes that touch the DB raise, the middleware counts them as status 500 in <code>gym_http_requests_total</code> and <code>gym_http_exceptions_total</code>, and logs "request failed" with <code>error.type</code>. /healthz still says ok (it does not touch the DB).',
    viva: [
      { q: 'How would you notice Postgres being down?', a: 'Error rate panel and HighErrorRate alert (5xx share > 5% for 2m), the Unhandled exceptions panel by exception class, and "request failed" log lines in Kibana search 02 (errors and warnings).' },
      { q: 'Is the database commit timed separately?', a: 'Yes: <code>gym_db_commit_duration_seconds</code>, a histogram observed via <code>observe_duration</code> around <code>pipeline.commit_draft</code> at POST /save. It separates "the DB is slow" from "the request is slow".' },
    ],
  },
  {
    id: 'groq', name: 'Groq API', x: 290, y: 30, group: 'external',
    what: 'External LLM API that turns a pasted journal entry into structured sets. Called only on <code>POST /log</code>.',
    where: ['pipeline.py — extraction loop timed at [[p_inflight_inc]]', '<code>_record_extraction</code> at [[p_record_extraction]]', 'compose env <code>GROQ_API_KEY</code>, <code>GROQ_MODEL</code>'],
    why: 'It is the product\'s core trick (free text → sets), and the dependency most likely to be slow or rate-limited, so it has its own metrics.',
    talks: ['App → Groq over HTTPS'],
    stores: 'Nothing for us.',
    dies: 'Parsing fails; nothing is lost because nothing is saved before review. Counted as <code>gym_llm_extractions_total{outcome="error"}</code> or <code>"rate_limited"</code>, logged as "extraction attempt failed". With no key at all: "extraction unavailable" warning and a page saying nothing was saved.',
    viva: [
      { q: 'Why record the LLM duration on failures too?', a: 'Because a histogram that only observes successes would report the system getting faster as it starts failing. <code>_record_extraction</code> records outcome and duration on every path.' },
      { q: 'Does the experiment need a Groq key?', a: 'No. The load generator only hits /healthz, /, /login, /progress, /metrics — routes that work without Groq.' },
    ],
  },
  {
    id: 'prometheus', name: 'Prometheus', x: 580, y: 150, group: 'metrics',
    what: 'Time-series database that <b>pulls</b> metrics: every 15 s it GETs <code>/metrics</code> from each target, stores the samples, and evaluates alert rules.',
    where: ['observability/prometheus/prometheus.yml — <code>scrape_interval: 15s</code> at [[prom_scrape_interval]], job gym-tracker at [[prom_job_app]]', 'observability/prometheus/alerts.yml — alert rules', 'docker-compose.yml service <code>prometheus</code> at [[dc_prometheus]], retention <code>15d</code> at [[dc_retention]]'],
    why: 'Metrics need somewhere to be stored over time and queried with PromQL. Pull means a dead target is itself visible: <code>up</code> drops to 0.',
    talks: ['App <code>app:8000/metrics</code> (Docker DNS name)', 'Node Exporter at <code>172.28.77.1:9100</code> (bridge gateway, because node-exporter is in the host network)', 'Itself <code>localhost:9090</code>', 'cardinality-demo <code>:8001</code> (only under the experiment profile; DOWN otherwise, which is expected)', 'Grafana queries it'],
    stores: 'Metric samples on volume <code>prometheus-data</code>, deleted after <b>15 days</b> (<code>--storage.tsdb.retention.time=15d</code>).',
    dies: 'Dashboards go blank. Scrapes missed while it is down are a <b>permanent gap</b> — the app does not buffer metrics. The app itself is unaffected; when Prometheus returns it scrapes the current counter values again.',
    viva: [
      { q: 'What happens if Prometheus goes down?', a: 'The app keeps working because it does not depend on Prometheus to serve requests. Prometheus just stops scraping, so there is a gap in the data. Counters keep counting in the app, so after it comes back, <code>increase()</code> over a window that spans the gap still sees the total rise.' },
      { q: 'Pull vs push — why pull?', a: 'With pull, Prometheus knows the list of targets, so silence is detectable: <code>up == 0</code> and the TargetDown alert. A push system cannot tell "nothing happened" from "the sender died".' },
      { q: 'Why 15 s?', a: 'It is the resolution of every chart: a spike shorter than one scrape is not in the data at all. It also sets the floor for <code>rate()</code> windows — you need at least a few samples in the window, which is why the dashboards use 1m and 5m.' },
      { q: 'Do the alerts notify anyone?', a: 'No. There is no Alertmanager in the stack; alerts only show in the Prometheus /alerts page. Being honest about this matters.' },
    ],
  },
  {
    id: 'node', name: 'Node Exporter', x: 580, y: 30, group: 'metrics',
    what: 'Exposes host-level metrics: CPU, memory, disk, network (<code>node_*</code> series).',
    where: ['docker-compose.yml service <code>node-exporter</code> at [[dc_node_exporter]] — <code>network_mode: host</code>, <code>pid: host</code>, read-only mounts of /proc, /sys, /', 'prometheus.yml job node-exporter at [[prom_job_node]], label <code>machine: docker-host</code>', 'observability/grafana/dashboards/03-host-node-exporter.json'],
    why: 'The brief asks for machine metrics. App metrics cannot tell you the host is out of memory or disk.',
    talks: ['Scraped by Prometheus at 172.28.77.1:9100'],
    stores: 'Nothing.',
    dies: 'No host metrics; <code>up{job="node-exporter"}=0</code> and TargetDown fires.',
    viva: [
      { q: 'Which machine is being measured?', a: 'On Docker Desktop for Windows it is the WSL2 Linux VM that runs all the containers (12 cores, 7.56 GiB in the report), not the Windows PC. On a Linux host it is the host.' },
      { q: 'Why is it scraped at 172.28.77.1 rather than by container name?', a: 'It runs in the host network namespace, so it is not on the gym-net bridge. 172.28.77.1 is the pinned bridge gateway, i.e. the host as seen from a container. host.docker.internal was avoided because on Docker Desktop it resolves to Windows, not the VM.' },
    ],
  },
  {
    id: 'grafana', name: 'Grafana', x: 810, y: 150, group: 'metrics',
    what: 'Dashboards. Runs PromQL against Prometheus and draws three provisioned dashboards.',
    where: ['observability/grafana/provisioning/datasources/datasources.yml — Prometheus (uid <code>prometheus</code>) and Elasticsearch datasources', 'observability/grafana/provisioning/dashboards/dashboards.yml — loads JSON from disk', 'observability/grafana/dashboards/01-app-performance.json, 02-business.json, 03-host-node-exporter.json', 'docker-compose.yml service <code>grafana</code> at [[dc_grafana]]'],
    why: 'Turns stored series into something a human reads. Dashboards are files in the repo, so they are versioned and reviewable, not clicked together and lost with a volume.',
    talks: ['Prometheus (<code>http://prometheus:9090</code>, PromQL)', 'Elasticsearch datasource is provisioned, but no dashboard panel actually uses it'],
    stores: 'Its own DB on volume <code>grafana-data</code>; the dashboards themselves are files mounted read-only.',
    dies: 'No charts. Collection (Prometheus, Filebeat) continues, so nothing is lost.',
    viva: [
      { q: 'Does Grafana store the metrics?', a: 'No. Every panel is a live PromQL query to Prometheus at render time (refresh every 10s). Grafana only stores dashboard definitions and settings.' },
      { q: 'Why provision dashboards from files?', a: 'A dashboard that only exists in Grafana\'s database is lost with the volume and cannot be reviewed in a diff. Files are the source of truth; UI edits are overwritten on restart.' },
    ],
  },
  {
    id: 'docker', name: 'Docker json-file logs', x: 290, y: 300, group: 'logs',
    what: 'Docker\'s logging driver captures the app container\'s stdout and appends each line, wrapped in its own JSON envelope, to <code>/var/lib/docker/containers/&lt;id&gt;/&lt;id&gt;-json.log</code>.',
    where: ['docker-compose.yml app <code>logging:</code> json-file, <code>max-size: "10m"</code>, <code>max-file: "3"</code> at [[dc_logging]]', 'logging_setup.py <code>configure_logging</code> writes to <code>sys.stdout</code> ([[l_configure]])', 'Dockerfile <code>PYTHONUNBUFFERED=1</code> at [[df_unbuffered]]'],
    why: 'Writing to stdout makes the container runtime responsible for capturing, rotating and cleaning up the log file. The app never manages a log file.',
    talks: ['App writes stdout → Docker', 'Filebeat reads the files (mounted read-only) and asks the Docker socket which container each file belongs to'],
    stores: 'The raw log file on the Docker host. Rotated at 10 MB, 3 files kept (so ≤ ~30 MB per container). Deleted when the container is removed.',
    dies: '(Not a separate service.) If the file rotates before Filebeat reads it, those lines are lost.',
    viva: [
      { q: 'Why PYTHONUNBUFFERED?', a: 'When stdout is a pipe, Python buffers it, so log lines could sit in memory for minutes and Filebeat would see a gap that looks like downtime.' },
      { q: 'Why cap the log file size?', a: 'Without max-size the json-file log grows without limit and a long experiment can fill the disk — the monitoring taking down what it monitors.' },
    ],
  },
  {
    id: 'filebeat', name: 'Filebeat', x: 580, y: 300, group: 'logs',
    what: 'Log shipper. Autodiscovers containers labelled <code>co.elastic.logs/enabled=true</code>, tails their Docker log files, strips Docker\'s wrapper, parses the app\'s JSON into fields, adds container metadata, and bulk-sends to Elasticsearch.',
    where: ['observability/filebeat/filebeat.yml — autodiscover at [[fb_autodiscover]], label condition at [[fb_condition]], <code>decode_json_fields</code> at [[fb_decode]], output at [[fb_output]], ILM at [[fb_ilm]]', 'docker-compose.yml service <code>filebeat</code> at [[dc_filebeat]], registry volume at [[dc_filebeat_registry]]', 'App label at [[dc_label]]'],
    why: 'Something has to move lines from files on disk into a searchable store, and turn text into typed fields. The parsing step is what makes <code>http.request.id</code> an exact-match keyword and the duration a number.',
    talks: ['Reads <code>/var/lib/docker/containers</code> (read-only bind mount)', 'Docker socket (read-only) for container names and labels', 'Elasticsearch <code>http://elasticsearch:9200</code>', 'Loads the ILM policy and index template into Elasticsearch at startup'],
    stores: 'Its <b>registry</b> (byte offset reached in each file) on volume <code>filebeat-data</code>, so a restart resumes rather than re-indexing.',
    dies: 'Logs keep accumulating in the Docker files; when Filebeat returns it resumes from the registry offset. Lines are lost only if Docker rotates a file away (past 3 × 10 MB) before Filebeat reads it.',
    viva: [
      { q: 'Why only containers with that label?', a: 'So Elasticsearch, Kibana and Filebeat do not ship their own logs into the same cluster — a feedback loop where an ES error produces a log that ES indexes, which can produce another error.' },
      { q: 'What do the three key decode_json_fields settings do?', a: '<code>target: ""</code> puts the app\'s keys at the top level; <code>overwrite_keys: true</code> lets the app\'s values win, so its own <code>@timestamp</code> (when it happened) replaces Filebeat\'s (when it was read); <code>expand_keys: true</code> turns dotted keys like <code>http.request.id</code> into nested objects. Also <code>add_error_key: true</code> marks lines that are not valid JSON instead of dropping them.' },
      { q: 'Why autodiscover rather than a fixed path?', a: 'Container IDs change every time a container is recreated, so a hardcoded path breaks on the first rebuild. Autodiscover watches the Docker API.' },
    ],
  },
  {
    id: 'es', name: 'Elasticsearch', x: 810, y: 300, group: 'logs',
    what: 'Stores and indexes each log line as a JSON document with typed fields, in the data stream <code>filebeat-gym-tracker-logs</code>.',
    where: ['docker-compose.yml service <code>elasticsearch</code> at [[dc_elasticsearch]] (single node, security disabled, 512 MB heap)', 'observability/elasticsearch/ilm-policy.json — rollover at [[ilm_rollover]], delete at [[ilm_delete]]', 'filebeat.yml <code>setup.template.*</code> and <code>setup.ilm.*</code>'],
    why: 'Logs are for finding one specific event; that needs an index over every field, not a time series.',
    talks: ['Receives bulk writes from Filebeat', 'Queried by Kibana (and provisioned in Grafana)'],
    stores: 'Log documents on volume <code>elasticsearch-data</code>. ILM rolls the write index over at 1 day or 1 GB primary shard (or 10M docs), and deletes an index 7 days after rollover.',
    dies: 'Filebeat cannot send, so it retries and stops advancing its offset; logs wait in the Docker files. Kibana shows errors. Logs are only lost if the Docker file rotates away first. The app is unaffected.',
    viva: [
      { q: 'Where do the field types come from?', a: 'Filebeat loads its built-in ECS index template (renamed by <code>setup.template.name</code>), which defines ECS fields like <code>http.request.id</code> (keyword), <code>http.response.status_code</code> (long), <code>url.path</code> (wildcard), <code>message</code> (match_only_text). <code>event.duration_ms</code> is not an ECS field, so ES maps it dynamically from the JSON number — float. The types shown in the report were read back from the real index.' },
      { q: 'Why is the health check "yellow" rather than "green"?', a: 'Single node: every index has a replica that cannot be placed on a second node, so the cluster is yellow forever. Waiting for green would never finish.' },
      { q: 'When are logs deleted?', a: 'By ILM, not a cron: the hot phase rolls over daily (or at 1 GB), and the delete phase removes an index 7 days after rollover. <code>docker compose down -v</code> deletes the whole volume immediately.' },
    ],
  },
  {
    id: 'kibana', name: 'Kibana', x: 810, y: 410, group: 'logs',
    what: 'Search UI over Elasticsearch. A data view <code>filebeat-gym-tracker-*</code> and 8 saved searches are imported by a script.',
    where: ['observability/kibana/saved-objects.ndjson — data view + 8 saved searches', 'scripts/setup_kibana.sh — imports them via the saved objects API', 'docker-compose.yml service <code>kibana</code> at [[dc_kibana]]'],
    why: 'Where a human asks "what happened to request X?" — e.g. <code>http.request.id: "259fd665642941e0"</code>.',
    talks: ['Elasticsearch <code>http://elasticsearch:9200</code>'],
    stores: 'Only its saved objects (inside Elasticsearch). No volume of its own.',
    dies: 'No log search UI. Ingestion (Filebeat → ES) continues.',
    viva: [
      { q: 'How do you find every log line for one request?', a: 'Copy the <code>x-request-id</code> response header (or the id from any log line) and search <code>http.request.id: "&lt;id&gt;"</code> — saved search "04 - Trace one request by id". Because it is a keyword field, that is an exact term match.' },
      { q: 'How do you find errors?', a: 'Saved search 02: <code>log.level: ("error" or "warning" or "critical")</code>, check <code>error.type</code>, then search that line\'s request id to see the whole request.' },
    ],
  },
  {
    id: 'demo', name: 'cardinality-demo', x: 810, y: 30, group: 'experiment',
    what: 'A separate tiny HTTP server for Part E2 with two counters: one labelled by <code>request_id</code> (deliberately wrong), one by endpoint only. Capped at 100 unique ids.',
    where: ['observability/cardinality_demo.py — UNSAFE at [[c_unsafe]], SAFE at [[c_safe]], cap at [[c_cap]]', 'docker-compose.yml service <code>cardinality-demo</code> at [[dc_cardinality]] (profile <code>experiment</code>)', 'scripts/experiment_cardinality.sh'],
    why: 'To prove the cardinality rule without polluting the real app\'s metrics: it has its own registry, port (8001) and container.',
    talks: ['Scraped by Prometheus (job cardinality-demo)'],
    stores: 'In-memory counters + a Python <code>set</code> of seen ids.',
    dies: 'Nothing else is affected; it is not started by default.',
    viva: [
      { q: 'Why a separate app?', a: 'So the per-request series never get into the real app\'s metrics, and the experiment can be bounded (100 ids) and switched to safe mode with <code>CARDINALITY_SAFE_ONLY=true</code>.' },
    ],
  },
];

window.EDGES = [
  { from: 'app', to: 'postgres', label: 'SQL', kind: 'app' },
  { from: 'app', to: 'groq', label: 'HTTPS (POST /log only)', kind: 'app' },
  { from: 'prometheus', to: 'app', label: 'scrape /metrics', kind: 'metrics' },
  { from: 'prometheus', to: 'node', label: 'scrape :9100', kind: 'metrics' },
  { from: 'grafana', to: 'prometheus', label: 'PromQL', kind: 'metrics' },
  { from: 'prometheus', to: 'demo', label: 'scrape :8001', kind: 'metrics' },
  { from: 'app', to: 'docker', label: 'JSON lines on stdout', kind: 'logs' },
  { from: 'docker', to: 'filebeat', label: 'tail *-json.log', kind: 'logs' },
  { from: 'filebeat', to: 'es', label: 'bulk index', kind: 'logs' },
  { from: 'kibana', to: 'es', label: 'search', kind: 'logs' },
];

// ---------------------------------------------------------------------------
// Metrics: all 19, from metrics.py
// ---------------------------------------------------------------------------

window.METRIC_LIST = [
  // ---- COUNTERS
  {
    name: 'gym_http_requests_total', type: 'counter', kind: 'App', unit: 'requests', labels: ['method', 'route', 'status'],
    attr: 'METRICS.http_requests_total', decl: 'm_http_requests_total', updates: ['ok_count', 'exc_count'],
    purpose: 'Every HTTP response, split by method, route template and status code. Numerator and denominator of the error rate; basis of requests per second.',
    q: 'sum by (route) (rate(gym_http_requests_total[1m]))', src: 'observability/grafana/dashboards/01-app-performance.json',
    deep: true,
    example: 'gym_http_requests_total{method="GET",route="/login",status="200"} 718.0\ngym_http_requests_total{method="GET",route="/",status="303"} 360.0\ngym_http_requests_total{method="GET",route="/healthz",status="200"} 396.0',
    exampleSrc: 'observability/results-rerun/01-baseline-metrics.txt (end of the E1 baseline stage)',
    changes: 'Goes up by 1 at the end of every request in the middleware (success path [[ok_count]]; exception path [[exc_count]] with status="500"). Never goes down. Resets to 0 only when the app process restarts.',
    whyType: 'It answers "how many, ever?" — a monotonic total. Rates are derived from it in PromQL with <code>rate()</code>, which handles restarts.',
    whyNot: 'A Gauge could go down and would not survive the restart logic of rate(). A Histogram/Summary is for distributions of a value, not for counting events.',
    labelsWhy: '<code>route</code> is the route <b>template</b> (e.g. <code>/weekly-report</code>) from <code>route_label()</code>, never the raw path; unmatched paths collapse to <code>"unmatched"</code> ([[m_unmatched]]) so 404 scanners cannot mint series. Injected faults that short-circuit get route <code>"injected"</code>.',
    shows: 'Request rate by route (timeseries), Responses by status code, and the Request rate and Error rate stats on the Application dashboard.',
    professor: [
      { q: 'How do you compute the error rate from this?', a: '<code>sum(rate(gym_http_requests_total{status=~"5.."}[5m])) / sum(rate(gym_http_requests_total[5m]))</code> — 5xx responses per second divided by all responses per second. That is exactly the HighErrorRate alert; the dashboard version adds <code>or vector(0)</code> and <code>clamp_min</code> so it shows 0 instead of "no data".' },
      { q: 'Why not label it with the raw URL path?', a: 'Raw paths are unbounded (/wp-login.php, /.env, ids in URLs) so every distinct path would be a new time series — a cardinality explosion driven by strangers. The raw path goes in the log (<code>url.path</code>); the metric gets the template (<code>http.route</code> label value).' },
    ],
  },
  {
    name: 'gym_http_exceptions_total', type: 'counter', kind: 'App', unit: 'exceptions', labels: ['route', 'exception'],
    attr: 'METRICS.http_exceptions_total', decl: 'm_http_exceptions_total', updates: ['exc_exceptions'],
    purpose: 'Requests that raised an unhandled exception, by exception class — tells a crash apart from a deliberate 500.',
    q: 'sum by (route, exception) (rate(gym_http_exceptions_total[5m])) or vector(0)', src: 'observability/grafana/dashboards/01-app-performance.json',
    whyType: 'Count of events.', labelsWhy: '<code>exception</code> is the class name (<code>type(exc).__name__</code>) — a small set in practice.',
  },
  {
    name: 'gym_workout_entries_total', type: 'counter', kind: 'Business', unit: 'entries', labels: ['outcome'],
    attr: 'METRICS.entries_total', decl: 'm_entries_total', updates: ['saved_inc', 'blocked_inc', 'dup_held_inc'],
    purpose: 'Journal entries that reached a terminal state at <code>POST /save</code>: <code>saved</code> (rows written), <code>blocked</code> (review form refused it), <code>duplicate_held</code> (duplicate guard asked first).',
    q: 'sum by (outcome) (increase(gym_workout_entries_total[5m]))', src: 'observability/grafana/dashboards/02-business.json',
    deep: true,
    example: 'gym_workout_entries_total{outcome="saved"} 0.0\ngym_workout_entries_total{outcome="blocked"} 0.0\ngym_workout_entries_total{outcome="duplicate_held"} 0.0',
    exampleSrc: 'observability/results-rerun/01-baseline-metrics.txt — all three exist at 0 before anything happens (zero-init)',
    changes: 'At POST /save exactly one of the three is incremented: blocked at [[blocked_inc]], duplicate_held at [[dup_held_inc]], saved at [[saved_inc]].',
    whyType: 'Events that happen; we want "how many per 5 minutes, by outcome" → <code>increase()</code> on a counter.',
    whyNot: 'A Gauge of "entries" would be a level, not a flow. A Histogram makes no sense: there is no value to distribute, only an outcome.',
    labelsWhy: 'One bounded label, <code>outcome</code>, with exactly three values declared in <code>ENTRY_OUTCOMES</code> ([[m_label_values]]). All three are created at 0 in <code>Metrics.__init__</code> ([[m_zero_init]]).',
    shows: '"Entry outcomes" timeseries and "Entries saved (24h)" stat on the Business dashboard.',
    professor: [
      { q: 'Why create the label values at 0 up front?', a: 'A labelled series only appears when first incremented, so Prometheus\'s first sample of it would already read 1 and <code>increase()</code> sees no rise — the dashboard would silently drop the first blocked save. That bug was found on the dashboard ("blocked 0" next to a blocked save) and fixed by zero-initialising; a test checks every series exists at 0.0.' },
      { q: 'Why does "Entries saved" show 4 in Screenshot 2 while 6 entries went through?', a: '4 saved + 1 duplicate_held + 1 blocked = 6. And per the report, earlier saves that day happened before the zero-init fix, so they did not show as a rise.' },
    ],
  },
  {
    name: 'gym_sets_written_total', type: 'counter', kind: 'Business', unit: 'sets', labels: [],
    attr: 'METRICS.sets_written_total', decl: 'm_sets_written_total', updates: ['sets_inc'],
    purpose: 'Individual sets inserted into the database — the product\'s unit of value. Entries vary in size, so sets are the honest measure of training recorded.',
    q: 'increase(gym_sets_written_total[24h])', src: 'observability/grafana/dashboards/02-business.json',
    deep: true,
    example: 'gym_sets_written_total 17.0',
    exampleSrc: 'Scraped at 05:12 UTC on 21 Sept (observability/results-rerun/metric-trace-sets-written.json)',
    changes: 'At a successful save, <code>METRICS.sets_written_total.inc(result.inserted_sets)</code> ([[sets_inc]]) adds the number of rows actually inserted. <code>inc(0)</code> is a no-op, so a bodyweight-only entry does not disturb it. Resets to 0 when the app restarts.',
    whyType: 'Total amount of something that only accumulates. <code>.inc(n)</code> adds n, not just 1.',
    whyNot: 'A Gauge would lose the meaning on restart and could not be safely <code>increase()</code>d. A Summary of sets (that exists separately: gym_sets_per_entry) gives an average per entry, not a total.',
    labelsWhy: 'No labels — a single series. A per-user or per-exercise label would be unbounded.',
    shows: '"Sets logged (24h)" stat (33 in Screenshot 2) and "Sets and bodyweight readings written" (per minute) on the Business dashboard.',
    professor: [
      { q: 'The counter reset to 0 at 05:21 — why does Grafana show 33, not 16?', a: '<code>increase()</code> detects that a counter value went down and treats it as a reset: it adds the rise before the reset (0→17) and the rise after it (0→16). 17 + 16 = 33. The query result was 33.0007 because increase() extrapolates slightly to the window edges.' },
      { q: 'Are all sets counted?', a: 'No — known limitation. Only the journal paste flow (<code>POST /save</code>) increments it. Sets logged from the newer live-workout screen (sessions.py / api.py) do not touch any metric. The report says so in Part A.' },
    ],
  },
  {
    name: 'gym_bodyweight_entries_written_total', type: 'counter', kind: 'Business', unit: 'readings', labels: [],
    attr: 'METRICS.bodyweight_written_total', decl: 'm_bodyweight_written_total', updates: ['bw_inc'],
    purpose: 'Bodyweight readings picked out of the same entries and inserted.',
    q: 'sum(rate(gym_bodyweight_entries_written_total[5m])) * 60', src: 'observability/grafana/dashboards/02-business.json',
    whyType: 'Count of inserted rows.', labelsWhy: 'None.',
  },
  {
    name: 'gym_duplicate_decisions_total', type: 'counter', kind: 'Business', unit: 'decisions', labels: ['decision'],
    attr: 'METRICS.duplicate_decisions_total', decl: 'm_duplicate_decisions_total', updates: ['dup_decision_held', 'overridden_inc'],
    purpose: 'How the duplicate-submission prompt was answered: <code>held</code> (guard stopped it) or <code>overridden</code> (person saved anyway).',
    q: 'sum by (decision) (increase(gym_duplicate_decisions_total[10m]))', src: 'observability/grafana/dashboards/02-business.json',
    whyType: 'Count of decisions.', labelsWhy: '<code>decision</code> ∈ DUPLICATE_DECISIONS = (held, overridden).',
  },
  {
    name: 'gym_llm_extractions_total', type: 'counter', kind: 'Business', unit: 'calls', labels: ['outcome'],
    attr: '_METRICS.llm_extractions_total', decl: 'm_llm_extractions_total', updates: ['p_llm_inc'],
    purpose: 'Groq calls by outcome: success, rate_limited, error.',
    q: 'sum by (outcome) (increase(gym_llm_extractions_total[5m]))', src: 'observability/grafana/dashboards/02-business.json',
    whyType: 'Count of calls.', labelsWhy: '<code>outcome</code> ∈ EXTRACTION_OUTCOMES.',
  },
  {
    name: 'gym_weekly_reports_total', type: 'counter', kind: 'Business', unit: 'reports', labels: ['narration'],
    attr: 'METRICS.weekly_reports_total', decl: 'm_weekly_reports_total', updates: ['weekly_inc'],
    purpose: 'Weekly reports generated, split by whether the LLM narration worked (<code>ok</code>/<code>failed</code>). A failed narration still produces correct numbers — a quality signal, not an error rate.',
    q: 'sum by (narration) (increase(gym_weekly_reports_total[1h]))', src: 'observability/grafana/dashboards/02-business.json',
    whyType: 'Count.', labelsWhy: '<code>narration</code> ∈ NARRATION_RESULTS.',
  },
  {
    name: 'gym_logins_total', type: 'counter', kind: 'App', unit: 'attempts', labels: ['result'],
    attr: 'METRICS.logins_total', decl: 'm_logins_total', updates: ['login_ok_inc', 'login_fail_inc', 'login_rl_inc'],
    purpose: 'Login attempts by result: success, failure, rate_limited. A spike in failure without success is the shape of credential stuffing.',
    q: 'sum by (result) (increase(gym_logins_total[5m]))', src: 'observability/grafana/dashboards/02-business.json',
    whyType: 'Count.', labelsWhy: 'No username label: it would be unbounded AND would disclose who has an account.',
  },
  {
    name: 'gym_faults_injected_total', type: 'counter', kind: 'App', unit: 'faults', labels: ['kind'],
    attr: 'self._metrics.faults_injected_total', decl: 'm_faults_injected_total', updates: ['f_record_inc'],
    purpose: 'Deliberately injected faults (Part E), by kind (latency/error). Proves from the metrics alone exactly when the fault was live.',
    q: 'sum by (kind) (increase(gym_faults_injected_total[1m]))', src: 'observability/grafana/dashboards/01-app-performance.json',
    whyType: 'Count of injected faults.', labelsWhy: '<code>kind</code> ∈ FAULT_KINDS = (latency, error).',
  },
  // ---- GAUGES
  {
    name: 'gym_http_requests_in_flight', type: 'gauge', kind: 'App', unit: 'requests', labels: [],
    attr: 'METRICS.http_requests_in_flight', decl: 'm_http_requests_in_flight', updates: ['inflight_inc', 'inflight_dec'],
    purpose: 'Requests being served right now — the saturation signal on a single worker.',
    q: 'gym_http_requests_in_flight', src: 'observability/grafana/dashboards/01-app-performance.json',
    whyType: 'A level that goes up and down.',
    labelsWhy: 'None.',
    note: '<code>.dec()</code> is in a <code>finally</code> ([[inflight_dec]]) because a client disconnect raises <code>asyncio.CancelledError</code> (a BaseException) which neither the except nor the success path would catch — the gauge would leak upwards. Every recorded scrape shows <code>1.0</code>: that is the <code>/metrics</code> request counting itself while it is being served.',
  },
  {
    name: 'gym_llm_extractions_in_flight', type: 'gauge', kind: 'Business', unit: 'calls', labels: [],
    attr: '_METRICS.llm_extractions_in_flight', decl: 'm_llm_extractions_in_flight', updates: ['p_inflight_inc', 'p_inflight_dec'],
    purpose: 'Groq calls in progress. Exposed but <b>not charted</b> on any dashboard.',
    whyType: 'Level.', labelsWhy: 'None.',
  },
  {
    name: 'gym_review_drafts_open', type: 'gauge', kind: 'Business', unit: 'drafts', labels: [],
    attr: 'METRICS.review_drafts_open (via DRAFTS)', decl: 'm_review_drafts_open', updates: ['m_draft_publish', 'drafts_opened', 'drafts_closed'],
    purpose: 'Parsed entries shown for review and not yet saved — the brief\'s "orders waiting to be prepared".',
    q: 'gym_review_drafts_open', src: 'observability/grafana/dashboards/02-business.json',
    deep: true,
    example: 'gym_review_drafts_open 0.0',
    exampleSrc: 'every recorded scrape (no parses during the load tests); Screenshot 2 showed 2 (the held and blocked entries)',
    changes: 'Never incremented directly. <code>DraftTracker</code> keeps a dict <code>{(user_id, session_date): opened_at}</code>; <code>opened()</code> at POST /log ([[drafts_opened]]) adds a key, <code>closed()</code> at a successful POST /save ([[drafts_closed]]) removes it, and after each change the gauge is <code>.set(len(dict))</code> ([[m_draft_publish]]). Drafts older than 30 min (ttl 1800 s) are pruned; the dict is capped at 1000.',
    whyType: 'It is a backlog — a level that rises and falls. "How many right now?" is the gauge question.',
    whyNot: 'A Counter cannot go down. Two counters (opened/closed) would never balance because abandoned drafts never close — the gauge would drift upwards forever. That is why the TTL exists.',
    labelsWhy: 'None. user_id and session_date are <b>dict keys inside the process</b>, never labels; only the count is published.',
    shows: '"Drafts awaiting review" stat on the Business dashboard.',
    professor: [
      { q: 'What happens if someone parses an entry and closes the tab?', a: 'They never save, so closed() is never called. The entry is aged out after 30 minutes by <code>_prune</code>, so the gauge comes back down instead of counting it forever.' },
      { q: 'Why is the user id not a label here?', a: 'It is unbounded — one series per user. It is used only as a dict key to de-duplicate (re-parsing the same date does not double count); the gauge exports just the length.' },
      { q: 'Blocked and duplicate-held entries — do they close the draft?', a: 'No. Only the success path calls DRAFTS.closed(). That is why Screenshot 2 shows 2 open drafts: the held one and the blocked one.' },
    ],
  },
  {
    name: 'gym_build_info', type: 'gauge', kind: 'App', unit: '1', labels: ['version', 'commit'],
    attr: 'METRICS.build_info', decl: 'm_build_info', updates: ['m_set_build_info', 'app_build_info'],
    purpose: 'Always 1; the labels carry which build is running. Not charted.',
    example: 'gym_build_info{commit="unknown",version="1.0.0"} 1.0',
    whyType: 'The conventional "info" pattern: a gauge set to 1 whose labels are the payload.', labelsWhy: 'One series per build — bounded.',
  },
  // ---- HISTOGRAMS
  {
    name: 'gym_http_request_duration_seconds', type: 'histogram', kind: 'App', unit: 'seconds', labels: ['method', 'route'],
    attr: 'METRICS.http_request_duration', decl: 'm_http_request_duration', updates: ['ok_observe', 'exc_observe'],
    purpose: 'Wall-clock time to serve each request. The source of p50/p95/p99.',
    q: 'histogram_quantile(0.95, sum by (le) (rate(gym_http_request_duration_seconds_bucket[5m])))', src: 'observability/grafana/dashboards/01-app-performance.json',
    deep: true,
    example: 'gym_http_request_duration_seconds_bucket{le="0.005",method="GET",route="/login"} 191.0\ngym_http_request_duration_seconds_bucket{le="0.01",method="GET",route="/login"} 192.0\n... (0.025 … 0.5 all 192.0)\ngym_http_request_duration_seconds_bucket{le="0.75",method="GET",route="/login"} 243.0\ngym_http_request_duration_seconds_bucket{le="+Inf",method="GET",route="/login"} 243.0\ngym_http_request_duration_seconds_count{method="GET",route="/login"} 243.0\ngym_http_request_duration_seconds_sum{method="GET",route="/login"} 25.796399287001805',
    exampleSrc: 'observability/results-rerun/02-fault-metrics.txt — during the fault: 192 fast /login requests, then 51 that landed in the 0.5–0.75 s bucket (the delayed ones)',
    changes: 'At the end of each request the middleware calls <code>.labels(method, route).observe(seconds)</code> ([[ok_observe]], or [[exc_observe]] on exceptions). Inside prometheus_client that adds 1 to <b>every bucket whose upper bound le ≥ the value</b> (buckets are cumulative), adds 1 to <code>_count</code> and adds the value to <code>_sum</code>.',
    whyType: 'Latency needs a distribution, not a total — we want "95% of requests finished within X". Buckets let PromQL estimate any percentile over any window and sum across instances.',
    whyNot: 'Counter: total time is meaningless alone. Gauge: only the last value, misses everything between scrapes. Summary: the Python client\'s Summary only gives _sum/_count (no quantiles), and summary quantiles cannot be aggregated across instances anyway.',
    labelsWhy: '<code>method</code> and <code>route</code> (template) — bounded. Not status, to keep series count down. Buckets (seconds): 0.005, 0.01, 0.025, 0.05, 0.075, 0.1, 0.25, 0.5, 0.75, 1, 2.5, 5, 10 — fine resolution in 10 ms–1 s, room up to 10 s for the experiment ([[m_buckets_http]]).',
    shows: 'p95 and p99 stats, "Latency percentiles (5m window)" p50/p95/p99/mean, the heatmap, "p95 by route". Also the SlowRequests alert (p95 > 1 s per route for 2m).',
    professor: [
      { q: 'Why is gym_http_request_duration_seconds a histogram?', a: 'Because I need percentiles. It records each observation into buckets, so Prometheus can compute p95/p99 with <code>histogram_quantile</code> over any time window and across instances. A Counter or Gauge cannot give a distribution, and the Python client\'s Summary has no quantiles.' },
      { q: 'Where does the timer start, and why does that matter?', a: 'Before fault injection ([[timer_start]], then [[fault_call]]). The injected delay must fall inside the timed region, otherwise Prometheus would report a healthy service while clients waited — exactly the blind spot the experiment is about.' },
      { q: 'Is p95 exact?', a: 'No. It is an estimate: histogram_quantile finds the bucket containing the 95th percentile rank and linearly interpolates inside it. That is why the fault p95 was 687.5 ms, not 500 ms.' },
    ],
  },
  {
    name: 'gym_llm_extraction_duration_seconds', type: 'histogram', kind: 'Business', unit: 'seconds', labels: [],
    attr: '_METRICS.llm_extraction_duration', decl: 'm_llm_extraction_duration', updates: ['p_llm_observe'],
    purpose: 'Time for one Groq extraction including retries and any rate-limit wait. Buckets 0.25 s … 60 s.',
    q: 'histogram_quantile(0.95, sum by (le) (rate(gym_llm_extraction_duration_seconds_bucket[10m])))', src: 'observability/grafana/dashboards/02-business.json',
    whyType: 'Distribution of a duration.', labelsWhy: 'None.',
  },
  {
    name: 'gym_db_commit_duration_seconds', type: 'histogram', kind: 'App', unit: 'seconds', labels: [],
    attr: 'METRICS.db_commit_duration', decl: 'm_db_commit_duration', updates: ['commit_timer'],
    purpose: 'Time inside the committing transaction at POST /save. Separates "DB slow" from "request slow".',
    q: 'histogram_quantile(0.95, sum by (le) (rate(gym_db_commit_duration_seconds_bucket[5m])))', src: 'observability/grafana/dashboards/02-business.json',
    whyType: 'Distribution of a duration.', labelsWhy: 'None.',
    note: 'Recorded by the <code>observe_duration</code> context manager ([[m_observe_duration]]), which exists because call sites need the elapsed value for logs too and <code>Histogram.time()</code> does not hand it back. It observes in <code>__exit__</code> even when the block raises.',
  },
  // ---- SUMMARIES
  {
    name: 'gym_entry_text_bytes', type: 'summary', kind: 'Business', unit: 'bytes', labels: [],
    attr: 'METRICS.entry_text_bytes', decl: 'm_entry_text_bytes', updates: ['entry_bytes_observe'],
    purpose: 'Size of each pasted entry submitted to POST /log. Drives LLM token cost. Observes the <b>size</b>, never the text.',
    q: 'rate(gym_entry_text_bytes_sum[30m]) / clamp_min(rate(gym_entry_text_bytes_count[30m]), 0.0001)', src: 'observability/grafana/dashboards/02-business.json',
    whyType: 'The question is "what is the average size?" — Summary gives _sum and _count, and average = sum/count.', labelsWhy: 'None.',
  },
  {
    name: 'gym_sets_per_entry', type: 'summary', kind: 'Business', unit: 'sets', labels: [],
    attr: 'METRICS.sets_per_entry', decl: 'm_sets_per_entry', updates: ['spe_observe'],
    purpose: 'Sets written per saved entry. The metric explored "myself" in the report: catches a model that silently starts dropping rows.',
    q: 'rate(gym_sets_per_entry_sum[30m]) / clamp_min(rate(gym_sets_per_entry_count[30m]), 0.0001)', src: 'observability/grafana/dashboards/02-business.json',
    deep: true,
    example: 'gym_sets_per_entry_count 0.0\ngym_sets_per_entry_sum 0.0',
    exampleSrc: 'recorded scrapes (no saves during load tests). In Screenshot 2 the average read 5.5.',
    changes: 'At a successful save, <code>.observe(result.inserted_sets)</code> ([[spe_observe]]) adds 1 to <code>_count</code> and inserted_sets to <code>_sum</code>. E.g. entries of 4, 7 and 5 sets → count 3, sum 16, average 5.33.',
    whyType: 'Average is the question, distribution is not. Summary is the cheapest type that yields sum and count.',
    whyNot: 'A Histogram would also work but needs buckets and many series for no extra value here. A Counter of sets alone (gym_sets_written_total) cannot tell you sets <i>per entry</i> without dividing by entries; Summary carries both halves in one metric.',
    labelsWhy: 'None.',
    shows: '"Average sets per saved entry" timeseries on the Business dashboard (5.5 in Screenshot 2).',
    professor: [
      { q: 'Why is this useful when you already count sets and entries?', a: 'If extraction started dropping rows, entries saved and sets written would both still look normal-ish, but the average per entry would fall. It is a quiet-failure detector.' },
      { q: 'Can you get p95 sets per entry from this?', a: 'No. The Python client\'s Summary exposes only _sum and _count — no quantiles. If I needed a percentile it would have to be a Histogram.' },
      { q: 'What does clamp_min do in that query?', a: 'Avoids dividing by zero when there were no saves in the window: the denominator becomes at least 0.0001, so the panel shows 0 instead of NaN.' },
    ],
  },
];

// ---------------------------------------------------------------------------
// Logs
// ---------------------------------------------------------------------------

window.LOGS = [
  {
    id: 'served', title: 'request served', level: 'info', priority: 1, ref: 'log_served',
    why: 'One line per request, from the one place every request passes. It is the per-event twin of the request metrics.',
    extra: ['http.request.method', 'url.path (raw path)', 'http.route (template)', 'http.response.status_code (int)', 'event.duration_ms (float, rounded to 0.1)'],
    snippet: 'mw_success_tail',
    sample: '{"@timestamp": "2026-09-22T09:12:17.329Z", "log.level": "info", "message": "request served", "service.name": "gym-tracker", "service.version": "1.0.0", "event.dataset": "gym_tracker.app", "log.logger": "gym_tracker.app", "log.origin.file.name": "app.py", "log.origin.file.line": 463, "log.origin.function": "log_requests", "process.pid": 1, "http.request.id": "259fd665642941e0", "http.request.method": "GET", "url.path": "/login", "http.route": "/login", "http.response.status_code": 200, "event.duration_ms": 191.5}',
    sampleSrc: 'observability/results-rerun/log-trace-example.json (real, 22 Sept)',
    kibana: 'http.request.id: "259fd665642941e0"',
    savedSearch: '01 - All gym-tracker logs; 03 - Slow requests (event.duration_ms > 500); 04 - Trace one request by id',
    professor: { q: 'Why log the raw path but label the metric with the route template?', a: 'Logs are indexed per event and can hold a specific value; a metric label creates a series per distinct value. So /login/xyz-specific detail goes in the log, the template goes on the metric.' },
  },
  {
    id: 'failed', title: 'request failed', level: 'error', priority: 2, ref: 'log_failed',
    why: 'Unhandled exceptions, tied to the request they belong to via the request id. <code>logger.exception</code> attaches exc_info, so the formatter adds <code>error.type</code>, <code>error.message</code> and <code>error.stack_trace</code> (all scrubbed).',
    extra: ['http.request.method', 'url.path', 'http.route', 'http.response.status_code = 500', 'event.duration_ms', 'error.type / error.message / error.stack_trace'],
    snippet: 'mw_exception',
    sample: '{"@timestamp": "…", "log.level": "error", "message": "request failed", "http.request.id": "…", "http.request.method": "POST", "url.path": "/save", "http.route": "/save", "http.response.status_code": 500, "event.duration_ms": 12.3, "error.type": "OperationalError", "error.message": "…", "error.stack_trace": "Traceback …"}',
    sampleSrc: 'ILLUSTRATIVE shape built from the code (no recorded failure in the results folder); the values are made up, the fields are real',
    kibana: 'log.level: "error"  → then  http.request.id: "<that id>"',
    savedSearch: '02 - Errors and warnings',
    professor: { q: 'How do you get from an error to the whole request?', a: 'Search log.level: "error", read error.type and the request id on that line, then search http.request.id for that id — every line the request produced shares it because the id lives in a contextvar.' },
  },
  {
    id: 'saved', title: 'journal entry saved / drafted', level: 'info', priority: 3, ref: 'log_saved',
    why: 'The core business events. Drafted at POST /log ([[log_drafted]]), saved at POST /save ([[log_saved]]). They log ids and counts — never the journal text.',
    extra: ['event.action = entry_saved / entry_drafted', 'user.id', 'session.date', 'entry.inserted_sets / entry.sets', 'entry.text_bytes (size, not content)', 'entry.flagged / entry.blocked / entry.failed'],
    snippet: 'save_success',
    sample: '{"log.level": "info", "message": "journal entry saved", "event.action": "entry_saved", "user.id": 1, "session.date": "2026-09-21", "entry.replace": false, "entry.inserted_sets": 8, "entry.inserted_bodyweight": 1, "entry.skipped": 0, "entry.review_items": 0, "entry.replaced": 0, "http.request.id": "…"}',
    sampleSrc: 'ILLUSTRATIVE values; field names copied from the extra={…} at the call site',
    kibana: 'event.action: "entry_saved"',
    savedSearch: '05 - Workout entry events',
    professor: { q: 'Why not log the journal text?', a: 'It is health data. The policy is that call sites log ids and counts (entry.text_bytes, entry.sets). As a backstop, raw_text is in SENSITIVE_KEYS so it would be redacted if anyone passed it.' },
  },
  {
    id: 'login', title: 'login succeeded / failed / rate limited', level: 'info / warning', priority: 4, ref: 'log_login_fail',
    why: 'Security signal. Failures and rate limits are warnings ([[log_login_fail]], [[log_login_rl]]); success is info ([[log_login_ok]]). <b>No username</b> in failure lines — which account was targeted is personal data and would reveal which usernames exist.',
    extra: ['event.action = auth_failed / auth_rate_limited / login', 'user.id (success only)'],
    snippet: 'login_paths',
    sample: '{"log.level": "warning", "message": "login failed", "event.action": "auth_failed", "http.request.id": "…"}',
    sampleSrc: 'shape from the code',
    kibana: 'event.action: ("auth_failed" or "auth_rate_limited")',
    savedSearch: '06 - Authentication failures',
    professor: { q: 'You said no personal data, but "login succeeded" logs user.id. Why?', a: 'It is an internal numeric id, not a username, email or password — enough to correlate, not to identify someone from the log alone. The failure lines carry no identifier at all.' },
  },
  {
    id: 'fault', title: 'injected fault', level: 'warning', priority: 5, ref: 'f_latency_log',
    why: 'Makes the Part E fault visible in logs: one warning per delayed request with its sequence number. Plus one "FAULT INJECTION ARMED" warning at startup ([[f_armed_log]]).',
    extra: ['fault.kind = latency / error', 'fault.sequence', 'fault.latency_ms', 'url.path'],
    snippet: 'f_before',
    sample: '{"log.level": "warning", "message": "injected fault: delaying this request deliberately", "fault.kind": "latency", "fault.sequence": 5, "fault.latency_ms": 500.0, "url.path": "/healthz", "http.request.id": "…"}',
    sampleSrc: 'shape from the code',
    kibana: 'fault.kind: *',
    savedSearch: '07 - Injected faults (Part E)',
    professor: { q: 'How did you count the fault logs?', a: 'Saved search 07 (fault.kind: *) over each stage window: 0 before, 118 during, 0 after — matching increase(gym_faults_injected_total[2m]) ≈ 118.' },
  },
  {
    id: 'llm', title: 'extraction attempt failed / unavailable', level: 'warning', priority: 6, ref: 'p_attempt_failed',
    why: 'Why the LLM step failed: per attempt ([[p_attempt_failed]]) with attempt number and the error message (scrubbed); or "extraction unavailable" when GROQ_API_KEY is unset ([[p_unavailable]]).',
    extra: ['event.action = llm_attempt_failed / extraction_unavailable', 'llm.attempt', 'llm.attempts_total', 'llm.json_mode', 'error.message'],
    snippet: 'p_extract_loop',
    sample: '{"log.level": "warning", "message": "extraction attempt failed", "event.action": "llm_attempt_failed", "llm.attempt": 1, "llm.attempts_total": 2, "llm.json_mode": true, "error.message": "…", "http.request.id": "…"}',
    sampleSrc: 'shape from the code',
    kibana: 'event.action: "llm_attempt_failed"',
    savedSearch: '08 - LLM extraction failures',
    professor: { q: 'Could the Groq error message leak the API key?', a: 'The formatter runs every extra value through scrub_value, and string values through scrub_text, whose patterns redact gsk_… keys and Bearer tokens.' },
  },
];

// The six stages of one real log line. Representations copied from
// observability/results-rerun/log-trace-example.json.
window.LOG_TRACE = [
  {
    title: 'Application', who: 'app.py log_requests middleware + logging_setup.EcsJsonFormatter',
    rep: 'logger.info(\n    "request served",\n    extra={\n        "http.request.method": "GET",\n        "url.path": "/login",\n        "http.route": "/login",\n        "http.response.status_code": 200,\n        "event.duration_ms": 191.5,\n    },\n)',
    changed: 'A Python LogRecord is created. The <code>extra</code> dict becomes attributes on the record.',
    why: 'The call site only supplies event-specific fields. Time, level, service name and request id are added centrally so no call site can forget them.',
    where: '[[log_served]]',
  },
  {
    title: 'stdout', who: 'EcsJsonFormatter.format → json.dumps; StreamHandler(sys.stdout)',
    rep: '{"@timestamp": "2026-09-22T09:12:17.329Z", "log.level": "info", "message": "request served", "service.name": "gym-tracker", "service.version": "1.0.0", "event.dataset": "gym_tracker.app", "log.logger": "gym_tracker.app", "log.origin.file.name": "app.py", "log.origin.file.line": 463, "log.origin.function": "log_requests", "process.pid": 1, "http.request.id": "259fd665642941e0", "http.request.method": "GET", "url.path": "/login", "http.route": "/login", "http.response.status_code": 200, "event.duration_ms": 191.5}',
    changed: 'The record became ONE line of JSON with ECS names. Added: <code>@timestamp</code> (UTC, ms), <code>log.level</code>, <code>service.name</code>, <code>http.request.id</code> from the contextvar, origin file/line/function. Extras were promoted to top-level keys and passed through <code>scrub_value</code>. SensitiveDataFilter already scrubbed the message.',
    why: 'One object per line because Filebeat reads newline-delimited records. JSON because the producer is the only place that still knows the types (200 is an int, 191.5 a float).',
    where: '[[l_format]], [[l_add_rid]], [[l_dumps]]',
  },
  {
    title: 'Docker', who: 'Docker json-file logging driver',
    rep: '{"log":"{\\"@timestamp\\": \\"2026-09-22T09:12:17.329Z\\", \\"log.level\\": \\"info\\", \\"message\\": \\"request served\\", … \\"http.request.id\\": \\"259fd665642941e0\\", … \\"event.duration_ms\\": 191.5}\\n","stream":"stdout","time":"2026-09-22T09:12:17.332421916Z"}',
    changed: 'Docker wrapped our line as a <b>string</b> inside its own envelope: <code>log</code> (our JSON, escaped), <code>stream</code>, and <code>time</code> (when Docker received it — 3 ms after our timestamp). Appended to <code>/var/lib/docker/containers/&lt;id&gt;/&lt;id&gt;-json.log</code>.',
    why: 'That is simply how the json-file driver stores stdout. It is capped at 10 MB × 3 files.',
    where: '[[dc_logging]]',
  },
  {
    title: 'Filebeat', who: 'Filebeat container input + processors',
    rep: '1. container input   → strips {"log": …, "stream", "time"}, our JSON string is now the event\'s "message"\n2. decode_json_fields → parses "message", target "" (top level),\n                        overwrite_keys (our @timestamp and message win),\n                        expand_keys ("http.request.id" → http.request.id nested)\n3. add_docker_metadata → container.name = "gym-tracker", container.image.name, labels\n4. add_host_metadata   → host.*\n5. drop_fields         → agent.ephemeral_id, ecs.version, input.type, log.offset',
    changed: 'Text became structured fields. Dotted keys became nested objects (http → request → id). Container metadata added. Our <code>message</code> ("request served") replaced the raw JSON string because of overwrite_keys.',
    why: 'Without decode_json_fields, a search for a request id would be a full-text match against a blob. With it, it is a term query against an indexed keyword field.',
    where: '[[fb_decode]], [[fb_docker_meta]]',
  },
  {
    title: 'Elasticsearch', who: 'Elasticsearch, using Filebeat\'s ECS index template + ILM',
    rep: 'index: .ds-filebeat-gym-tracker-logs-2026.09.22-000002\n\n@timestamp                 2026-09-22T09:12:17.329Z   date\nlog.level                  info                       keyword\nmessage                    request served             match_only_text\nhttp.request.id            259fd665642941e0           keyword\nurl.path                   /login                     wildcard\nhttp.response.status_code  200                        long\nevent.duration_ms          191.5                      float\ncontainer.name             gym-tracker                keyword',
    changed: 'Stored as a document in a backing index of the data stream. Each field got a <b>type</b>: numbers are numeric (range queries, charts), the request id is keyword (exact match), message is full-text.',
    why: 'Types decide what queries work. <code>event.duration_ms &gt; 500</code> only works because it is a number. Exact id lookup works because it is a keyword (not analysed/tokenised).',
    where: 'observability/results-rerun/log-trace-example.json',
  },
  {
    title: 'Kibana', who: 'Kibana Discover, data view filebeat-gym-tracker-*',
    rep: 'KQL:  http.request.id: "259fd665642941e0"\n\n→ 2 hits:\n  09:12:17.329  info  request served                         (gym_tracker.app)\n  09:12:17.332  info  172.28.77.1:36834 - "GET /login HTTP/1.1" 200   (uvicorn.access)',
    changed: 'Nothing is changed — Kibana only queries. Both lines share the id because uvicorn\'s access log runs in the same request context and was routed through our JSON handler.',
    why: 'This is the payoff: one search returns everything one request produced.',
    where: 'docs/screenshots/11-kibana-trace-login.png, saved search 04 ([[kb_trace]])',
  },
];

// The five stages of gym_sets_written_total. Values from
// observability/results-rerun/metric-trace-sets-written.json.
window.METRIC_TRACE = [
  {
    title: 'Code', who: 'app.py save_reviewed, after pipeline.commit_draft succeeds',
    rep: 'METRICS.sets_written_total.inc(result.inserted_sets)',
    detail: '<code>METRICS</code> is the module-level <code>Metrics()</code> singleton ([[m_singleton]]). <code>sets_written_total</code> is a prometheus_client <code>Counter</code> declared at [[m_sets_written_total]]. <code>result.inserted_sets</code> is the number of rows <code>commit_draft</code> actually inserted. <code>.inc(n)</code> adds n to a float held in process memory (thread-safe; nothing is sent anywhere). On 21 Sept two entries totalling 17 sets were saved → value 17.',
    value: '17 (in memory)', where: '[[sets_inc]]',
  },
  {
    title: '/metrics', who: 'GET /metrics → metrics.render_latest → prometheus_client.generate_latest',
    rep: '# HELP gym_sets_written_total Individual workout sets inserted into the database.\n# TYPE gym_sets_written_total counter\ngym_sets_written_total 17.0\n# HELP gym_sets_written_created Individual workout sets inserted into the database.\n# TYPE gym_sets_written_created gauge\ngym_sets_written_created 1.789990099633522e+09',
    detail: 'When asked, the app serialises every metric in the registry into the Prometheus text exposition format. Nothing is pushed; the value just sits there until someone GETs it. prometheus_client also emits a companion <code>gym_sets_written_created</code> gauge: the Unix time the counter was created (i.e. process start). The <code>_created</code> value shown is the real one from the baseline scrape; the 17.0 is the value scraped at 05:12 on 21 Sept.',
    value: 'gym_sets_written_total 17.0', where: '[[metrics_route]], [[m_render_latest]]',
  },
  {
    title: 'Prometheus scrape', who: 'Prometheus, job gym-tracker, every 15 s',
    rep: 'GET http://app:8000/metrics   (scrape_interval: 15s)\n→ attaches target labels: job="gym-tracker", instance="app:8000", service="gym-tracker"',
    detail: 'Prometheus pulls the page, parses each line, and attaches its own labels. If the scrape fails, <code>up</code> for that target is 0.',
    value: 'one sample per 15 s', where: '[[prom_job_app]]',
  },
  {
    title: 'Time series', who: 'Prometheus TSDB (volume prometheus-data, 15 days)',
    rep: 'gym_sets_written_total{instance="app:8000",job="gym-tracker",service="gym-tracker"}\n\n05:06:45  0\n05:12:30  17     ← two entries saved\n05:21:00  0      ← app restarted: in-memory counter reset\n05:21:45  3\n05:22:15  16',
    detail: 'One series (no labels of our own) = one list of (timestamp, value) pairs. The drop to 0 is the app process restarting — counters live in memory.',
    value: '0 → 17 → 0 → 3 → 16', where: 'observability/results-rerun/metric-trace-sets-written.json',
  },
  {
    title: 'PromQL', who: 'Grafana sends the panel query to Prometheus',
    rep: 'increase(gym_sets_written_total[24h])',
    detail: '<code>[24h]</code> selects all samples of the last 24 hours. <code>increase()</code> walks them, and whenever a value is <b>lower</b> than the previous one it treats it as a counter reset and continues counting from 0. So the rises are 0→17 (+17) and 0→3→16 (+16). Total 33. The real answer was 33.00075 because increase() extrapolates slightly towards the window boundaries.',
    value: '17 + 16 = 33', where: '[[gf_sets_panel]]',
  },
  {
    title: 'Grafana', who: 'Business dashboard, stat panel "Sets logged (24h)"',
    rep: '┌──────────────────────┐\n│  Sets logged (24h)   │\n│         33           │\n└──────────────────────┘',
    detail: 'The stat panel shows the last value of that query. 33 is correct: 33 sets really were saved that day, even though the live counter only says 16.',
    value: '33 (Screenshot 2)', where: 'docs/screenshots/02-grafana-business.png',
  },
];

// ---------------------------------------------------------------------------
// PromQL queries (verified verbatim against the source file)
// ---------------------------------------------------------------------------

window.QUERIES = [
  {
    q: 'histogram_quantile(0.95, sum by (le) (rate(gym_http_request_duration_seconds_bucket[5m])))', src: 'observability/grafana/dashboards/01-app-performance.json',
    panel: 'Application → "p95 latency" stat and "Latency percentiles (5m window)"',
    parts: [
      ['gym_http_request_duration_seconds_bucket', 'The cumulative bucket counters of the latency histogram — one series per (method, route, le).'],
      ['[5m]', 'Range selector: for each series, take the samples from the last 5 minutes (≈20 samples at 15 s).'],
      ['rate( … )', 'Per-second increase of each bucket counter over those 5 minutes (handles restarts). Now each bucket says "requests per second that finished within le".'],
      ['sum by (le) ( … )', 'Add up across method and route, keeping only <code>le</code>. Result: one cumulative histogram for the whole app.'],
      ['histogram_quantile(0.95, … )', 'Find the bucket where 95% of the total is reached and linearly interpolate inside it. Output: one number, in seconds.'],
    ],
    out: 'One value in seconds, e.g. 0.6875 (687.5 ms) during the fault.',
  },
  {
    q: 'sum by (route) (rate(gym_http_requests_total[1m]))', src: 'observability/grafana/dashboards/01-app-performance.json',
    panel: 'Application → "Request rate by route"',
    parts: [
      ['gym_http_requests_total', 'Counter series, one per (method, route, status).'],
      ['[1m]', 'Last minute of samples (4 at 15 s) — reacts fast.'],
      ['rate( … )', 'Requests per second per series.'],
      ['sum by (route)', 'Collapse method and status, one line per route.'],
    ],
    out: 'One line per route, in requests/second.',
  },
  {
    q: '(sum(rate(gym_http_requests_total{status=~"5.."}[5m])) or vector(0)) / clamp_min(sum(rate(gym_http_requests_total[5m])), 0.0001)', src: 'observability/grafana/dashboards/01-app-performance.json',
    panel: 'Application → "Error rate (5xx)" stat',
    parts: [
      ['{status=~"5.."}', 'Label matcher with a regex: only status codes 500–599.'],
      ['sum(rate(…[5m]))', 'Total 5xx per second over 5 minutes.'],
      ['or vector(0)', 'If there are no 5xx series at all, use 0 instead of "no data".'],
      ['clamp_min(…, 0.0001)', 'Never divide by zero when there is no traffic.'],
      ['/', 'Share of responses that are 5xx (0–1).'],
    ],
    out: 'A fraction, shown as a percentage. 0 throughout E1 — the fault delayed, it did not fail.',
  },
  {
    q: 'increase(gym_sets_written_total[24h])', src: 'observability/grafana/dashboards/02-business.json',
    panel: 'Business → "Sets logged (24h)" stat',
    parts: [
      ['gym_sets_written_total', 'One counter series.'],
      ['[24h]', 'Every sample from the last day.'],
      ['increase( … )', 'How much it went up in that window, treating any drop as a counter reset. Equivalent to rate × window length.'],
    ],
    out: '33 in Screenshot 2 (17 before a restart + 16 after).',
  },
  {
    q: 'sum by (outcome) (increase(gym_workout_entries_total[5m]))', src: 'observability/grafana/dashboards/02-business.json',
    panel: 'Business → "Entry outcomes"',
    parts: [
      ['increase(…[5m])', 'Entries per 5 minutes, per outcome series.'],
      ['sum by (outcome)', 'One line each for saved, blocked, duplicate_held.'],
    ],
    out: 'Counts per 5-minute window. Only works for the first event because the series are zero-initialised.',
  },
  {
    q: 'rate(gym_sets_per_entry_sum[30m]) / clamp_min(rate(gym_sets_per_entry_count[30m]), 0.0001)', src: 'observability/grafana/dashboards/02-business.json',
    panel: 'Business → "Average sets per saved entry"',
    parts: [
      ['rate(…_sum[30m])', 'Sets per second being added to the sum.'],
      ['rate(…_count[30m])', 'Entries per second being observed.'],
      ['/', 'Sets per entry over the last 30 minutes (the "per second" cancels out).'],
    ],
    out: '5.5 in Screenshot 2.',
  },
  {
    q: 'count(count by (cpu) (node_cpu_seconds_total{mode="idle"}))', src: 'observability/grafana/dashboards/03-host-node-exporter.json',
    panel: 'Host → "Cores"',
    parts: [
      ['node_cpu_seconds_total{mode="idle"}', 'One series per CPU core for idle time.'],
      ['count by (cpu)', 'Group by core → one entry per core.'],
      ['count( … )', 'How many cores. (count() counts series — the same function used for the cardinality experiment.)'],
    ],
    out: '12 on the WSL2 VM.',
  },
  {
    q: '1 - avg(rate(node_cpu_seconds_total{mode="idle"}[5m]))', src: 'observability/grafana/dashboards/03-host-node-exporter.json',
    panel: 'Host → "CPU busy"',
    parts: [
      ['rate(node_cpu_seconds_total{mode="idle"}[5m])', 'Fraction of each second each core spent idle.'],
      ['avg( … )', 'Average across cores.'],
      ['1 - …', 'Busy fraction.'],
    ],
    out: '2.98% in Screenshot 3.',
  },
  {
    q: 'sum by (le) (increase(gym_http_request_duration_seconds_bucket[1m]))', src: 'observability/grafana/dashboards/01-app-performance.json',
    panel: 'Application → "Latency distribution" heatmap',
    parts: [
      ['increase(…_bucket[1m])', 'How many requests landed at or below each le in each minute.'],
      ['sum by (le)', 'Whole app, one value per bucket boundary → heatmap rows.'],
    ],
    out: 'During the fault it splits into two bands: fast requests and the 0.5–0.75 s ones.',
  },
  {
    q: 'increase(gym_faults_injected_total[5m]) > 0', src: 'observability/prometheus/alerts.yml',
    panel: 'Alert FaultInjectionActive (for: 1m)',
    parts: [
      ['increase(…[5m])', 'Faults injected in the last 5 minutes.'],
      ['> 0', 'Comparison filter: keep only series where that is positive. Any result = alert condition true.'],
    ],
    out: 'Fires during the fault stage. No Alertmanager, so it only shows on /alerts.',
  },
];
