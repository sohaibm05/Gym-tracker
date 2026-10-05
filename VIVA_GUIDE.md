# Viva Guide: Observability (Assignment 1)

The highest-priority things to understand and be able to say out loud. The
interactive version, with code excerpts, simulators and self-tests, is the
**Viva Tutor**:

```bash
python3 -m http.server 8090        # from the repository root
# open http://localhost:8090/viva-tutor/
```

Line numbers below are as of this commit. The tutor recomputes them from the
source every time `python3 viva-tutor/build_snippets.py` runs.

---

## 1. Architecture in 30 seconds

> The app is FastAPI with Postgres, plus Groq for parsing. **Metrics are pulled**:
> Prometheus GETs `/metrics` from the app and Node Exporter every **15 s**, keeps
> **15 days**, and Grafana draws three dashboards stored as JSON in the repo.
> **Logs are pushed**: the app prints one JSON line per event to stdout, Docker's
> json-file driver saves it (10 MB × 3), Filebeat parses it into fields and
> ships it to Elasticsearch, which keeps it **7 days** (ILM), and Kibana searches it.
> The app depends on none of the monitoring.

What happens if a component dies:

| Component | Effect |
|---|---|
| App | Outage; `up{job="gym-tracker"}=0`, TargetDown fires after 1m; counters restart at 0 |
| Postgres | Saves and charts fail → 5xx, `gym_http_exceptions_total`, "request failed" logs. **`/healthz` stays ok**: it doesn't query the DB |
| Prometheus | Dashboards blank, permanent gap for missed scrapes; app unaffected |
| Filebeat | Logs wait in the Docker files; it resumes from its registry offset |
| Elasticsearch | Filebeat retries; logs are lost only if Docker rotates the file first |
| Kibana / Grafana | No UI; collection continues |

## 2. Metrics: four types, 19 metrics, all in `metrics.py`

| Type | Answers | My example | Updated at |
|---|---|---|---|
| Counter | how many, ever | `gym_http_requests_total{method,route,status}` | `app.py:459` (middleware) |
| Counter | | `gym_sets_written_total` (no labels) | `app.py:1400` `.inc(result.inserted_sets)` |
| Gauge | how many right now | `gym_review_drafts_open` | `DraftTracker` → `.set(len(dict))` `metrics.py:463` |
| Histogram | distribution → p95 | `gym_http_request_duration_seconds{method,route}` | `app.py:456` `.observe(seconds)` |
| Summary | average (sum ÷ count) | `gym_sets_per_entry` | `app.py:1402` |

- **Why a Histogram for latency?** Buckets can be `rate()`d over any window and
  summed across instances, then `histogram_quantile` estimates p95. The Python
  client's Summary has **no quantiles**, only `_sum` and `_count`.
- **Labels are always bounded.** `route` is the route *template*, and unmatched
  paths collapse to `"unmatched"`. No user ids, request ids or raw paths.
  `tests/test_observability.py::test_labels_are_bounded_dimensions_only`
  enforces this.
- **Labelled counters start at 0** (`metrics.py:266`). Otherwise the first
  sample already reads 1 and `increase()` misses the first event (the
  "blocked 0" bug).
- **The drafts gauge is backed by a dict** `{(user_id, session_date): opened_at}`:
  opened at `POST /log`, closed at a successful `POST /save`, aged out after
  30 min, capped at 1000. The user id is a dict key, never a label.
- `gym_http_requests_in_flight` reads 1 in every scrape. That's the `/metrics`
  request counting itself.

**p95 query, inside out:**
`histogram_quantile(0.95, sum by (le) (rate(gym_http_request_duration_seconds_bucket[5m])))`.
Take the bucket counters, use the last 5 min, convert to per-second rates,
add routes together keeping `le`, then interpolate the 95th-percentile rank
inside its bucket. 5m is a trade-off between responsiveness and noise. A
15 s scrape gives ~20 samples.

## 3. Logs

- One JSON line per event in ECS names. The formatter (`EcsJsonFormatter.format`,
  `logging_setup.py:212`) adds `@timestamp`, `log.level`, `message`,
  `service.name`, origin, and **`http.request.id`**.
- The request id is a **ContextVar** set by the middleware (`app.py:371`). It's
  per async task, so concurrent requests on one thread don't mix ids. It's
  also returned as the `x-request-id` header.
- Know two logs cold:
  - **"request served"** (`app.py:463`, info): `http.request.method`, `url.path`
    (raw), `http.route` (template), `http.response.status_code`, `event.duration_ms`.
  - **"request failed"** (`app.py:425`, error, `logger.exception`): adds
    `error.type`, `error.message`, `error.stack_trace` and status 500.
- **Never logged:** journal text, passwords, tokens, cookies, API keys, usernames
  on failed logins. There are two backstops in `logging_setup.py`:
  `SensitiveDataFilter` scrubs the message text, and `scrub_value` in the
  formatter blanks keys in `SENSITIVE_KEYS` and scrubs exception text.

## 4. Follow one metric: `gym_sets_written_total`

`.inc(result.inserted_sets)` → value in process memory (17) → `GET /metrics`
prints `gym_sets_written_total 17.0` → Prometheus scrapes every 15 s and adds
`job/instance/service` → series **0 → 17 → 0 (app restart) → 3 → 16** →
`increase(gym_sets_written_total[24h])` treats the drop as a counter reset:
**17 + 16 = 33** (33.0007 from extrapolation) → "Sets logged (24h)" shows 33.

## 5. Follow one log: request `259fd665642941e0` (GET /login, 191.5 ms)

1. `logger.info("request served", extra={...})`
2. stdout: `{"@timestamp": "2026-09-22T09:12:17.329Z", "log.level": "info", ..., "http.request.id": "259fd665642941e0", ..., "event.duration_ms": 191.5}`
3. Docker wraps it as a string: `{"log":"{\"@timestamp\": ...}\n","stream":"stdout","time":"...332421916Z"}`
4. Filebeat only reads containers labelled `co.elastic.logs/enabled=true`. It
   strips the wrapper, then `decode_json_fields` runs with `target: ""` (top
   level), `overwrite_keys` (the app's timestamp wins) and `expand_keys`
   (dotted → nested).
5. Elasticsearch: `@timestamp` date, `log.level` keyword, `message`
   match_only_text, **`http.request.id` keyword**, `url.path` wildcard,
   **status long, duration float**. The types come from Filebeat's ECS
   template. `event.duration_ms` isn't ECS, so it's mapped dynamically.
6. Kibana: `http.request.id: "259fd665642941e0"` returns 2 lines: this one and
   uvicorn's access log.

## 6. E1: fault injection

Every 5th request gets +500 ms (`FAULT_INJECTION_ENABLED=true FAULT_LATENCY_MS=500 FAULT_LATENCY_EVERY_N=5`,
then `docker compose up -d --force-recreate app`). The delay is `asyncio.sleep`,
**inside** the timed region: the timer starts before the fault hook.

| Stage | p50 | p95 | p99 | faults | fault logs | errors | rps |
|---|---|---|---|---|---|---|---|
| Normal | 2.5 ms | 4.8 ms | 5.0 ms | 0 | 0 | 0 | 4.98 |
| Fault | 3.3 ms | **687.5 ms** | 737.5 ms | 118 | 118 | 0 | 3.38 |
| Recovered | 2.6 ms | 5.6 ms | 20.9 ms | 0 | 0 | 0 | 4.82 |

- **Why p95 moved but p50 didn't:** 20% of requests are slow. The median falls
  among the fast 80%; the 95th percentile falls among the slow 20%.
- **Why 687.5 and not 500:** linear interpolation inside the 0.5–0.75 s bucket.
  0.5 + ((0.95 − 0.80) / 0.20) × 0.25 = **0.6875 s**. The same formula gives
  p99 = 0.7375 s, exactly what was measured.
- **Why throughput dropped:** the load generator waits for each response
  (closed loop).
- **No errors:** an uptime check or error-rate alert would have stayed green.
  Only the percentiles showed the problem.
- **Prediction** was written to `observability/results-rerun/predictions.md`
  before the run, and every predicted direction was right.

## 7. E2: cardinality

- One time series = metric name + one exact set of label values.
- `demo_requests_total{endpoint, request_id}`: 1, 10, 25, 50, 100 series for
  1, 10, 25, 50, 100 unique ids. It holds at **100** for 120 ids because of the
  cap. `demo_requests_safe_total{endpoint}` stays at **1**.
- After removing the label, an instant query returns no data, but **history
  stays** until 15-day retention. Queried at 04:57:30 it still returns 100.
- 100 rps × 86,400 s = **8.64 million** new series a day.
- **Request ids belong in logs**: logs are built to find one event, and their
  cost grows with events, not label combinations.

## 8. Traps: know these before the examiner does

1. The report refers to `docs/REPORT.md`. **That file was deleted** in the last
   commit, and the Dockerfile comment still points at it.
2. **Live-workout sets aren't counted** in `gym_sets_written_total`. Only
   `POST /save` increments it. The report admits this.
3. **406 client requests vs 118 faults.** The injector counts server-side
   requests: redirects are followed and scrapes count too. The fault-stage
   dump shows 595 server requests, and 595 / 5 = 119.
4. **The baseline 2.5 / 4.8 / 5.0 ms just means "under 5 ms".** It's
   interpolation inside the first bucket.
5. The **SlowRequests alert (p95 > 1 s) wouldn't fire** at 687 ms. There's no
   Alertmanager, so alerts only appear in Prometheus.
6. **Key-based redaction happens in the formatter**, not in
   `SensitiveDataFilter`. **`user.id` is logged** in business events; it's an
   internal id, not a username.
7. The report's `faults.py:168` is one line off. The log call is at
   `faults.py:169`. Every other line reference matches.
8. An Elasticsearch datasource is provisioned in Grafana, but **no panel uses
   it**.

## 9. Commands

| | |
|---|---|
| `docker compose up -d --build` | start everything |
| `./scripts/setup_kibana.sh` | import the data view and 8 saved searches |
| `python scripts/load_generator.py --duration 120 --rps 5` | traffic |
| `./scripts/experiment_anomaly.sh` | E1 |
| `docker compose --profile experiment up -d cardinality-demo` | E2 |
| `docker compose down` | stop and **keep** the volumes |
| `docker compose down -v` | stop and **delete** every volume |
