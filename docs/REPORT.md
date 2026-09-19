# Assignment 1: Observability — Gym Tracker

Enterprise Software Development, Fall 2026

Adding metrics and logging to an existing application, then using them to
investigate two deliberately introduced problems.

**Contents**

- [Part A — The project](#part-a--the-project)
- [Part B — Metrics](#part-b--metrics)
- [Part C — Logs](#part-c--logs)
- [Part D — System design](#part-d--system-design)
- [Part E — Experiments](#part-e--experiments)
- [Running it yourself](#running-it-yourself)
- [What is verified, and what is not](#what-is-verified-and-what-is-not)
- [Credits](#credits)

---

## Part A — The project

### The problem

People who lift weights keep a training journal, and almost nobody keeps it in a
form a computer can read. A real entry looks like this:

```
Mon - chest
bench 60x8, 70x6, 70x5 (last one was a grind)
incline db press 22.5s x10 x10 x9
cable fly 3x12 light
bw 78.4
```

That is enough for the person who wrote it and useless to everything else.
Answering "is my bench press actually going up?" means reading back through
months of it by hand. The structured alternatives — apps that make you tap
through *exercise → sets → reps → weight* for every set — are accurate and so
tedious that people stop using them mid-session. The journal wins because it
costs nothing to write; it loses because nothing can read it.

### Intended users

One person's own training log, with accounts so several people can share one
deployment. Each account sees only its own training: every query is scoped by
`user_id`, ownership always comes from the session cookie and never from
anything the browser posted. It is not a social app, a coaching platform, or a
gym's membership system.

### The solution

A small FastAPI web app. Paste the journal entry exactly as written; a language
model extracts the structured rows; **you review every row before anything is
saved**; the saved data feeds progress charts and a weekly report.

The design decision the whole thing rests on: **the language model never
produces a number that reaches the database unseen.** It proposes rows, the
pipeline scores how well each is grounded in the source text, and the review
screen shows every prospective row with anything missing or implausible marked
in red. Nothing is written until a person has looked at it. The weekly report
works the same way — every figure is computed in code, and the model only writes
the prose around figures it did not calculate.

```
journal text ──▶ extract (LLM) ──▶ score & validate ──▶ REVIEW SCREEN ──▶ database
                                      (code)            (a human)          │
                                                                           ▼
                                                         progress charts, weekly report
```

### What works

| Capability | State |
|---|---|
| Paste a messy journal entry, get structured sets | Works |
| Review-before-save, with per-row confidence | Works |
| Bodyweight picked out of the same entry | Works |
| Fuzzy matching so "bench"/"Bench Press" stay one exercise | Works |
| Duplicate-submission guard that asks rather than refuses | Works |
| Per-user accounts, timezones, sessions | Works |
| Progress charts and a weekly report | Works |
| **Prometheus metrics, four types, app + business** | **Added for this assignment** |
| **Structured JSON logs → Filebeat → Elasticsearch → Kibana** | **Added for this assignment** |
| **Grafana dashboards, node-exporter, alert rules** | **Added for this assignment** |
| **Fault injection and the cardinality demo** | **Added for this assignment** |
| Live set-by-set logging, routines, PRs, measurements (installable PWA) | Added after this assignment |

1019 automated tests pass, 63 of them covering the observability layer added
here.

A second input method — an installable app for logging set by set during a
workout — was added after this assignment was written. It writes the same
`workout_logs` table, so every metric, log line and chart described below
covers it too, and its API routes are labelled by route template like the rest
(`/api/session/{session_id}/sets`), so it added endpoints without adding
cardinality. See the README for what it does.

### How to try it

See [Running it yourself](#running-it-yourself). In short:

```bash
docker compose up -d --build
./scripts/stack.sh status
python scripts/load_generator.py --duration 120 --rps 5
```

Then open Grafana at <http://localhost:3000> and Kibana at
<http://localhost:5601>.

---

## Part B — Metrics

### Setup

Prometheus scrapes; nothing pushes. Each target exposes `/metrics` and
Prometheus fetches it every 15 seconds. That direction matters: because
Prometheus initiates, a target that dies is *visible* — the synthetic `up`
series drops to 0 — whereas a push-based system simply stops receiving and
cannot tell silence from health.

| Component | Image | Port | Role |
|---|---|---|---|
| Application | built from `Dockerfile` | 8000 | exposes `/metrics` |
| Prometheus | `prom/prometheus:v2.54.1` | 9090 | scrapes and stores |
| Grafana | `grafana/grafana:11.2.0` | 3000 | queries and draws |
| node-exporter | `prom/node-exporter:v1.8.2` | 9100 | machine metrics |

Configuration: `observability/prometheus/prometheus.yml`, with alerting rules in
`observability/prometheus/alerts.yml`. Grafana's datasources and dashboards are
**provisioned from files** in `observability/grafana/`, not clicked together in
the UI — a dashboard that exists only in Grafana's database cannot be reviewed
in a diff and is lost with the volume.

Instrumentation lives in one module, `metrics.py`. Call sites import a name from
it; no metric is ever constructed inline. That is what keeps the table below
checkable against the code.

### The metric list

19 metrics. Unit "1" means a dimensionless count.

#### Counters — monotonic totals, always queried through `rate()`

| Metric | Purpose | Unit | Labels | Where recorded |
|---|---|---|---|---|
| `gym_http_requests_total` | Every response served. Numerator and denominator of the error rate. | 1 | `method`, `route`, `status` | `app.py:425` (middleware) |
| `gym_http_exceptions_total` | Requests that raised out of the handler. Distinguishes a crash from a deliberate 500. | 1 | `route`, `exception` | `app.py:399` |
| `gym_workout_entries_total` | **Business.** Journal entries reaching a terminal state at save. | 1 | `outcome` = `saved`/`blocked`/`duplicate_held` | `app.py:1210`, `1252`, `1275` |
| `gym_sets_written_total` | **Business.** Individual sets inserted — the product's unit of value. | 1 | — | `app.py:1278` |
| `gym_bodyweight_entries_written_total` | **Business.** Bodyweight readings inserted. | 1 | — | `app.py:1279` |
| `gym_duplicate_decisions_total` | **Business.** How the duplicate prompt was answered. | 1 | `decision` = `held`/`overridden` | `app.py:1227`, `1253` |
| `gym_llm_extractions_total` | **Business.** Groq extraction calls by outcome. | 1 | `outcome` = `success`/`rate_limited`/`error` | `pipeline.py:1042` |
| `gym_weekly_reports_total` | **Business.** Reports generated, by narration success. | 1 | `narration` = `ok`/`failed` | `app.py:1380` |
| `gym_logins_total` | Authentication outcomes. Failures without successes = credential stuffing. | 1 | `result` = `success`/`failure`/`rate_limited` | `app.py:729`, `752`, `767` |
| `gym_faults_injected_total` | Part E only. Proves from metrics alone when a fault was live. | 1 | `kind` = `latency`/`error` | `faults.py:138` |

#### Gauges — a value that goes up and down

| Metric | Purpose | Unit | Labels | Where recorded |
|---|---|---|---|---|
| `gym_http_requests_in_flight` | Concurrent requests. The saturation signal on a single worker. | 1 | — | `app.py:353` / `392` / `421` |
| `gym_llm_extractions_in_flight` | Extractions waiting on Groq right now. | 1 | — | `pipeline.py:1175` / `1212` |
| `gym_review_drafts_open` | **Business.** Entries parsed but not yet confirmed. | 1 | — | `metrics.py:269` (`DraftTracker` at `453`), driven from `app.py` |
| `gym_build_info` | Always 1; labels carry the build identity. | 1 | `version`, `commit` | `metrics.py:277` / `359` |

`gym_review_drafts_open` is this app's version of the brief's *"orders waiting to
be prepared"*: it rises when an entry is parsed at `POST /log` and falls when it
is saved at `POST /save`. The interesting part is what happens when somebody
parses an entry and closes the tab. A naive counter would drift upward until the
process restarted, and a dashboard would read that as a real and growing
backlog — **a gauge that can only rise is worse than no gauge at all.** So an
open draft carries a timestamp and is aged out after 30 minutes, and the whole
map is capped so a burst cannot grow memory without bound (`metrics.py`,
`DraftTracker`).

#### Histograms — bucketed, so percentiles can be computed

| Metric | Purpose | Unit | Labels | Where recorded |
|---|---|---|---|---|
| `gym_http_request_duration_seconds` | The headline latency metric. | seconds | `method`, `route` | `app.py:393` / `422` |
| `gym_llm_extraction_duration_seconds` | **Business.** One Groq extraction, retries included. | seconds | — | `pipeline.py:1042` |
| `gym_db_commit_duration_seconds` | Time inside the committing transaction. | seconds | — | `app.py:1234` |

Bucket boundaries are chosen per metric, because the three live on different
scales (`metrics.py`): HTTP latency keeps resolution from 5ms to 10s, the LLM
from 0.25s to 60s, the database from 5ms to 5s. **Buckets are the resolution
limit of every percentile computed from them** — see the Part E result, where
this is visible in the numbers.

#### Summaries — client-side sum and count only

| Metric | Purpose | Unit | Labels | Where recorded |
|---|---|---|---|---|
| `gym_entry_text_bytes` | **Business.** Size of a pasted entry. Drives the token budget, so it is a cost metric too. | bytes | — | `app.py:1142` |
| `gym_sets_per_entry` | **Business.** Sets written per saved entry. | 1 | — | `app.py:1280` |

**On Python summaries.** The brief notes that Python summaries lack percentiles,
and they do: `prometheus_client` implements no streaming quantiles, so a Summary
exposes `_sum` and `_count` and nothing else. The average is therefore the only
question it can answer:

```promql
rate(gym_entry_text_bytes_sum[30m]) / rate(gym_entry_text_bytes_count[30m])
```

This is asserted in the test suite
(`test_summary_exposes_sum_and_count_but_no_quantiles`) rather than merely
stated, and it is the reason every p95 in this project comes from a Histogram.

### Dashboards and queries

Three provisioned dashboards, 41 panels, 52 queries.

**1. Application performance** (`gym-app-perf`) — the RED method: Rate, Errors,
Duration.

| Panel | Query | What it shows |
|---|---|---|
| Request rate | `sum(rate(gym_http_requests_total[1m]))` | Throughput. A counter is meaningless raw (it resets on restart); `rate()` is how it is always read. |
| Error rate | `sum(rate(gym_http_requests_total{status=~"5.."}[5m])) / clamp_min(sum(rate(gym_http_requests_total[5m])), 0.0001)` | 5xx share. `clamp_min` avoids a divide-by-zero making the panel read `NaN` when there is no traffic. |
| p95 latency | `histogram_quantile(0.95, sum by (le) (rate(gym_http_request_duration_seconds_bucket[5m])))` | 95% of requests in the last 5 minutes were faster than this. |
| p99 latency | same with `0.99` | The tail. |
| Latency percentiles | p50, p95, p99 and `sum(rate(..._sum[5m])) / sum(rate(..._count[5m]))` on one axis | **The most instructive panel here**: the mean sits below p95 and barely moves when a minority of requests get slow. |
| Latency distribution | `sum by (le) (increase(gym_http_request_duration_seconds_bucket[1m]))` (heatmap) | Every bucket over time. A latency fault appears as a second band before any percentile line moves far. |
| p95 by route | `histogram_quantile(0.95, sum by (le, route) (rate(gym_http_request_duration_seconds_bucket[5m])))` | Which route is slow. `/log` carries the LLM call and is expected to sit high. |
| In flight | `gym_http_requests_in_flight` | A gauge, read directly — no `rate()`. |
| Injected faults | `sum by (kind) (increase(gym_faults_injected_total[1m]))` | Zero except during Part E. |

**2. Business metrics** (`gym-business`) — what the product is doing, not how the
machine is.

| Panel | Query |
|---|---|
| Sets logged (24h) | `increase(gym_sets_written_total[24h])` |
| Entry outcomes | `sum by (outcome) (increase(gym_workout_entries_total[5m]))` |
| Drafts awaiting review | `gym_review_drafts_open` |
| Avg entry size | `rate(gym_entry_text_bytes_sum[30m]) / clamp_min(rate(gym_entry_text_bytes_count[30m]), 0.0001)` |
| Avg sets per entry | `rate(gym_sets_per_entry_sum[30m]) / clamp_min(rate(gym_sets_per_entry_count[30m]), 0.0001)` |
| Duplicate decisions | `sum by (decision) (increase(gym_duplicate_decisions_total[10m]))` |
| Extraction outcomes | `sum by (outcome) (increase(gym_llm_extractions_total[5m]))` |
| Extraction latency | `histogram_quantile(0.95, sum by (le) (rate(gym_llm_extraction_duration_seconds_bucket[10m])))` |
| Login attempts | `sum by (result) (increase(gym_logins_total[5m]))` |

**The metric I explored myself: `gym_sets_per_entry`.**

The obvious application metrics — request rate, error rate, latency — all look
perfectly healthy during the failure I actually worry about with this app. If
extraction quality regresses (a model change, a prompt change, a provider
swapping a model out from under you), the app returns 200 promptly every time
and writes *fewer rows*. Nothing in the RED metrics moves. `gym_sets_per_entry`
is the metric that does: a fall in average sets per entry while entry volume
holds steady means rows are being dropped in extraction. It is a Summary because
the average is genuinely the question — I do not need the distribution of set
counts, I need to know whether today's average is lower than last week's.

The alert built on the same idea is `SavesFailingReview`
(`observability/prometheus/alerts.yml`): more than half of save attempts being
blocked by the review form for 10 minutes. Nothing is down; the product has
stopped working. That distinction is the whole reason business metrics exist
alongside application ones.

### Percentiles and the time window

Every percentile in this project is over a **sliding 5-minute window** (10
minutes for the LLM, which is rarer and would otherwise have too few samples to
interpolate from). `histogram_quantile(0.95, sum by (le) (rate(..._bucket[5m])))`
reads: take the per-second rate of each bucket over the last 5 minutes, sum the
buckets across all label combinations, and interpolate the value below which 95%
of observations fall.

Two constraints worth stating, because both are easy to get wrong:

- **The window must hold at least four scrapes.** At a 15-second scrape interval,
  nothing below `[1m]` is trustworthy — `rate()` needs several points to fit.
- **A percentile cannot be averaged.** `avg(p95)` across instances is not the p95
  of the whole. Summing the *buckets* first and taking the quantile of the sum,
  as above, is correct; taking quantiles per instance and averaging them is not.

### Node Exporter — the machine

`prom/node-exporter:v1.8.2` reports CPU, memory, disk and network for the
**Docker host** — the laptop or VM running the stack — labelled
`machine="docker-host"`, `job="node-exporter"`. Its hostname appears as the
`instance` label on every series and in the legend of every panel on the host
dashboard.

It runs in the host's own network, PID and filesystem namespaces
(`network_mode: host`, `pid: host`, with `/proc`, `/sys` and `/` bind-mounted
read-only). That is the whole trick: without it the container would faithfully
report *its own* network interfaces and process table instead of the machine's.
Every container in this stack is a process on this one kernel, so their CPU and
memory use is included in these totals.

Because it is in the host network namespace, Prometheus — on a bridge network —
reaches it at `host.docker.internal:9100`, which `extra_hosts:
host.docker.internal:host-gateway` makes resolve on Linux as well as Docker
Desktop.

| Panel | Query | Note |
|---|---|---|
| CPU busy | `1 - avg(rate(node_cpu_seconds_total{mode="idle"}[5m]))` | There is no "CPU %" metric. It is derived from a counter of seconds spent idle. |
| CPU by mode | `avg by (mode) (rate(node_cpu_seconds_total[5m]))` | A high `iowait` band means the disk is the bottleneck, which a single CPU% number cannot tell you. |
| Memory used | `1 - (node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes)` | `MemAvailable`, not `MemFree` — `MemFree` excludes the page cache and makes a healthy machine look nearly full. |
| Disk used | `1 - (node_filesystem_avail_bytes{mountpoint="/"} / node_filesystem_size_bytes{mountpoint="/"})` | Elasticsearch stops accepting writes at its flood-stage watermark, so a full disk silently ends log ingestion. |
| Disk I/O time | `sum by (device) (rate(node_disk_io_time_seconds_total[5m]))` | Seconds of I/O per second. Approaching 1 = saturated, which throughput alone does not reveal. |
| Network | `sum by (device) (rate(node_network_receive_bytes_total{device!="lo"}[5m]))` | Loopback excluded. |

---

## Part C — Logs

### The pipeline

```
app writes JSON to stdout
   └─▶ Docker json-file driver appends to
       /var/lib/docker/containers/<id>/<id>-json.log
          └─▶ Filebeat tails the file, strips Docker's envelope,
              parses the app's JSON into real fields
                 └─▶ Elasticsearch indexes every field
                        └─▶ Kibana searches it
```

### What is logged, why, and where

The app already had good event logging in a `key=value` format. The change made
here was not *what* to log but *how*: `event=request method=POST path=/save
status=200` is readable in a terminal and miserable downstream, because every
consumer has to re-parse it with a grok pattern and one unescaped space inside a
value breaks that pattern silently. **Emitting JSON moves the parsing to the
producer, which is the only place that still knows the types.**

Logging is configured in one place, `logging_setup.py`, and installed at import
in `app.py`.

| Event | Level | Where | Why |
|---|---|---|---|
| `request served` | info | `app.py:429`, middleware | One line per request: method, route, status, duration. The spine everything else hangs off. |
| `request failed` | error | `app.py:404` | Unhandled exception, with type and stack trace. |
| `journal entry drafted` | info | `app.py` `/log` | How many sets were extracted, how many flagged — the extraction-quality record. |
| `journal entry saved` | info | `app.py` `/save` | What was actually written. The audit trail for a row appearing in the database. |
| `save blocked by the review form` | info | `app.py` `/save` | The review screen refused it; nothing written. |
| `duplicate submission held for a decision` | info | `app.py` `/save` | The guard asked before writing. |
| `login succeeded` / `login failed` / `login rate limited` | info / warning | `app.py` `/login` | Security signal. |
| `extraction attempt failed` | warning | `pipeline.py` | Which attempt, JSON mode or not, and the provider's error. |
| `weekly report generated` | info | `app.py` `/weekly-report` | Including whether narration failed. |
| `injected fault: ...` | warning | `faults.py` | Part E. A degraded process must never be quiet about it. |
| `FAULT INJECTION ARMED` | warning | `faults.py`, at startup | Impossible to read the log stream and not notice. |

Levels are used as a decision, not decoration: **warning** means a human would
want to know eventually, **error** means something broke. A failed login is a
warning; a failed extraction attempt that will be retried is a warning; an
unhandled exception is an error.

### Field names

Elastic Common Schema (ECS), so Kibana's built-in columns and field groups work
without custom mappings:

| Field | Meaning |
|---|---|
| `@timestamp` | ISO-8601 UTC, milliseconds — **when the event happened** |
| `log.level` | severity |
| `message` | a short human sentence, not a `key=value` blob |
| `service.name` / `service.version` | which service, which build |
| `event.dataset` | which logger produced it |
| `http.request.id` | **the correlation id** |
| `http.request.method`, `url.path`, `http.route` | request identity |
| `http.response.status_code` | integer, so `> 499` is a valid query |
| `event.duration_ms` | float, so `> 500` is a valid query |
| `error.type`, `error.message`, `error.stack_trace` | on exceptions |

Storing the duration as a **number** rather than as text inside a message is the
entire difference between `event.duration_ms > 500` and a hopeless full-text
search. That is what "turns logs into data" in practice.

### Request IDs

`logging_setup.request_id_var` is a `ContextVar` set once per request by the
middleware. Every log line emitted while handling that request — from any
module, at any depth — picks it up automatically, so one Kibana search returns
the whole story of a single click.

A `ContextVar` rather than a thread-local **because the app is async**: several
requests share one thread, and a thread-local would cross their ids over.

The id is returned to the caller in the `x-request-id` response header, so a
person reporting a problem has the exact token to search for. An inbound
`x-request-id` is reused, so one identifier follows a request across process
boundaries — but it is validated first: the header is attacker-controlled and
ends up in every log line for that request, so anything longer than 64
characters or outside `[A-Za-z0-9._:-]` is **discarded and replaced**, not
sanitised.

### Never logged

The app handles a person's training journal, which is health data, plus
passwords and API keys.

The **policy** is that call sites log ids and counts, never content. The
**backstop** is `SensitiveDataFilter`, installed on the handler rather than on a
logger so it also covers library output — a dependency that helpfully logs the
request it just made with the `Authorization` header attached is exactly the case
it catches. It redacts:

- Values under known-sensitive keys: `password`, `token`, `session`, `cookie`,
  `authorization`, `api_key`, `secret`, `database_url`, and **`raw_text` /
  `raw_source`** — the journal text itself.
- Secret-*shaped* values anywhere in a message: Groq keys (`gsk_…`),
  `Bearer`/`Basic` credentials, and credentials in a URL's userinfo
  (`postgres://user:pass@host`).

Journal text is never logged at any level. Its **length** is recorded instead
(`entry.text_bytes`), which is the operationally useful part — it drives the
token budget — and discloses nothing. Login failures record *that* a login
failed, never which username was tried: which account was targeted is personal
data, and logging it would turn the log into a list of who has an account.

Eight tests cover this, including the recursive case and exception text.

### How Filebeat collects and parses

`observability/filebeat/filebeat.yml`.

Docker's json-file driver wraps every stdout line in its own envelope:

```json
{"log":"{\"@timestamp\":\"2026-09-15T06:07:28.710Z\",\"message\":\"request served\",...}\n",
 "stream":"stdout","time":"2026-09-15T06:07:28.710Z"}
```

Two steps unwrap it:

1. **The `container` input** strips Docker's envelope, leaving the application's
   own JSON *as a string* in `message`.
2. **`decode_json_fields`** parses that string and promotes its keys to the top
   level of the event. This is the step that turns text into data: without it a
   search for a request id is a full-text match against a blob; with it, it is a
   term query against an indexed field.

```yaml
- decode_json_fields:
    fields: ["message"]
    target: ""            # promote to the root, so log.level is log.level
    overwrite_keys: true  # the app's @timestamp wins over Filebeat's read time
    add_error_key: true   # a malformed line gets error.message, is not dropped
    expand_keys: true     # "http.request.id" becomes a nested object
```

`overwrite_keys: true` matters more than it looks: Filebeat's own `@timestamp` is
*when the line was read*, the app's is *when the event happened*. The two differ
by the shipping delay, which is exactly the error you do not want when
correlating a log against a metric spike.

`add_error_key: true` means a line that is not valid JSON gets an `error.message`
field rather than vanishing. Silently dropping malformed logs is how a parsing
bug hides for months.

**Autodiscover, not a static path.** Containers get new ids every time they are
recreated, so a hardcoded path breaks on the first `docker compose up --build`.
Filebeat watches the Docker API and starts a harvester when a container carrying
the label `co.elastic.logs/enabled=true` appears. Only the app carries that
label — deliberately. Elasticsearch and Kibana are chatty, and indexing their
logs into the same cluster is a feedback loop: an Elasticsearch error produces a
log line, which is indexed by Elasticsearch, which can produce another error.

`add_docker_metadata` then attaches which container, image and compose service
produced each line.

*(No Logstash. The brief marks it optional, and it earns its place when you need
enrichment, routing or buffering that Beats cannot do. Here the app already
emits correct JSON, so a Logstash hop would add a process, a queue and a config
language for no transformation.)*

### Where logs live, what survives a restart, when they are deleted

Three separate lifetimes, routinely confused:

| Stage | Lives in | Bounded by | Survives restart? |
|---|---|---|---|
| The file | `/var/lib/docker/containers/<id>/` on the host | json-file driver: `max-size: 10m`, `max-file: 3` (30MB/container) | Yes, until the container is removed |
| Filebeat's position | `filebeat-data` volume | — | **Yes** — this is why a restart resumes instead of re-indexing everything |
| The document | `elasticsearch-data` volume | **ILM policy: deleted 7 days after rollover** | Yes |

The log rotation cap is not optional. Without `max-size`/`max-file` the
json-file driver grows without limit, and a long experiment can fill the host
disk — the failure mode where the monitoring takes down the thing it monitors.
Note that Docker deletes the oldest file whether or not Filebeat has read it,
which is why Filebeat needs a persistent registry and a cluster that stays up.

**Retention** is `observability/elasticsearch/ilm-policy.json`, enforced by
Elasticsearch itself rather than by a cron job:

- **Hot:** roll over at 1GB, 10M documents, or 1 day — whichever comes first.
- **Delete:** 7 days after rollover.

Writes always go to the newest index, so deleting an old one never interrupts
ingestion. Deleting by whole index is also far cheaper than deleting individual
documents, which in Lucene only marks them and reclaims nothing until a merge.

Seven days suits a course project. Production retention is a legal and financial
question, not a technical one: long enough to investigate an incident nobody
noticed for a week, short enough that the storage bill and the personal-data
exposure stay bounded.

`docker compose down` keeps all of it; `docker compose down -v` deletes it.

### Searching in Kibana

Nine saved objects are imported by `./scripts/setup_kibana.sh` — a data view
over `filebeat-gym-tracker-*` plus eight saved searches.

| Search | Query (KQL) |
|---|---|
| All gym-tracker logs | `service.name: "gym-tracker"` |
| Errors and warnings | `service.name: "gym-tracker" and log.level: ("error" or "warning" or "critical")` |
| Slow requests | `event.duration_ms > 500` |
| **Trace one request** | `http.request.id: "<id>"` |
| Workout entry events | `event.action: ("entry_saved" or "entry_drafted" or "save_blocked" or "duplicate_decision_offered")` |
| Authentication failures | `event.action: ("auth_failed" or "auth_rate_limited")` |
| Injected faults | `fault.kind: *` |
| LLM extraction failures | `event.action: "llm_attempt_failed"` |

**To find one request:**

```bash
curl -si http://localhost:8000/healthz | grep -i x-request-id
#  x-request-id: f1d4bb6745b64b3d
```

then in Discover: `http.request.id: "f1d4bb6745b64b3d"`, sorted ascending.

**To find an error:** `log.level: "error"`, add `error.type` as a column, pick
one, then pivot to its `http.request.id` to see everything else that request did.
This is the payoff of correlation: you go from "something threw a
`ValidationError`" to the complete story of the click that caused it in one hop.

### Worked example: one log, and what is stored

**1. The original line, as the app writes it to stdout** (real, from
`observability/results/02-fault-app.log`):

```json
{"@timestamp": "2026-09-15T06:07:28.710Z", "log.level": "info", "message": "request served", "service.name": "gym-tracker", "service.version": "1.0.0", "event.dataset": "gym_tracker.app", "log.logger": "gym_tracker.app", "log.origin.file.name": "app.py", "log.origin.file.line": 429, "log.origin.function": "log_requests", "process.pid": 3124, "http.request.id": "f1d4bb6745b64b3d", "http.request.method": "GET", "url.path": "/healthz", "http.route": "/healthz", "http.response.status_code": 200, "event.duration_ms": 4.1}
```

**2. As Docker stores it** — the same line wrapped in the driver's envelope:

```json
{"log":"{\"@timestamp\": \"2026-09-15T06:07:28.710Z\", ... \"event.duration_ms\": 4.1}\n","stream":"stdout","time":"2026-09-15T06:07:28.710Z"}
```

**3. As Elasticsearch indexes it** — envelope stripped, JSON parsed into fields,
container metadata attached:

| Field | Value | Type |
|---|---|---|
| `@timestamp` | `2026-09-15T06:07:28.710Z` | date |
| `log.level` | `info` | keyword |
| `message` | `request served` | text |
| `service.name` | `gym-tracker` | keyword |
| `http.request.id` | `f1d4bb6745b64b3d` | keyword |
| `http.request.method` | `GET` | keyword |
| `url.path` | `/healthz` | keyword |
| `http.response.status_code` | `200` | **long** |
| `event.duration_ms` | `4.1` | **float** |
| `container.name` | `gym-tracker` | keyword *(added by Filebeat)* |
| `container.image.name` | `gym-tracker-observability-app` | keyword *(added by Filebeat)* |
| `host.name` | *the Docker host* | keyword *(added by Filebeat)* |

**4. A working search:**

```
http.request.id: "f1d4bb6745b64b3d"
```

returns exactly the lines for that one request. And because the status code and
duration are stored as numbers, this works too:

```
url.path: "/save" and http.response.status_code >= 500 and event.duration_ms > 1000
```

which is a question you simply cannot ask of a plain-text log without a grok
pattern that someone has to maintain.

---

## Part D — System design

### 1. Architecture

```mermaid
flowchart TB
    user(["Person with a<br/>phone and a<br/>journal entry"])

    subgraph app_tier["Application"]
        app["<b>gym-tracker</b><br/>FastAPI / uvicorn :8000<br/>───────────────<br/>/metrics exposition<br/>JSON logs to stdout"]
        pg[("<b>Postgres 16</b><br/>:5432<br/>workouts, users,<br/>reports")]
    end

    groq{{"<b>Groq API</b><br/>external<br/>LLM extraction"}}

    subgraph metrics_pipeline["Metrics — pull"]
        prom["<b>Prometheus</b> :9090<br/>scrapes every 15s<br/>TSDB, 15d retention"]
        node["<b>node-exporter</b> :9100<br/>host CPU/mem/disk/net"]
        graf["<b>Grafana</b> :3000<br/>3 provisioned dashboards"]
    end

    subgraph logs_pipeline["Logs — push"]
        docker[/"<b>Docker json-file driver</b><br/>/var/lib/docker/containers/<br/>10MB x 3 per container"/]
        fb["<b>Filebeat</b><br/>tails, strips envelope,<br/>decode_json_fields"]
        es[("<b>Elasticsearch</b> :9200<br/>indexed documents<br/>ILM: delete after 7d")]
        kib["<b>Kibana</b> :5601<br/>Discover + saved searches"]
    end

    user -->|HTTPS| app
    app -->|"SQL<br/>(SQLAlchemy)"| pg
    app -->|"HTTPS<br/>extraction"| groq

    app -.->|"HTTP GET /metrics<br/><b>Prometheus pulls</b>"| prom
    node -.->|"HTTP GET /metrics"| prom
    prom -->|PromQL| graf

    app ==>|stdout| docker
    docker ==>|"reads file"| fb
    fb ==>|"bulk index"| es
    es -->|queries| kib
    es -.->|"log panels"| graf

    classDef store fill:#e8f0fe,stroke:#3367d6,stroke-width:2px
    classDef ext fill:#fff4e5,stroke:#e8830c,stroke-width:2px,stroke-dasharray:4 3
    class pg,es store
    class groq ext
```

Dotted arrows are **pull** (Prometheus scraping), double arrows are **push** (the
log pipeline shipping forward).

#### What each component does, and how they communicate

| Component | Does | Talks to | How |
|---|---|---|---|
| **gym-tracker** | Serves the app; exposes `/metrics`; writes JSON to stdout | Postgres, Groq | SQL over TCP; HTTPS |
| **Postgres** | Stores workouts, users, reports | — | Receives SQL |
| **Groq** | LLM extraction. The one external dependency | — | HTTPS, called by the app |
| **Prometheus** | Scrapes `/metrics` every 15s, stores in its TSDB, evaluates alert rules | app, node-exporter, itself | HTTP GET, **it initiates** |
| **node-exporter** | Reads `/proc` and `/sys`, exposes host metrics | — | Scraped by Prometheus |
| **Grafana** | Queries Prometheus and Elasticsearch, draws dashboards | Prometheus, Elasticsearch | PromQL over HTTP; ES query DSL |
| **Docker json-file** | Captures stdout to disk, rotates it | — | Writes files |
| **Filebeat** | Tails container logs, parses, ships | Docker API, Elasticsearch | Reads socket + files; bulk HTTP |
| **Elasticsearch** | Indexes and stores documents, enforces ILM | — | Receives bulk writes |
| **Kibana** | Search UI | Elasticsearch | HTTP queries |

#### Where data is stored, and why this design

| Data | Stored in | Why there |
|---|---|---|
| Workouts, users, reports | Postgres (`postgres-data`) | Relational, needs transactions, foreign keys and exact answers. This is the system of record. |
| Metrics | Prometheus TSDB (`prometheus-data`), 15 days | Numeric time series compress enormously as delta-of-delta. Prometheus is a *recent-history* store, deliberately not an archive — this is why it is not a database you query for business facts. |
| Logs | Elasticsearch (`elasticsearch-data`), 7 days | An inverted index is what makes arbitrary field search fast. Storage cost per event is far higher than a metric sample, which is exactly why identifiers belong here and not in a metric label. |
| Dashboards, datasources, alert rules | **Files in the repository** | Reviewable in a diff, recreatable on any machine, not lost with a volume. |

The split between the last two rows is the load-bearing design decision, and it
is the same one Part E2 is about. **Metrics answer "how many, how fast, right
now" cheaply for unlimited events but only across a few bounded dimensions.
Logs answer "what exactly happened to this one request" at a cost per event.**
Trying to use one for the other's job is how both bills get large: putting a
request id in a metric label is a cardinality explosion, and computing a request
rate by counting log documents is expensive and imprecise.

#### If a component stops working

| Fails | Immediate effect | Still works | Recovery |
|---|---|---|---|
| **Postgres** | Saving and charts fail. The app serves errors, correctly | `/healthz`, `/metrics`, logs. `pool_pre_ping` avoids handing out dead connections | Restart; the volume persists. Errors visible as `gym_http_exceptions_total` |
| **Groq** | Parsing fails at `POST /log`. **No data is lost** — nothing was saved yet | Everything else. Weekly reports still work: narration degrades, every number is computed in code | Retry. Visible as `gym_llm_extractions_total{outcome="error"}` |
| **Prometheus** | No new metrics, dashboards go blank. **The app is unaffected** | Everything. The app does not know it is being scraped | Restart. The gap in history is permanent — scrapes missed are missed |
| **node-exporter** | No host metrics; `up{job="node-exporter"} == 0` fires `TargetDown` | Everything | Restart |
| **Grafana** | No dashboards | Prometheus still collects and still alerts; data is not lost, only the view | Restart. Dashboards are re-provisioned from files |
| **Elasticsearch** | Filebeat cannot ship and **backs off, retrying** | The app logs normally to stdout; Docker keeps the files | Restart. Filebeat resumes from its registry and catches up — **as long as Docker has not rotated the files away first.** This is the real data-loss window in the design |
| **Kibana** | No log UI | Ingestion continues; nothing is lost | Restart |
| **Filebeat** | No new documents | Files keep accumulating on the host | Restart; resumes from the registry |
| **The app** | Total outage | `up{job="gym-tracker"} == 0` fires `TargetDown` within a minute | The one failure the app cannot report itself — which is precisely why `up` is synthesised by the scraper |

The pattern worth naming: **observability failures degrade observability, not the
application.** The app does not know whether Prometheus is scraping it, and
writes to stdout whether or not anything is reading. That is deliberate and it is
why `/metrics` is a plain endpoint and logging is a stdout write — the two
cheapest, least coupled things they could be.

#### Things I do not fully understand yet

Stated plainly, as the brief asks:

- **Lucene segment merging.** I know ILM deletes whole indices because deleting
  documents only marks them until a merge reclaims the space. I could not
  explain the merge policy's scheduling or how to tune it.
- **Prometheus TSDB internals.** I understand the head block, the WAL and
  compaction into 2-hour blocks at the level of "why a restart does not lose
  recent data". I could not predict memory use from a series count with any
  precision — my cardinality reasoning in Part E is about *growth shape*, not
  bytes.
- **`histogram_quantile` interpolation at the edges.** I know it interpolates
  linearly within a bucket and that the result is bounded by bucket width — the
  Part E numbers show this — but I have not worked through its behaviour when
  observations cluster at a bucket boundary or fall in `+Inf`.
- **Filebeat back-pressure.** I know it retries and backs off when Elasticsearch
  rejects a bulk request. I have not traced how the queue interacts with file
  rotation to decide exactly when a log is lost rather than delayed.

### 2. Following one metric and one log

#### A metric: `gym_sets_written_total`

**Step 1 — code updates it.** Someone saves an entry with 5 sets. In
`app.py` (`POST /save`):

```python
METRICS.sets_written_total.inc(result.inserted_sets)   # app.py:1278
```

The counter is a float in the process's memory. Nothing is sent anywhere.

**Step 2 — the app exposes it.** On the next scrape, `GET /metrics` renders:

```
# HELP gym_sets_written_total Individual workout sets inserted into the database.
# TYPE gym_sets_written_total counter
gym_sets_written_total 5.0
```

**Step 3 — Prometheus collects it.** Every 15 seconds Prometheus fetches that
endpoint and appends a sample to the series, attaching the labels from the scrape
config:

```
gym_sets_written_total{job="gym-tracker", instance="app:8000", service="gym-tracker",
                       monitor="gym-tracker-local", environment="development"}
   @ 1789451340 → 5
   @ 1789451355 → 5
   @ 1789451370 → 12     # two more entries saved
```

**Step 4 — Grafana queries it.** The "Sets logged (24h)" panel asks:

```promql
increase(gym_sets_written_total[24h])
```

`increase()` — not the raw value — because a counter resets to 0 when the process
restarts. `increase()` and `rate()` detect the reset and account for it; reading
the raw value would show the total dropping to zero on every deploy.

**Step 5 — it is drawn.** A stat panel, last value, 0 decimal places.

**Observed values**, from `observability/results/02-fault-metrics.txt` — the
scrape taken at the end of the fault stage:

```
gym_http_requests_total{method="GET",route="/healthz",status="200"} 45.0
gym_http_request_duration_seconds_count{method="GET",route="/healthz"} 45.0
gym_http_request_duration_seconds_sum{method="GET",route="/healthz"} 4.029808496002261
gym_faults_injected_total{kind="latency"} 40.0
```

45 requests to `/healthz` taking 4.03 seconds in total — a mean of 89.6ms, from a
route whose real cost is under 4ms, because 8 of those 45 were delayed by half a
second each.

#### A log: one `POST /save`

**Step 1 — code writes it.** `app.py`:

```python
logger.info("journal entry saved", extra={
    "event.action": "entry_saved",
    "user.id": user.user_id,
    "session.date": str(draft.session_date),
    "entry.inserted_sets": result.inserted_sets,
    ...
})
```

The formatter adds `@timestamp`, `service.name`, `log.level`, and — from the
contextvar — `http.request.id`, without the call site mentioning it.

**Step 2 — it reaches stdout** as one line:

```json
{"@timestamp":"2026-09-15T06:07:29.210Z","log.level":"info","message":"journal entry saved","service.name":"gym-tracker","event.dataset":"gym_tracker.app","http.request.id":"959d6bccd86742ab","event.action":"entry_saved","user.id":1,"session.date":"2026-09-15","entry.inserted_sets":5}
```

**Step 3 — Docker stores it**, wrapped:

```json
{"log":"{\"@timestamp\":\"2026-09-15T06:07:29.210Z\",...}\n","stream":"stdout","time":"2026-09-15T06:07:29.210Z"}
```

**Step 4 — Filebeat parses it.** The `container` input strips the envelope;
`decode_json_fields` promotes the app's keys to the root; `add_docker_metadata`
attaches `container.name`. **The format changes twice** on this journey: flat
JSON → wrapped in Docker's envelope → unwrapped and expanded into a nested
document, with `http.request.id` becoming `{"http": {"request": {"id": ...}}}`
via `expand_keys`.

**Step 5 — Elasticsearch indexes it** into `filebeat-gym-tracker-*`, with
`entry.inserted_sets` mapped as a `long` and `http.request.id` as a `keyword`.

**Step 6 — Kibana finds it:**

```
event.action: "entry_saved" and entry.inserted_sets > 3
```

or, for that one request:

```
http.request.id: "959d6bccd86742ab"
```

#### The two paths meeting

The metric says **12 sets were written in the last hour**. The log says **which
entries, by whom, at what time, in which request**. Neither answers the other's
question, and the `http.request.id` is what lets you walk from a spike on a chart
to the specific clicks that caused it.

---

## Part E — Experiments

### 1. Reproducing a problem: injected latency

#### Method

The fault is **configuration, not code**: `faults.py` reads environment
variables, and nothing in the request path differs between a healthy and a
degraded run. Editing a route to add a `sleep` would work once and leave the
fault in the source history; this way, reverting is unsetting a variable.

Safety, because this is the one module whose purpose is to break the app:

- Inert unless `FAULT_INJECTION_ENABLED=true` — a latency value alone does
  nothing, two variables must agree.
- **Refuses to arm when `APP_ENV=production`**, whatever else is set.
- `/healthz` reports `"status": "degraded"` and the full configuration.
- Every injected fault increments `gym_faults_injected_total`.
- The delay is clamped to 10s, so a typo cannot hang a worker.
- `asyncio.sleep`, never `time.sleep` — the app is async on one worker, and
  `time.sleep` would block the event loop and stall *every* concurrent request,
  making the experiment measure queueing instead of latency.

The load is identical in all three stages: `scripts/load_generator.py` issues a
fixed, deterministic sequence across `/healthz`, `/`, `/login`, `/progress` and
`/metrics` at 5 rps. No randomness, so any difference between stages is the
fault.

```bash
./scripts/experiment_anomaly.sh                  # all three stages
STAGE_SECONDS=180 FAULT_LATENCY_MS=800 ./scripts/experiment_anomaly.sh
```

#### Predictions (written before the fault stage ran)

Fault: 500ms on every 5th request, i.e. 20% of requests.

| Signal | Prediction | Reasoning |
|---|---|---|
| p95 latency | rises to ~500ms | 20% of requests are delayed, so the 95th percentile lands inside the delayed group |
| **p50 latency** | **barely moves** | the median request is not one of the delayed ones |
| mean latency | rises ~100ms | 500ms averaged over 5 requests |
| throughput | **falls** | a closed-loop client issues fewer requests when each takes longer |
| error rate | unchanged at 0 | a delay is not a failure |
| `gym_faults_injected_total` | 0 → non-zero | proves the fault was live |
| logs | `warning` per delayed request; no `error` lines | it degrades, it does not break |

#### Results

Three stages, 40s each at 5 rps. Full data in `observability/results/`.

**Client-side** (measured by the load generator, `*.json`):

| Stage | Requests | Achieved rps | p50 | **p95** | p99 | Mean | Errors |
|---|---|---|---|---|---|---|---|
| 01-baseline | 200 | 4.99 | 3.7ms | **5.3ms** | 6.5ms | 3.5ms | 0% |
| 02-fault | 141 | 3.51 | 3.7ms | **506.1ms** | 506.9ms | 141.8ms | 0% |
| 03-recovery | 200 | 4.99 | 3.8ms | **5.2ms** | 5.8ms | 3.5ms | 0% |

**Server-side** (computed from the Prometheus histogram buckets exactly as
`histogram_quantile` does, `server-side-percentiles.json`):

| Stage | Observations | p50 | **p95** | p99 | Mean | `gym_faults_injected_total` |
|---|---|---|---|---|---|---|
| 01-baseline | 282 | 2.5ms | **4.8ms** | 5.0ms | 0.8ms | **0** |
| 02-fault | 199 | 3.1ms | **686.2ms** | 737.2ms | 98.9ms | **40** |
| 03-recovery | 282 | 2.5ms | **4.8ms** | 5.0ms | 0.9ms | **0** |

Time windows for the Grafana/Kibana pickers:

```
01-baseline   2026-09-15T06:20:43Z .. 2026-09-15T06:21:23Z
02-fault      2026-09-15T06:21:26Z .. 2026-09-15T06:22:06Z
03-recovery   2026-09-15T06:22:10Z .. 2026-09-15T06:22:50Z
```

The raw histogram for `/healthz` during the fault stage shows the distribution
directly:

```
gym_http_request_duration_seconds_bucket{le="0.5", route="/healthz"}   37.0
gym_http_request_duration_seconds_bucket{le="0.75",route="/healthz"}   45.0
gym_http_request_duration_seconds_count{route="/healthz"}              45.0
gym_http_request_duration_seconds_sum{route="/healthz"}                4.031
```

37 requests under half a second, 8 more between 0.5s and 0.75s. Bimodal — two
populations, not one slow average. The baseline for the same route has all 62
observations in the smallest bucket.

#### What changed, and what it means

**The prediction held.** Every one:

- **p95 rose by 95×** (5.3ms → 506.1ms) while **p50 did not move at all** (3.7ms →
  3.7ms). This is the entire argument for percentiles. A dashboard showing only
  the median would have reported a perfectly healthy service while one request in
  five took half a second.
- **The mean is the worst of both** — 141.8ms describes no actual request. No
  request took 142ms; they took either ~4ms or ~506ms. An average over a bimodal
  distribution is a number with no referent.
- **Throughput fell 30%** (4.99 → 3.51 rps) with the offered load unchanged. The
  client is closed-loop: it waits for each response, so latency converts directly
  into lost throughput. On a real service this is how a latency problem becomes a
  capacity problem.
- **The error rate stayed at 0%.** Every request returned 200. A monitoring setup
  watching only errors and uptime would have seen nothing at all.
- **`gym_faults_injected_total` went 0 → 40 → 0**, which is what makes this
  evidence rather than an assertion that a variable was set.

**Cause.** `faults.py` delays every 5th request by 500ms inside the timed
region. `fault.sequence` in the logs confirms the sampling was exactly
deterministic: 5, 10, 15, 20, 25 …

**Effect on users.** One request in five takes half a second longer. But a page
load involves several requests, so in practice *most page loads feel slow* rather
than one in five — which is why p95 predicts how an app feels far better than an
average does.

**Recovery.** Stage 3 returns to baseline within measurement noise (p95 5.2ms vs
5.3ms), and `/healthz` goes back to `{"status": "ok"}`. Verified, not assumed.

#### Two things this experiment taught me that I did not plan

**1. I had the instrumentation wrong, and the experiment caught it.**

The first run produced a contradiction: the client measured a p95 of 506ms while
the server-side histogram showed every request in the fastest bucket. The cause
was ordering in my own middleware — the fault was applied *before*
`started = time.perf_counter()`, so the delay fell outside the timed region.
Prometheus was reporting a perfectly healthy service while every client waited
half a second.

That is exactly the blind spot this assignment is about, and I had built it by
accident. The fix (`app.py`) was to start the timer before fault injection, so
the server's measurement covers everything the client waits for. **The general
lesson: instrumentation that does not span the whole path is worse than none,
because it produces confident wrong answers.** I only found it because I compared
two independent measurements.

**2. Server-side p95 (686.2ms) and client-side p95 (506.1ms) disagree by 180ms.**

Not an error — a property of histograms. The real observations cluster just above
0.5s, but the surrounding buckets are `le="0.5"` and `le="0.75"`, so
`histogram_quantile` interpolates linearly across that 250ms gap and lands at
686ms. **A histogram's percentile is only as precise as its bucket boundaries.**
The client, holding every raw sample, can report the true value; Prometheus
trades that precision for the ability to aggregate across instances and any time
window. Knowing which number is which matters before anyone sets an SLO on it.

### 2. Cardinality explosion

#### Why it happens

Prometheus stores **one time series per unique combination of label values**. A
label drawn from a small fixed set (an HTTP method, a status code) adds a handful
of series. A label carrying an identifier — a request id, a user id, an email —
adds one series for *every distinct value ever seen*. Each series carries its own
samples, index entries and memory in the head block.

#### Method

`observability/cardinality_demo.py` exposes the same event as two counters:

```python
UNSAFE = Counter("demo_requests", ..., ["endpoint", "request_id"])  # one series per request
SAFE   = Counter("demo_requests_safe", ..., ["endpoint"])           # one series, ever
```

**Bounded by design**, as the brief requires: a hard cap of 100 unique ids, past
which the script re-uses an id rather than minting series, and `--max-ids` above
100 is *refused*. 100 series is nothing to a Prometheus server — the lesson is the
shape of the growth curve, not the damage.

```bash
./scripts/experiment_cardinality.sh            # in-process
./scripts/experiment_cardinality.sh --stack    # through Prometheus
```

#### Results

**Step 1–2 — with the `request_id` label**, growth measured at five points (each
an independent process):

| Requests | `count(demo_requests_total)` | `count(demo_requests_safe_total)` |
|---|---|---|
| 1 | **1** | 1 |
| 10 | **10** | 1 |
| 25 | **25** | 1 |
| 50 | **50** | 1 |
| 100 | **100** | 1 |

Series growth is exactly **linear in the number of requests**. The safe counter
is flat at 1.

**Step 3 — label removed, app restarted**, same 100 requests:

```
[cardinality-demo] requests=100 mode=safe demo_requests_total series=0 demo_requests_safe_total series=1
```

100 requests, one series.

Through Prometheus (`--stack`), the same comparison runs as:

```promql
count(demo_requests_total)        # 100
count(demo_requests_safe_total)   # 1
```

#### The cost at scale

Extrapolating the measured 1-series-per-request rate:

| Traffic | New series/day with a `request_id` label |
|---|---|
| 10 req/s | 864,000 |
| 100 req/s | 8,640,000 |
| 1,000 req/s | **86,400,000** |

A single Prometheus server is generally comfortable into the low millions of
active series. At 100 req/s this design exceeds that **in one day**, and the
failure is not graceful: memory climbs until the process is killed, taking with
it the monitoring for everything else — at precisely the moment you most need it.

And the cost is not only Prometheus's. Every one of those series is stored,
indexed, and replicated forever inside the retention window, while being queried
exactly never: *nobody asks "how many requests had id `a3f9c2`?"* The series
answers a question no one has.

#### Why identifiers belong in logs

The request id is genuinely useful — it is what made the Part D trace possible.
The question is which store it goes in.

| | Metric label | Log field |
|---|---|---|
| Cost model | One **series**, forever, per distinct value | One **field** on one event |
| Growth | Multiplies with every other label | Linear in events |
| Good at | "how many, how fast" across bounded dimensions | "what happened to *this* one" |
| Retention | Full resolution for the whole window | Age out cheaply by index |

So this app puts the request id in **every log line** and in **no metric label**.
`gym_http_requests_total` is labelled by route *template* (`/weekly-report`),
never the raw path — which is also why an unmatched path collapses to a single
`route="unmatched"` series, so a scanner probing `/wp-login.php` and `/.env`
cannot mint series on this server from outside.

This is enforced in the test suite, not just documented:
`test_labels_are_bounded_dimensions_only` fails if anyone ever adds `user_id`,
`request_id`, `session_date`, `username` or similar as a label.

#### The part that is easy to get wrong

**Removing the label does not delete the series that already exist.** They stop
receiving samples, but they stay in Prometheus — consuming memory and disk —
until they fall out of the retention window, 15 days here.

So the timeline of a real cardinality incident is: deploy the bad label, memory
climbs, notice, ship a fix, and then *wait days* for the server to recover. There
is no command that undoes it.

**That is why cardinality is a code-review question, not an incident-response
one.** The cheapest moment to catch it is before it merges, which is exactly what
the label test is for.

---

## Running it yourself

### Prerequisites

Docker and Docker Compose. Around 4GB of free RAM (Elasticsearch takes a 512MB
heap and Kibana is not small). No Groq API key is needed for any of the
observability work — it is only required to parse a real journal entry at
`POST /log`.

### Start

```bash
git clone <this repo> && cd Gym-tracker
cp .env.example .env          # optional; every value has a working default

./scripts/stack.sh up         # build, start, wait for health, import Kibana objects
```

`stack.sh up` builds the app image, starts all eight services, waits for each to
report healthy, generates a little traffic so nothing is empty, and imports the
Kibana saved objects.

### Use

| What | Where |
|---|---|
| The app | <http://localhost:8000> — register at `/signup` |
| Metrics | <http://localhost:8000/metrics> |
| Prometheus | <http://localhost:9090> — targets at `/targets`, alerts at `/alerts` |
| Grafana | <http://localhost:3000> — `admin` / `admin` |
| Kibana | <http://localhost:5601> — Discover → Open → saved searches |
| node-exporter | <http://localhost:9100/metrics> |

```bash
./scripts/stack.sh status     # containers, Prometheus targets, docs indexed, fault state
./scripts/stack.sh urls
./scripts/stack.sh logs
```

### Generate traffic

```bash
python scripts/load_generator.py --duration 120 --rps 5
# with a real account, to reach the authenticated routes:
python scripts/load_generator.py --duration 120 --rps 5 --username you --password ...
```

### Run the experiments

```bash
./scripts/experiment_anomaly.sh          # Part E1 — writes observability/results/
./scripts/experiment_cardinality.sh      # Part E2
```

### Test

```bash
python -m pytest -q                      # 929 tests
python -m pytest tests/test_observability.py -q   # 63 covering this assignment
```

### Clean up safely

```bash
./scripts/stack.sh down        # stop, KEEP all data
./scripts/stack.sh destroy     # stop and DELETE every volume (asks to confirm)
```

`down` and `destroy` are separate on purpose: losing an experiment's data to the
habit of typing `down -v` is a bad afternoon. `destroy` requires typing the word
`destroy`.

To confirm nothing is left behind:

```bash
docker compose ps -a && docker volume ls | grep gym-tracker
```

### Security notes

**This compose file is a local lab, not a production deployment.** Two things
would be wrong on any network anyone else can reach, and both are deliberate
choices to keep the focus on observability:

- **Elasticsearch runs with `xpack.security.enabled=false`** — no TLS, no
  authentication. Anyone who can reach port 9200 can read and delete every log.
- **Grafana uses `admin`/`admin`.**

A third is worth calling out because it is a mistake that is easy to make in
earnest: **`/metrics` is unauthenticated.** That is correct here — nothing
outside the Docker network can reach it — and wrong on a public deployment, where
it would publish login failure counts, business volumes and build identity to
anyone who asked. The app supports `METRICS_TOKEN` for that case, compared with
`hmac.compare_digest` and matched by an `authorization` block in the Prometheus
scrape config. It is unset by default because a token on localhost is ceremony,
not security.

---

## What is verified, and what is not

Stated plainly, because a report that quietly implies more testing than actually
happened is worse than one that draws the line.

### Verified by running it

- **929 automated tests pass**, including 63 new ones covering metrics, JSON log
  shape, redaction, request-id propagation, fault injection and the cardinality
  cap.
- **The app runs** under uvicorn and serves `/metrics` in valid Prometheus
  exposition format, with all four metric types present.
- **JSON logging works**, including request-id correlation and all four
  redaction paths (key, Groq key shape, `Bearer` header, URL credentials).
- **Fault injection works end to end** — the three-stage experiment in Part E was
  really run; every number in those tables is measured, not illustrative.
- **The cardinality demo works** — the growth table is measured output.
- **The cardinality cap holds** at 100, and `--max-ids` above it is refused.
- **Config files are valid**: `docker compose config` validates the compose file
  against Docker's own schema; every YAML and JSON file parses; the Grafana
  dashboards have no duplicate panel ids or refIds; every Kibana saved object and
  its nested query JSON parse.
- **All shell scripts pass `bash -n`.**

### Not verified by running it

**The containerised stack has not been started end to end.** The environment this
was developed in blocks Docker registry pulls at the network policy level
(`production.cloudfront.docker.com` returns 403), so no image could be
downloaded. That means the following are *written and validated as
configuration* but have not been observed running:

- Prometheus actually scraping the app and node-exporter.
- Grafana rendering the three dashboards from the provisioned files.
- Filebeat tailing Docker logs, and `decode_json_fields` parsing them.
- Elasticsearch indexing, and the ILM policy rolling over and deleting.
- Kibana importing the saved objects.

**What this means in practice.** The Part E1 experiment was run against the app
directly, so the latency numbers are real and the server-side percentiles are
computed from the app's genuine histogram buckets — but they were computed by
replicating `histogram_quantile`'s interpolation in Python
(`observability/results/server-side-percentiles.json`) rather than by querying
Prometheus. Likewise the cardinality numbers come from counting series in the
real exposition output rather than from `count(demo_requests_total)` against a
running server. The arithmetic is the same; the path it took is not.

The most likely places for a first run to need a fix are the version-sensitive
ones: the Kibana saved-object schema (8.15 format — `setup_kibana.sh` says what
to do by hand if the import is rejected) and the Filebeat ILM setup. Everything
Python-side is exercised by the test suite.

---

## Credits

- **Existing application** (FastAPI app, extraction pipeline, review flow,
  insights, accounts): my own prior work in this repository.
- **Added for this assignment**: `metrics.py`, `logging_setup.py`, `faults.py`,
  `observability/`, `scripts/`, `Dockerfile`, `docker-compose.yml`,
  `tests/test_observability.py`, and the instrumentation call sites in `app.py`
  and `pipeline.py`.
- **AI assistance**: Claude (Anthropic) was used to implement the observability
  layer, the compose stack, the dashboards and this report, working from the
  assignment brief and the existing codebase. All of it was reviewed and the
  experiments were run and their results recorded.
- **Documentation consulted**: Prometheus docs (metric types, naming, and the
  [label cardinality guidance](https://prometheus.io/docs/practices/naming/#labels)
  the brief links), Grafana provisioning docs, Elastic's Filebeat/ECS/ILM docs,
  `prometheus_client` source for the Summary quantile question.
- **Method**: the RED method (Rate, Errors, Duration) for the application
  dashboard, after Tom Wilkie; the USE method (Utilisation, Saturation, Errors)
  informed the host dashboard.
- Course materials: Lab 1 — Midnight Launch, used as a reference for the stack
  layout.
