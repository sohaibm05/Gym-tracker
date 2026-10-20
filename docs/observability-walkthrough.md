# Three metrics, one chart, two logs, one experiment

A guided tour of one slice of this project's observability, chosen to be deep
rather than wide. The app exports 19 metric families, ships every log line to
Elasticsearch, and provisions three Grafana dashboards. This file takes four
things out of that pile and follows each one all the way down: what it is, the
code that produces it, how the value travels from a line of Python to a picture
on a screen, and what it actually read during the recorded experiment.

| | Chosen | Type | Declared / emitted at |
|---|---|---|---|
| Metric 1 | `gym_http_request_duration_seconds` | Histogram — application | [metrics.py:323](../metrics.py) |
| Metric 2 | `gym_sets_written_total` | Counter — business | [metrics.py:195](../metrics.py) |
| Metric 3 | `gym_review_drafts_open` | Gauge — business | [metrics.py:300](../metrics.py) |
| Chart | "Latency percentiles (5m window)" | Grafana timeseries | [01-app-performance.json](../observability/grafana/dashboards/01-app-performance.json) |
| Log 1 | `request served` | `info`, one per HTTP request | [app.py:463](../app.py) |
| Log 2 | `injected fault: delaying this request deliberately` | `warning`, one per injected delay | [faults.py:169](../faults.py) |
| Experiment | Baseline → fault → recovery | 3 stages × 120 s | [scripts/experiment_anomaly.sh](../scripts/experiment_anomaly.sh) |

**Why this particular set.** The three metrics are one of each type that
behaves differently under query — a distribution, a monotonic total, and a
value that goes both ways — and they cover both families the project
instruments: the machinery (HTTP latency) and the product itself (sets written,
drafts awaiting review). The chart reads metric 1. Log 1 is the same event as
metric 1 recorded in the other medium, which is what lets the two cross-check
each other. Log 2 is the experiment's own evidence. And the experiment moves
metric 1, the chart, and both logs at once — so these are one story rather than
four unrelated features.

---

## 0. The two pipelines

Metrics and logs never touch each other. They are separate paths with separate
storage, separate query languages, and separate reasons to exist.

```
                        ┌──────────────────────────────────┐
                        │ FastAPI app (one uvicorn worker) │
                        │  app.py · metrics.py · faults.py │
                        └───────────┬──────────────┬───────┘
      numbers, aggregated           │              │   text, one event per line,
      in process memory             │              │   written to stdout
                                    ▼              ▼
                     GET /metrics (pull, 15s)    Docker json-file driver
                                    │              │  /var/lib/docker/containers/<id>/*.log
                                    ▼              ▼
                            ┌──────────────┐   ┌──────────┐
                            │  Prometheus  │   │ Filebeat │ tails, strips the Docker
                            │     TSDB     │   │          │ envelope, parses the JSON
                            └──────┬───────┘   └────┬─────┘
                                   │                ▼
                                   │        ┌───────────────┐
                                   │        │ Elasticsearch │ one document per line,
                                   │        │   (ILM: 7d)   │ every field indexed
                                   ▼        └───────┬───────┘
                            ┌──────────┐            ▼
                            │ Grafana  │      ┌──────────┐
                            │ (PromQL) │      │  Kibana  │ (KQL)
                            └──────────┘      └──────────┘
```

The division of labour is the same one that decides what may be a metric label
and what may not:

- **A metric answers "how much, how often, how slow", for everything at once.**
  It is cheap because it is aggregated before it is stored — one number per
  series per scrape, however many requests it summarises.
- **A log answers "what happened to *this* one".** It is expensive per event
  and can carry high-cardinality detail: a user id, a session date, a request
  id, an exact path.

So a request id lives in logs and never in a metric label, and a latency
percentile lives in a metric and is never computed by reading logs. Sections 1
and 5 are the same HTTP requests seen through both.

---

## 1. Metric 1 — `gym_http_request_duration_seconds` (Histogram, application)

### What it measures

Wall-clock seconds to serve one HTTP request, from before anything happens to
after the response is ready — bucketed, and split by HTTP method and matched
route template.

```python
# metrics.py:323
self.http_request_duration = Histogram(
    "gym_http_request_duration_seconds",
    "Wall-clock time to serve an HTTP request, by method and route template.",
    ["method", "route"],
    buckets=HTTP_DURATION_BUCKETS,
    **kw,
)

# metrics.py:113 — the buckets, in seconds
HTTP_DURATION_BUCKETS = (
    0.005, 0.01, 0.025, 0.05, 0.075, 0.1, 0.25, 0.5, 0.75, 1.0, 2.5, 5.0, 10.0,
)
```

The app is a phone-facing HTML form, so the interesting region is 10 ms – 1 s;
the buckets keep resolution up to 10 s because that is where the latency
experiment pushes requests.

### Where it is recorded

In exactly one place: the HTTP middleware at [app.py:339](../app.py). No route
times itself, so no route can disagree with the middleware about what happened.

```python
# app.py:456 — the success path. app.py:416 is the identical call on the
# exception path, so a request that crashes is still timed.
METRICS.http_request_duration.labels(
    method=request.method, route=route
).observe(duration_ms / 1000.0)
```

Two details there matter more than they look:

1. **The timer starts before fault injection**, not after
   ([app.py:387](../app.py)). An injected delay has to fall *inside* the timed
   region. Timing only the handler would report a perfectly healthy service
   while every client sat waiting half a second — the exact blind spot the
   experiment in section 7 is about. The server's measurement must cover
   everything the client waits for.
2. **`route` is the route *template*** (`/weekly-report`), never the raw path,
   and anything that matched no route collapses to the single series
   `unmatched` ([metrics.py:405](../metrics.py)). Without that, every
   `/wp-login.php` from a scanner would mint a new time series — a cardinality
   explosion driven by strangers.

### What a histogram actually stores

Not durations. Fourteen cumulative counters (13 buckets plus `+Inf`), a sum and
a count, per label combination. Here is the real exposition output for
`GET /healthz` at the end of the fault stage:

```
gym_http_request_duration_seconds_bucket{le="0.005",method="GET",route="/healthz"} 112.0
gym_http_request_duration_seconds_bucket{le="0.01", method="GET",route="/healthz"} 114.0
...
gym_http_request_duration_seconds_bucket{le="0.5",  method="GET",route="/healthz"} 114.0
gym_http_request_duration_seconds_bucket{le="0.75", method="GET",route="/healthz"} 139.0
gym_http_request_duration_seconds_bucket{le="+Inf", method="GET",route="/healthz"} 139.0
gym_http_request_duration_seconds_count{method="GET",route="/healthz"} 139.0
gym_http_request_duration_seconds_sum{method="GET",route="/healthz"}   12.664330702998086
```

Read it as: 139 requests, 114 of them finished within 500 ms, all 139 within
750 ms. The 25 requests sitting in the `(0.5, 0.75]` gap are the injected
500 ms delays — visible in the raw numbers, before any query is written.
Because the buckets are cumulative, `le="0.75"` includes everything `le="0.5"`
already counted, which is what lets Prometheus add up the buckets of many
instances (`sum by (le)`) and still hold a valid distribution.

That is the whole reason this is a Histogram and not a Summary: percentiles can
be computed *after the fact*, over any window, across every process. A Summary
in the Python client exposes only `_sum` and `_count`
(`test_summary_exposes_sum_and_count_but_no_quantiles` asserts exactly that),
so it can answer "what is the average" and nothing else.

### How it is queried

```promql
# p95 across everything
histogram_quantile(0.95, sum by (le) (rate(gym_http_request_duration_seconds_bucket[5m])))

# p95 per route — which part of the app is slow
histogram_quantile(0.95, sum by (le, route) (rate(gym_http_request_duration_seconds_bucket[5m])))

# the true mean: total time ÷ total requests
sum(rate(gym_http_request_duration_seconds_sum[5m]))
  / clamp_min(sum(rate(gym_http_request_duration_seconds_count[5m])), 0.0001)
```

`rate()` first, then `sum by (le)`, then `histogram_quantile` — in that order.
`rate()` turns the cumulative counters into per-second rates *and* absorbs
counter resets across restarts; summing before rating produces nonsense the
first time the app restarts.

`histogram_quantile` interpolates linearly inside the bucket the percentile
falls in, so it is an estimate bounded by the bucket edges, never an exact
observation. Section 7 shows this concretely: the server reported p95 = 687 ms
for delays the client measured at 540 ms.

### What it feeds

- The chart in section 4, and three other panels on the same dashboard.
- The `SlowRequests` alert
  ([alerts.yml:50](../observability/prometheus/alerts.yml)): p95 per route above
  1 s, sustained for 2 minutes.

---

## 2. Metric 2 — `gym_sets_written_total` (Counter, business)

### What it measures

Individual workout sets inserted into `workout_logs`. Not entries — sets. An
entry can be one set or thirty, so this is the honest measure of how much
training the product is actually capturing.

```python
# metrics.py:195
self.sets_written_total = Counter(
    "gym_sets_written_total",
    "Individual workout sets inserted into the database.",
    **kw,
)
```

No labels at all. Adding `exercise` would be the obvious idea and the wrong
one: exercise names are user-created, so the label set would be unbounded and
would grow forever. The per-exercise breakdown lives in Postgres, which is
built for it, and in the logs — `entry.inserted_sets` rides on the
`entry_saved` line at [app.py:1408](../app.py).

### Where it is incremented

Once, at the end of a successful `POST /save`, after the transaction has
committed — so the counter can never claim a write that was rolled back.

```python
# app.py:1396
if METRICS is not None:
    METRICS.entries_total.labels(outcome="saved").inc()
    # inc(0) is a no-op on a Counter, so an entry that wrote only a
    # bodyweight reading does not disturb the sets total.
    METRICS.sets_written_total.inc(result.inserted_sets)
```

### Counters only go up — and that is the point

A counter's raw value is close to meaningless; what matters is how fast it
climbs, so every useful query wraps it in `rate()` or `increase()`. Those
functions exist for a reason that shows up plainly in the recorded data. These
are the stored samples of this metric from 21 September
([metric-trace-sets-written.json](../observability/results-rerun/metric-trace-sets-written.json)):

| Sample time (UTC) | Value |
|---|---|
| 05:06:45 | 0 |
| 05:12:30 | 17 |
| 05:21:00 | **0** ← the app restarted |
| 05:21:45 | 3 |
| 05:22:15 | 16 |

`increase(gym_sets_written_total[24h])` over that window returned **33.0007**,
not 16: Prometheus recognised the drop to zero as a counter reset and added the
17 from before the restart to the 16 after it. (The trailing 0.0007 is
extrapolation to the window edges — `increase()` is explicitly an estimate,
which is why it is the right function for "roughly how many sets this week" and
the wrong one for billing.)

### How it is queried

```promql
increase(gym_sets_written_total[24h])         # "Sets logged (24h)" stat, business dashboard
sum(rate(gym_sets_written_total[5m])) * 60    # sets per minute, as a trend line
```

---

## 3. Metric 3 — `gym_review_drafts_open` (Gauge, business)

### What it measures

Journal entries that have been parsed and shown on the review screen but not
yet saved — the backlog of work waiting on a human. This is the project's
version of the classic "orders waiting to be prepared": a number that goes up
and down, where the current value is the answer and the history is context.

```python
# metrics.py:300
self.review_drafts_open = Gauge(
    "gym_review_drafts_open",
    "Parsed entries shown for review and not yet saved or abandoned.",
    **kw,
)
```

### Where it moves

Up at `POST /log` when a draft is rendered, down at `POST /save` when it is
resolved:

```python
# app.py:1271 — a parsed entry now sits on the review screen, unconfirmed
if DRAFTS is not None and not draft.error:
    DRAFTS.opened(user.user_id, parsed_date)

# app.py:1405 — the review screen is resolved, so the gauge comes back down
if DRAFTS is not None:
    DRAFTS.closed(user.user_id, draft.session_date)
```

### The hard part: making it come back down

A gauge that only ever rises is worse than no gauge, because a dashboard reads
it as a real backlog. And the most common way to leave the review screen is to
close the tab — no save, no signal, nothing to decrement on. `DraftTracker`
([metrics.py:428](../metrics.py)) handles that:

- Each open draft is held as `(user_id, session_date) -> timestamp` in a plain
  dict inside the process.
- Anything older than `ttl_seconds` (default 1800) is aged out on the next
  touch, so an abandoned draft disappears instead of accumulating.
- The dict is capped at `max_entries` (default 1000), oldest dropped first, so
  a burst of parses cannot grow memory without bound.
- Only `len(self._open)` is ever published. The user id and session date are
  dict keys inside the process; they never become metric labels.

Both behaviours are pinned by tests — `test_abandoned_drafts_age_out` and
`test_burst_is_capped` in
[tests/test_observability.py](../tests/test_observability.py).

### How it is queried

```promql
gym_review_drafts_open                      # read directly — no rate(), no increase()
max_over_time(gym_review_drafts_open[1h])   # the worst backlog in the last hour
```

A gauge is the one type you query raw. `rate()` on a gauge is meaningless, and
Prometheus will happily compute it anyway.

### One honest limitation

The value is per process. On a single worker — which is how this app is
deployed ([Dockerfile](../Dockerfile)) — it is the true global count. Run two
workers and each would publish its own drafts; the query would need
`sum(gym_review_drafts_open)`, and a draft opened on one worker and saved on
the other would leave a phantom behind until the TTL cleared it. Shared state
would be the fix, and it is not worth it here.

---

## 4. The chart — "Latency percentiles (5m window)"

One of the 13 data panels on the **Gym Tracker — Application performance**
dashboard (`uid: gym-app-perf`), provisioned from version-controlled JSON at
[observability/grafana/dashboards/01-app-performance.json](../observability/grafana/dashboards/01-app-performance.json).
Nothing about it is clicked into existence: Grafana's provisioning
([provisioning/dashboards/dashboards.yml](../observability/grafana/provisioning/dashboards/dashboards.yml))
loads the file at startup, so the dashboard is reviewable in a diff and
survives a wiped container.

### The panel

| Property | Value |
|---|---|
| Type | `timeseries` |
| Datasource | Prometheus (`uid: prometheus`) |
| Unit | `s`, 3 decimals — Grafana renders 0.5038 as `503.958 ms` |
| Legend | table, bottom, showing mean / max / last per series |
| Tooltip | `multi`, sorted descending — all four values at the hovered instant |
| Dashboard refresh | 10 s; default range `now-30m` |

Four queries on one set of axes:

```promql
histogram_quantile(0.50, sum by (le) (rate(gym_http_request_duration_seconds_bucket[5m])))   # p50
histogram_quantile(0.95, sum by (le) (rate(gym_http_request_duration_seconds_bucket[5m])))   # p95
histogram_quantile(0.99, sum by (le) (rate(gym_http_request_duration_seconds_bucket[5m])))   # p99
sum(rate(gym_http_request_duration_seconds_sum[5m]))
  / clamp_min(sum(rate(gym_http_request_duration_seconds_count[5m])), 0.0001)                # mean
```

### Why these four together

The panel's own description says it: *the mean sits below p95 and barely moves
when a minority of requests get slow — which is exactly why the percentile is
the one to alert on.* Plotting the mean beside the percentiles is not
redundancy, it is the argument. During the fault stage the median request was
still served in about 3 ms while one request in five took half a second; an
average alone shows a mild, ignorable rise, and a percentile shows the fault
immediately.

Three design decisions worth naming:

- **`sum by (le)` before `histogram_quantile`.** Aggregating the buckets across
  every instance and route first gives one distribution for the service.
  Averaging per-route percentiles instead would be arithmetically wrong — you
  cannot average percentiles.
- **`[5m]`, against a 15 s scrape interval.** That is 20 scrapes per window:
  smooth enough to read, still responsive. `rate()` needs at least four points
  to be trustworthy, so nothing below `[1m]` is meaningful here — the
  dashboard's own text panel says so.
- **`clamp_min(..., 0.0001)` in the mean.** With no traffic the denominator is
  zero; without the clamp the series becomes `NaN` and the panel shows a gap
  that looks like an outage instead of an idle app.

### Reading it

- **All four lines flat and low** — healthy.
- **p99 and p95 lift, p50 and mean stay down** — a minority of requests are
  slow. A dependency, a lock, a cold cache, or a deliberate fault.
- **All four rise together** — everything is slow: saturation, not a tail.
- **p50 above the mean** — impossible for a real distribution; it means the
  buckets are too coarse for the region the traffic is actually in, and the
  interpolation is guessing.

### Seen during the experiment

[docs/screenshots/01-grafana-app-performance.png](screenshots/01-grafana-app-performance.png)
captures this panel mid-fault: p50 flat at ~2.6 ms while p95 reaches 599 ms and
p99 reaches 720 ms, with the "Injected faults (Part E)" panel directly below
showing the `latency` series rising over the same minutes, and "Error rate
(5xx)" sitting at 0.00% throughout. Three panels, one conclusion, no ambiguity
about what was happening or when.

### Related, not the same

Two other panels read the same metric differently and are worth a glance beside
it. **Latency distribution** is a heatmap of
`sum by (le) (increase(...bucket[1m]))`, and it shows the *bimodality* the
percentile lines only imply — two separate horizontal bands during the fault,
one at 5 ms and one at 500–750 ms, rather than one smeared middle. **p95 by
route** answers whether the slowness is confined to one endpoint; during the
fault every route moved together, which is the signature of a fault in the
middleware rather than in a handler.

---

## 5. Log 1 — `request served`

### The line

Emitted once per HTTP request by the middleware at [app.py:463](../app.py),
immediately after the same block records the metrics from section 1:

```python
logger.info(
    "request served",
    extra={
        "http.request.method": request.method,
        # The raw path is logged, the route *template* is what labels the
        # metric. Logs are indexed for search and can hold the specific
        # value; a metric label cannot, without minting a series per path.
        "url.path": request.url.path,
        "http.route": route,
        "http.response.status_code": response.status_code,
        "event.duration_ms": round(duration_ms, 1),
    },
)
```

On the wire, one line, one JSON object — a real one, captured 22 September:

```json
{"@timestamp": "2026-09-22T09:12:17.329Z", "log.level": "info", "message": "request served",
 "service.name": "gym-tracker", "service.version": "1.0.0", "event.dataset": "gym_tracker.app",
 "log.logger": "gym_tracker.app", "log.origin.file.name": "app.py", "log.origin.file.line": 463,
 "log.origin.function": "log_requests", "process.pid": 1, "http.request.id": "259fd665642941e0",
 "http.request.method": "GET", "url.path": "/login", "http.route": "/login",
 "http.response.status_code": 200, "event.duration_ms": 191.5}
```

### What makes it useful

- **ECS field names** (`http.request.id`, `url.path`, `event.duration_ms`), so
  Kibana's built-in field groups and columns work without custom mappings.
  `EcsJsonFormatter` ([logging_setup.py:197](../logging_setup.py)) builds the
  document; anything a call site attaches with `extra={...}` is promoted to its
  top level.
- **`http.request.id`** comes from a `ContextVar` set once per request
  ([logging_setup.py:70](../logging_setup.py)). Every line emitted anywhere in
  the call stack while that request is handled picks it up automatically — a
  contextvar rather than a thread-local precisely because the app is async and
  several requests share a thread. The id also goes back in the `x-request-id`
  response header, so a person reporting a problem hands over the exact token
  to search for. An inbound `x-request-id` is reused if it is sane (at most 64
  characters, `[A-Za-z0-9._:-]` only) and otherwise silently replaced rather
  than sanitised.
- **`event.duration_ms` is the same measurement as the histogram**, to one
  decimal. That is the cross-check: if the metric says p95 is 500 ms, this
  field says *which* requests those were.
- **It is not the uvicorn access log.** Uvicorn's own line is still emitted
  (`event.dataset: uvicorn.access`, same request id), but it carries no
  duration and no route template. Both flow through the same formatter because
  `configure_logging` clears uvicorn's handlers and lets its records propagate
  to the root ([logging_setup.py:323](../logging_setup.py)) — otherwise every
  line would be printed twice, once as JSON and once in uvicorn's own format,
  and Filebeat would index the unparsed copy.

### What is deliberately absent

No journal text, ever. `raw_text` and `raw_source` are in `SENSITIVE_KEYS`
([logging_setup.py:104](../logging_setup.py)) alongside passwords, tokens and
`DATABASE_URL`; the entry's *length* and the number of rows extracted from it
are recorded instead. The filter is installed on the handler rather than on a
logger, so it also catches a dependency that helpfully logs an `Authorization`
header. `test_journal_text_is_never_logged` keeps it that way.

### Finding it

```
# Kibana, saved search "01 - All gym-tracker logs"
service.name: "gym-tracker"

# Saved search "04 - Trace one request by id" — the whole story of one click
http.request.id: "259fd665642941e0"

# Saved search "03 - Slow requests (over 500ms)"
event.duration_ms > 500
```

That last one is only possible because `event.duration_ms` is mapped as a
`float`, not as text. The mapping is the payoff from `decode_json_fields` in
[filebeat.yml](../observability/filebeat/filebeat.yml): Filebeat strips
Docker's envelope, parses the application's JSON out of `message`, and lifts
its keys to the root of the document, with `overwrite_keys: true` so the app's
`@timestamp` (when the event happened) replaces Filebeat's (when the line was
read). Without that step, a search for slow requests would be a substring match
against a blob of text.
[log-trace-example.json](../observability/results-rerun/log-trace-example.json)
records one real request at all four stages — stdout, the Docker json-file
line, the Elasticsearch document with its field types, and the Kibana query.

---

## 6. Log 2 — `injected fault: delaying this request deliberately`

### The line

Emitted by the fault injector at [faults.py:169](../faults.py), once per
delayed request, *before* the sleep:

```python
if self.latency_ms > 0 and sequence % self.latency_every_n == 0:
    self._record("latency")            # gym_faults_injected_total{kind="latency"}
    logger.warning(
        "injected fault: delaying this request deliberately",
        extra={
            "fault.kind": "latency",
            "fault.sequence": sequence,
            "fault.latency_ms": self.latency_ms,
            "url.path": path,
        },
    )
    await asyncio.sleep(self.latency_ms / 1000.0)
```

Recorded during a fault stage:

```json
{"@timestamp": "2026-09-15T06:21:26.915Z", "log.level": "warning",
 "message": "injected fault: delaying this request deliberately",
 "service.name": "gym-tracker", "event.dataset": "gym_tracker.faults",
 "log.origin.file.name": "faults.py", "log.origin.file.line": 169,
 "http.request.id": "3a504acc623745e3", "fault.kind": "latency",
 "fault.sequence": 5, "fault.latency_ms": 500.0, "url.path": "/healthz"}
```

### Why this one is worth documenting

- **It carries the same `http.request.id` as the `request served` line for the
  same request**, without `faults.py` knowing anything about request ids — that
  is the contextvar doing its job across module boundaries. Searching that one
  id returns the fault warning, the request line and uvicorn's access line
  together, and the `event.duration_ms` on the request line reads just over
  500 ms. Cause and effect, in separate documents, provably about the same
  request.
- **`fault.sequence` is the injector's own counter**, so the logs alone show
  the sampling was deterministic — every 5th request, not a random 20%.
- **It is a `warning`, not `info`, on purpose.** It should be impossible to
  read the log stream of a degraded process and not notice why. The same
  applies at startup: `FAULT INJECTION ARMED - this process is deliberately
  degraded` is logged once at [faults.py:115](../faults.py) with the whole
  configuration attached, and `/healthz` answers `"status": "degraded"` for as
  long as it is armed.
- **`asyncio.sleep`, never `time.sleep`.** On one async worker, `time.sleep`
  would block the event loop and stall every concurrent request — the
  experiment would then measure queueing everywhere instead of latency on the
  sampled requests.

### Finding it

```
# Kibana, saved search "07 - Injected faults (Part E)"
fault.kind: *

# Saved search "02 - Errors and warnings"
service.name: "gym-tracker" and log.level: ("error" or "warning" or "critical")
```

Captured in
[docs/screenshots/09-kibana-e1-fault-logs.png](screenshots/09-kibana-e1-fault-logs.png).

### The metric beside the log

Every one of these lines also increments
`gym_faults_injected_total{kind="latency"}` ([metrics.py:253](../metrics.py)),
which is always 0 in normal operation and is alerted on
(`FaultInjectionActive`,
[alerts.yml:102](../observability/prometheus/alerts.yml)). That pairing is why
the experiment's claims are checkable: the metric proves *when* the fault was
live from the time series alone, and the logs prove *which requests* it hit.
Neither asks the reader to take on trust that an environment variable was set
at the time.

---

## 7. The experiment — reproduce a problem, measure it, remove it

[scripts/experiment_anomaly.sh](../scripts/experiment_anomaly.sh). Three
stages, one variable.

### Design

| | |
|---|---|
| Hypothesis | A 500 ms delay on every 5th request moves p95 to ~500 ms while leaving p50 and the error rate alone |
| Independent variable | `FAULT_INJECTION_ENABLED` + `FAULT_LATENCY_MS=500` + `FAULT_LATENCY_EVERY_N=5` |
| Held constant | The same deterministic load in all three stages: 120 s at a target 5 rps over a fixed weighted rotation of `/healthz`, `/`, `/login`, `/progress`, `/metrics` ([load_generator.py:58](../scripts/load_generator.py)) — no RNG, so two runs issue identical request sequences |
| Measured | Client-side latency distribution (load generator), server-side histogram (Prometheus), the fault counter, the logs |
| Stages | `01-baseline` → `02-fault` → `03-recovery`, each preceded by a restart and a 20 s settle so the first scrape after start is not in the window |

Two methodological points the script enforces rather than suggests:

1. **Predictions are written to disk before the fault stage runs.**
   [predictions.md](../observability/results-rerun/predictions.md) is generated
   by the script, at the point in the run where the fault has not happened yet.
   An explanation invented after seeing the graph fits any graph.
2. **Recovery is a real stage, not an assumption.** The fault is removed, the
   process restarted, `/healthz` re-checked (`{"status":"ok"}`, not
   `degraded`), and the same load replayed a third time. A fix nobody measured
   is a hope.

### Running it

```bash
./scripts/stack.sh up                 # app, Postgres, Prometheus, Grafana, ELK, node-exporter
./scripts/experiment_anomaly.sh       # ~8 minutes: 3 x (restart + 20s settle + 120s load)
```

Overridable: `STAGE_SECONDS`, `RPS`, `FAULT_LATENCY_MS`, `FAULT_LATENCY_EVERY_N`,
`RESULTS_DIR`. Each stage writes `<stage>.json` (client-side percentiles plus
the UTC window to paste into Grafana and Kibana) and `<stage>-metrics.txt` (a
raw `/metrics` scrape), and the run ends with a combined `summary.md`.

### Results — client side

From [observability/results-rerun/summary.md](../observability/results-rerun/summary.md),
21 September, measured by the load generator from outside the app:

| Stage | Requests | Achieved rps | p50 | p95 | p99 | Mean | Error rate |
|---|---|---|---|---|---|---|---|
| 01-baseline | 598 | 4.98 | 17.2 ms | 35.6 ms | 45.5 ms | 19.3 ms | 0.00% |
| 02-fault | 406 | 3.38 | 24.2 ms | **540.0 ms** | 563.3 ms | 167.5 ms | 0.00% |
| 03-recovery | 579 | 4.82 | 21.0 ms | 83.4 ms | 277.7 ms | 34.5 ms | 0.00% |

### Results — server side

From [prometheus-percentiles.json](../observability/results-rerun/prometheus-percentiles.json),
queried from Prometheus at the end of each window with
`histogram_quantile(X, sum by (le) (rate(gym_http_request_duration_seconds_bucket{job="gym-tracker"}[2m])))`:

| Stage | p50 | p95 | p99 | `increase(gym_faults_injected_total{kind="latency"}[2m])` |
|---|---|---|---|---|
| 01-baseline | 2.52 ms | 4.78 ms | 4.98 ms | 0 |
| 02-fault | 3.26 ms | **687.46 ms** | 737.49 ms | 117.7 |
| 03-recovery | 2.64 ms | 5.64 ms | 20.88 ms | 0 |

### Predictions against outcomes

| Prediction | Outcome | |
|---|---|---|
| p95 rises to ~500 ms | client 540 ms, server 687 ms | ✅ (see the note on interpolation) |
| p50 barely moves | 2.52 → 3.26 ms server-side | ✅ |
| Mean rises by ~100 ms | rose 148 ms (19.3 → 167.5) | ✅ once the denominator is right |
| Request rate falls or holds | 4.98 → 3.38 rps | ✅ |
| `gym_faults_injected_total` 0 → non-zero | 0 → 119 | ✅ |
| Error rate unchanged at 0 | 0.00% in all three stages | ✅ |
| `gym_http_requests_in_flight` rises slightly | stayed at 1.0 in every scrape | ❌ |

### Reading the numbers

**Why the two p95s disagree (540 ms vs 687 ms).** They measure the same
requests and both are correct. The client timed each request directly. The
server has only buckets: it knows 114 requests finished within 500 ms and 139
within 750 ms, and `histogram_quantile` interpolates linearly inside that gap.
The delayed requests all landed just above 500 ms, but interpolation cannot
know that and spreads them evenly across `(0.5, 0.75]`. The honest reading of
the server-side figure is "p95 is somewhere between 500 ms and 750 ms", which
is exactly as precise as the bucket layout allows. Finer buckets would narrow
it, at the cost of more series.

**Why the client and server p50s disagree (17–24 ms vs 2.5–3.3 ms).** The
client's number includes TCP connection setup and redirect-following: a client
request to `/` while signed out is *two* server requests, the 303 and the
`/login` that follows. The server times only its own work. Both are true — the
client's is what a person experiences, the server's is what the app is
responsible for, and the gap between them is where a proxy or a network problem
would hide.

**Why the mean rose by 148 ms and not the predicted 100 ms.** The prediction
used the wrong denominator. The injector delays every 5th *server* request, and
the server served 595 requests during that stage while the client counted 406 —
the difference is redirect hops, Prometheus scraping `/metrics` every 15 s, and
the container healthcheck. So 119 delays fell across 406 client-visible
requests: 29%, not 20%. 0.29 × 500 ms = **146 ms**, against 148 ms observed.

**The consistency check that ties it together.** Total requests served during
the fault stage, summed from `gym_http_requests_total`: **595**. Faults
injected: **119**. 595 ÷ 5 = 119 exactly. The counter and the sampling rule
agree to the request.

**Why throughput fell to 3.38 rps.** The load generator is a closed loop: one
request at a time, paced to a 200 ms interval. A delayed request takes ~520 ms
and overruns that interval, so the effective spacing becomes
0.29 × 520 ms + 0.71 × 200 ms ≈ 293 ms → 3.4 requests per second. Observed:
3.38. This is a real property of latency faults worth naming — **a slowdown
looks like a traffic drop**, and a dashboard reporting "requests down 30%"
after a deploy may be showing latency, not lost users.

**The prediction that failed.** `gym_http_requests_in_flight` read exactly 1.0
in all three end-of-stage scrapes — and that 1 is the scrape's own request. The
prediction assumed delayed requests would overlap with new arrivals, but a
single-threaded closed-loop client cannot produce overlap: it waits for each
response before sending the next. Concurrency would have risen under a
concurrent load tool, or under real users. The honest conclusion is that this
experiment could not observe that metric, not that the metric is wrong.

**Recovery is clean server-side and slightly noisy client-side.** Server p95
returned to 5.64 ms against a 4.78 ms baseline and the fault counter stopped
increasing, so the app recovered completely. The client's recovery p95
(83.4 ms) and p99 (277.7 ms) sat above baseline, and since the server histogram
covering those same requests shows no such tail, whatever caused it was in the
client or on the host, not in the application. The 19 September run of the same
experiment ([results-live/summary.md](../observability/results-live/summary.md))
recovered cleanly on both sides: 29.1 → 524.3 → 31.8 ms client-side p95.

### What the four things looked like together

| | Baseline | Fault | Recovery |
|---|---|---|---|
| Metric 1 (histogram p95) | 4.78 ms | 687 ms | 5.64 ms |
| The chart | four flat lines | p95 and p99 lift, p50 and mean stay low | flat again |
| Log 1 (`request served`) | `event.duration_ms` 1–5 ms | ~1 in 5 above 500 ms | back under 10 ms |
| Log 2 (injected fault) | absent | 119 warnings, `fault.sequence` 5, 10, 15, … | absent |
| `/healthz` | `{"status":"ok"}` | `{"status":"degraded","fault_injection":{…}}` | `{"status":"ok"}` |

Metrics 2 and 3 stayed flat at 0 throughout, which is correct: the load
generator never signs in and never saves an entry, so no sets were written and
no drafts opened. A latency fault in the middleware has nothing to do with the
business metrics, and an experiment that moved them would mean the fault was
not doing what it claimed.

### Why the fault is configuration and not code

Editing a route to add a `sleep` would work once and then live on in the source
history and in whatever gets deployed next. Instead the fault is read from the
environment at startup ([faults.py:87](../faults.py)), and nothing in the
request path differs between a healthy run and a degraded one. The safety
rails:

- Off unless `FAULT_INJECTION_ENABLED` is explicitly true — a latency value on
  its own does nothing, two variables have to agree.
- Refuses to arm when `APP_ENV=production`, whatever else is set, and logs an
  `error` saying so.
- The delay is clamped to 10 s, so `5000` typed where `500` was meant cannot
  pin a worker for a minute.
- Configuration is read once at construction, so settings cannot drift
  mid-experiment. Changing a fault means restarting the process — which is also
  what makes "remove the problem and repeat the test" an honest step rather
  than a mutation of live state.

Nine tests in [tests/test_observability.py](../tests/test_observability.py)
cover the injector alone, including `test_disabled_by_default`,
`test_a_latency_value_alone_does_nothing`, `test_refuses_to_arm_in_production`
and `test_latency_is_clamped`.

---

## 8. Reproducing everything in this file

```bash
./scripts/stack.sh up            # the whole stack, then wait for health
./scripts/stack.sh urls          # where everything is
./scripts/setup_kibana.sh        # data view + the eight saved searches
```

| Thing | Where to look |
|---|---|
| Metric 1, raw | <http://localhost:8000/metrics> — grep `gym_http_request_duration_seconds` |
| Metric 1, queried | Prometheus <http://localhost:9090> — paste the PromQL from section 1 |
| Metrics 2 and 3 | Same endpoint: `gym_sets_written_total`, `gym_review_drafts_open`. Both need real use — sign in, paste an entry, save it, or run `scripts/submit_reviewed_entries.py`, which does it without a Groq key |
| The chart | Grafana <http://localhost:3000> (`admin`/`admin`) → *Gym Tracker — Application performance* → Latency row |
| Log 1 | Kibana <http://localhost:5601> → Discover → saved search *01* or *04* |
| Log 2 | Kibana → saved search *07 - Injected faults*, while the experiment runs |
| The experiment | `./scripts/experiment_anomaly.sh`, then read `observability/results/summary.md` |

The instrumentation can also be verified without any of that:

```bash
python -m pytest tests/test_observability.py -q     # 56 tests
```

---

## 9. Where every claim here comes from

| Claim | Source |
|---|---|
| Metric definitions, buckets, label rules | [metrics.py](../metrics.py) |
| Where each metric is recorded | [app.py](../app.py) — middleware (:339–:481), `POST /log` (:1271), `POST /save` (:1396–:1406) |
| Log formatting, redaction, request ids | [logging_setup.py](../logging_setup.py) |
| Fault behaviour and safety rails | [faults.py](../faults.py) |
| Panel JSON, queries, units | [01-app-performance.json](../observability/grafana/dashboards/01-app-performance.json) |
| Scrape interval and targets | [prometheus.yml](../observability/prometheus/prometheus.yml) |
| Alert thresholds | [alerts.yml](../observability/prometheus/alerts.yml) |
| Log shipping and field mapping | [filebeat.yml](../observability/filebeat/filebeat.yml) |
| Saved searches | [saved-objects.ndjson](../observability/kibana/saved-objects.ndjson) |
| Client-side experiment numbers | [results-rerun/summary.md](../observability/results-rerun/summary.md) and its `0*.json` |
| Server-side percentiles and fault counts | [results-rerun/prometheus-percentiles.json](../observability/results-rerun/prometheus-percentiles.json) |
| Raw exposition excerpts, request totals | [results-rerun/02-fault-metrics.txt](../observability/results-rerun/02-fault-metrics.txt) |
| Counter-reset trace | [results-rerun/metric-trace-sets-written.json](../observability/results-rerun/metric-trace-sets-written.json) |
| One request through the whole log pipeline | [results-rerun/log-trace-example.json](../observability/results-rerun/log-trace-example.json) |
| Screenshots | [docs/screenshots/](screenshots/) — 01 (the chart mid-fault), 09 (fault logs in Kibana), 10 and 07 (Prometheus, fault and recovery) |

Line numbers are against the commit this file was written at; the prose names
the function in each case, which survives a refactor better than a number does.
