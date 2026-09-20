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

1038 automated tests pass, 63 of them covering the observability layer added
here.

**Without a Groq key.** Pasting an entry needs `GROQ_API_KEY`; everything else
works without one. Until 20 September, `POST /log` on a server with no key raised
an unhandled `RuntimeError` and returned a 500 — found by walking through the app
as a user would, and caught by the logging and metrics on the way (see Part C).
It now shows *"Parsing is unavailable because this server has no GROQ_API_KEY
configured. Nothing was saved."* on the normal "Could not parse that entry" page,
with a `warning`-level log line (`event.action: extraction_unavailable`). The
check is a `MissingApiKeyError` raised inside `extract_entities` — a subclass of
the existing `ExtractionError`, so every caller that already handled a failed
extraction handles this too. Two tests cover it in `tests/test_pipeline.py`.

A second input method — an installable app for logging set by set during a
workout — was added after this assignment was written. Its API routes run through
the same middleware and are labelled by route template like the rest
(`/api/session/{session_id}/sets`), so the **application** metrics and request
logs cover it, and it added endpoints without adding cardinality. The
**business** metrics do not: they are recorded in the journal save path, and a
set logged through the workout app does not increment `gym_sets_written_total`.
That is a real gap in coverage rather than a design choice. See the README for
what the app does.

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
| `gym_http_requests_total` | Every response served. Numerator and denominator of the error rate. | 1 | `method`, `route`, `status` | `app.py:419` (exception path) / `459` (success path), both in the `log_requests` middleware |
| `gym_http_exceptions_total` | Requests that raised out of the handler. Distinguishes a crash from a deliberate 500. | 1 | `route`, `exception` | `app.py:422` |
| `gym_workout_entries_total` | **Business.** Journal entries reaching a terminal state at save. | 1 | `outcome` = `saved`/`blocked`/`duplicate_held` | `app.py:1332` (blocked), `1374` (duplicate_held), `1397` (saved) |
| `gym_sets_written_total` | **Business.** Individual sets inserted — the product's unit of value. | 1 | — | `app.py:1400` |
| `gym_bodyweight_entries_written_total` | **Business.** Bodyweight readings inserted. | 1 | — | `app.py:1401` |
| `gym_duplicate_decisions_total` | **Business.** How the duplicate prompt was answered. | 1 | `decision` = `held`/`overridden` | `app.py:1349`, `1375` |
| `gym_llm_extractions_total` | **Business.** Groq extraction calls by outcome. | 1 | `outcome` = `success`/`rate_limited`/`error` | `pipeline.py:1042` |
| `gym_weekly_reports_total` | **Business.** Reports generated, by narration success. | 1 | `narration` = `ok`/`failed` | `app.py:1509` |
| `gym_logins_total` | Authentication outcomes. Failures without successes = credential stuffing. | 1 | `result` = `success`/`failure`/`rate_limited` | `app.py:851`, `874`, `889` |
| `gym_faults_injected_total` | Part E only. Proves from metrics alone when a fault was live. | 1 | `kind` = `latency`/`error` | `faults.py:138` |

#### Gauges — a value that goes up and down

| Metric | Purpose | Unit | Labels | Where recorded |
|---|---|---|---|---|
| `gym_http_requests_in_flight` | Concurrent requests. The saturation signal on a single worker. | 1 | — | `app.py:377` (inc) / `447` (dec, in `finally`) |
| `gym_llm_extractions_in_flight` | Extractions waiting on Groq right now. | 1 | — | `pipeline.py:1175` / `1212` |
| `gym_review_drafts_open` | **Business.** Entries parsed but not yet confirmed. | 1 | — | `metrics.py:269` (`DraftTracker` at `453`), driven from `app.py:1272` (opened) / `1406` (closed) |
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
| `gym_http_request_duration_seconds` | The headline latency metric. | seconds | `method`, `route` | `app.py:416` (exception path) / `456` (success path) |
| `gym_llm_extraction_duration_seconds` | **Business.** One Groq extraction, retries included. | seconds | — | `pipeline.py:1042` |
| `gym_db_commit_duration_seconds` | Time inside the committing transaction. | seconds | — | `app.py:1356` |

Bucket boundaries are chosen per metric, because the three live on different
scales (`metrics.py`): HTTP latency keeps resolution from 5ms to 10s, the LLM
from 0.25s to 60s, the database from 5ms to 5s. **Buckets are the resolution
limit of every percentile computed from them** — see the Part E result, where
this is visible in the numbers.

#### Summaries — client-side sum and count only

| Metric | Purpose | Unit | Labels | Where recorded |
|---|---|---|---|---|
| `gym_entry_text_bytes` | **Business.** Size of a pasted entry. Drives the token budget, so it is a cost metric too. | bytes | — | `app.py:1264` |
| `gym_sets_per_entry` | **Business.** Sets written per saved entry. | 1 | — | `app.py:1402` |

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
| Error rate | `(sum(rate(gym_http_requests_total{status=~"5.."}[5m])) or vector(0)) / clamp_min(sum(rate(gym_http_requests_total[5m])), 0.0001)` | 5xx share. `clamp_min` avoids a divide-by-zero making the panel read `NaN` when there is no traffic. `or vector(0)` is there because a healthy app has *no* `status="5xx"` series at all — without it the panel read "No data" whenever nothing was failing, which looks exactly like a broken panel. Found by looking at the rendered dashboard; the query was valid and returned nothing. |
| p95 latency | `histogram_quantile(0.95, sum by (le) (rate(gym_http_request_duration_seconds_bucket[5m])))` | 95% of requests in the last 5 minutes were faster than this. |
| p99 latency | same with `0.99` | The tail. |
| Latency percentiles | p50, p95, p99 and `sum(rate(..._sum[5m])) / sum(rate(..._count[5m]))` on one axis | **The most instructive panel here**: the mean sits below p95 and barely moves when a minority of requests get slow. |
| Latency distribution | `sum by (le) (increase(gym_http_request_duration_seconds_bucket[1m]))` (heatmap) | Every bucket over time. A latency fault appears as a second band before any percentile line moves far. |
| p95 by route | `histogram_quantile(0.95, sum by (le, route) (rate(gym_http_request_duration_seconds_bucket[5m])))` | Which route is slow. `/log` carries the LLM call and is expected to sit high. |
| In flight | `gym_http_requests_in_flight` | A gauge, read directly — no `rate()`. |
| Injected faults | `sum by (kind) (increase(gym_faults_injected_total[1m]))` | Zero except during Part E. |

The "Unhandled exceptions" panel, `sum by (route, exception) (rate(gym_http_exceptions_total[5m]))`,
has the same `or vector(0)` for the same reason.

![Application performance dashboard over the Part E1 experiment](screenshots/01-grafana-app-performance.png)

*The application dashboard over the whole Part E1 run, 15:55–16:05 UTC. Grafana
draws in the browser's time zone, which here is UTC+5, so the axis reads
20:55–21:05; the three stages are 20:57–20:59 (baseline), 20:59–21:01 (fault)
and 21:02–21:04 (recovery). Three things to read off it: the p95 and p99 lines
jump only during the fault stage while p50 stays flat along the bottom; the
latency heatmap grows a second band in the 500–750ms buckets, separate from the
main band under 5ms; and "Injected faults" is non-zero only in that same window.
Error rate stays at 0.00% throughout. The dip in request rate between the stages
is the app restarting.*

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

![Business metrics dashboard](screenshots/02-grafana-business.png)

*The business dashboard after six journal entries were submitted through the
review form. Four were saved (5, 4, 6 and 3 sets — **18 sets** and **2 bodyweight
readings** in total), one was a re-submission held by the duplicate guard, and
one was blocked by the review form for an invalid rep count. Database commit
time is p50 17.5ms and p95 85ms.*

**How this data was produced, stated plainly.** The normal path is paste →
`POST /log` (Groq extracts) → review → `POST /save`, and only the first step
needs a `GROQ_API_KEY`. This machine has none, so `scripts/submit_reviewed_entries.py`
submits the review form to `POST /save` directly, in exactly the shape the review
page renders. Everything from `/save` onwards is the real application path, so
every business metric is recorded by the app itself; nothing was written to the
database or to Prometheus by hand. The three panels under "The language model"
are empty for the same reason: no extraction was ever attempted.

One of the five saves shown is an **empty entry with 0 sets**, which I submitted
by mistake while working out the form's field names. It wrote no rows, but a
Summary cannot be un-observed, so it is why "Avg sets per saved entry" reads
**3.60** (18 ÷ 5) rather than 4.5 (18 ÷ 4). I have left it in rather than
restart the app to hide it; it is also a fair example of what this metric is for,
since a real run of 0-set saves is exactly the extraction regression described
below.

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
has to reach it across that boundary. The target is the **bridge gateway
address**, `172.28.77.1:9100`, pinned by an explicit `ipam` subnet on `gym-net`
in `docker-compose.yml` so the address is stable and can be written into
`prometheus.yml`.

The obvious choice, `host.docker.internal:9100`, does **not** work on Docker
Desktop for Windows, and finding out why is the most useful thing this part of
the setup taught me. On Docker Desktop the containers do not run on Windows;
they run inside a WSL2 Linux VM. `host.docker.internal` resolves to the
*Windows* host, while `node-exporter` — being in the "host" network namespace —
is listening inside the *Linux VM*. The name and the process are on opposite
sides of the VM boundary, so the scrape fails with `connection refused` even
though the exporter is running perfectly. The bridge gateway is the VM itself,
which is the machine the containers actually live on, so that is the correct
address.

**The machine being measured**, read from `node_uname_info`:

```
nodename: docker-desktop
release:  6.18.33.2-microsoft-standard-WSL2
instance: 172.28.77.1:9100
machine:  docker-host      (the label set in prometheus.yml)
```

So on this setup `machine="docker-host"` is **the WSL2 Linux VM**, not the
Windows laptop. Every container in the stack is a process on that VM's kernel,
so these totals cover all of them — but Windows' own CPU and memory use is *not*
in these numbers. On a Linux host the same config measures the host directly and
the distinction disappears. Measuring the Windows machine itself would need
`windows_exporter` running natively, which is a different target, not a fix to
this one.

| Panel | Query | Note |
|---|---|---|
| CPU busy | `1 - avg(rate(node_cpu_seconds_total{mode="idle"}[5m]))` | There is no "CPU %" metric. It is derived from a counter of seconds spent idle. |
| CPU by mode | `avg by (mode) (rate(node_cpu_seconds_total[5m]))` | A high `iowait` band means the disk is the bottleneck, which a single CPU% number cannot tell you. |
| Memory used | `1 - (node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes)` | `MemAvailable`, not `MemFree` — `MemFree` excludes the page cache and makes a healthy machine look nearly full. |
| Disk used | `1 - (node_filesystem_avail_bytes{mountpoint=~"/\|/var/lib"} / node_filesystem_size_bytes{mountpoint=~"/\|/var/lib"})` | Elasticsearch stops accepting writes at its flood-stage watermark, so a full disk silently ends log ingestion. The matcher is not just `/`: on the Docker Desktop VM the data disk is mounted at `/var/lib`, and `mountpoint="/"` alone returned **no data at all**. A panel that silently shows nothing is worse than one that is obviously broken — this one was found by checking that every panel's query actually returns series, not by looking at the dashboard. |
| Disk I/O time | `sum by (device) (rate(node_disk_io_time_seconds_total[5m]))` | Seconds of I/O per second. Approaching 1 = saturated, which throughput alone does not reveal. |
| Network | `sum by (device) (rate(node_network_receive_bytes_total{device!="lo"}[5m]))` | Loopback excluded. |

![Host dashboard from node-exporter](screenshots/03-grafana-host.png)

*The host dashboard for the WSL2 VM, over the 30 minutes after the stack was
restarted on 20 September. 12 cores, 7.56 GiB of memory (38% used), 6.3% of the
data disk used. The network interfaces are the VM's Docker bridges
(`br-…`, `docker0`), which is what you would expect when the machine being
measured is the VM rather than Windows. The uptime of 8.73 minutes is the VM's:
Docker Desktop had been restarted, and node-exporter reports the kernel it runs
on, not the laptop.*

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

**A working search, on the live stack.** One `POST /save`, traced by its request
id:

![Kibana Discover tracing one request by id](screenshots/04-kibana-trace-request.png)

*`http.request.id:"8d59a31cdc6f454c"` returns exactly three documents, written by
three different loggers that never mention the id themselves: the business event
from the route (`journal entry saved`, `event.action: entry_saved`), the
middleware's request log (`request served`, `/save`, 200, 9.4ms), and uvicorn's
access log. The contextvar attached the same id to all three. The columns are
real indexed fields, not substrings of a message — which is what
`decode_json_fields` in the Filebeat config is for. Times are shown in the
browser's zone, UTC+5.*

**Something the observability caught.** While checking Part A, `POST /log`
without a `GROQ_API_KEY` turned out to return an unhandled 500. The pipeline
caught it end to end before I had noticed it any other way: a `log.level: error`
line with `error.type: RuntimeError` and `error.message: GROQ_API_KEY is not set`,
carrying the request id, and `gym_http_exceptions_total{route="/log",exception="RuntimeError"}`
going from absent to 1. It is fixed now — see the note in Part A.

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

**Observed values**, from `observability/results-live/02-fault-metrics.txt` — the
scrape taken at the end of the fault stage of the live run:

```
gym_http_requests_total{method="GET",route="/healthz",status="200"} 142.0
gym_http_request_duration_seconds_count{method="GET",route="/healthz"}  142.0
gym_http_request_duration_seconds_sum{method="GET",route="/healthz"}    12.55208804299673
gym_faults_injected_total{kind="latency"} 121.0
```

142 requests to `/healthz` taking 12.55 seconds in total — a mean of 88.4ms, from
a route whose real cost is under 3ms, because 25 of those 142 were delayed by half
a second each. The same arithmetic in PromQL, which is what the panel actually
runs:

```promql
rate(gym_http_request_duration_seconds_sum{route="/healthz"}[2m])
  / rate(gym_http_request_duration_seconds_count{route="/healthz"}[2m])
```

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

Three stages, **120s each at 5 rps**, run against the full containerised stack
with Prometheus scraping throughout. 120s at a 15s scrape interval gives 8
scrapes per stage, so a `rate()` over `[2m]` is computed from real samples
rather than interpolated from two points. Full data in
`observability/results-live/`.

**Client-side** (measured by the load generator, `*.json`):

| Stage | Requests | Achieved rps | p50 | **p95** | p99 | Mean | Errors |
|---|---|---|---|---|---|---|---|
| 01-baseline | 599 | 4.99 | 6.3ms | **29.1ms** | 38.9ms | 12.2ms | 0% |
| 02-fault | 415 | 3.45 | 20.2ms | **524.3ms** | 531.2ms | 156.1ms | 0% |
| 03-recovery | 599 | 4.99 | 6.2ms | **31.8ms** | 41.0ms | 12.9ms | 0% |

**Server-side**, queried from Prometheus with `histogram_quantile` at the end of
each stage window (`observability/results-live/prometheus-percentiles.json`):

```promql
histogram_quantile(0.95, sum by (le) (rate(gym_http_request_duration_seconds_bucket{job="gym-tracker"}[2m])))
sum(increase(gym_faults_injected_total{kind="latency"}[2m]))
```

| Stage | p50 | **p95** | p99 | Mean | rps | `gym_faults_injected_total{kind="latency"}` |
|---|---|---|---|---|---|---|
| 01-baseline | 2.5ms | **4.8ms** | 5.0ms | 0.5ms | 7.13 | *(absent)* |
| 02-fault | 3.1ms | **687.1ms** | 737.4ms | 98.7ms | 5.02 | **120** |
| 03-recovery | 2.5ms | **4.8ms** | 5.0ms | 0.5ms | 7.12 | *(absent)* |

The fault counter is *absent*, not zero, in the healthy stages: a labelled
counter that has never been incremented exports no sample at all. `01-baseline-metrics.txt`
contains no `gym_faults_injected_total` line; `02-fault-metrics.txt` has
`gym_faults_injected_total{kind="latency"} 121.0`. (121 rather than 120 because
Docker's container health check hit `/healthz` before the measured window opened
and drew one delay — traced in the Kibana screenshot below. It is a small
demonstration that the counter records real events, not stage boundaries.)

The server-side `rps` is higher than the client's 5 rps because it counts
Prometheus's own `/metrics` scrapes as well as the load generator's traffic.

Time windows for the Grafana/Kibana pickers:

```
01-baseline   2026-09-19T15:56:52Z .. 2026-09-19T15:58:53Z
02-fault      2026-09-19T15:59:16Z .. 2026-09-19T16:01:17Z
03-recovery   2026-09-19T16:01:40Z .. 2026-09-19T16:03:40Z
```

The raw histogram for `/healthz` during the fault stage shows the distribution
directly:

```
gym_http_request_duration_seconds_bucket{le="0.25",route="/healthz"}   117.0
gym_http_request_duration_seconds_bucket{le="0.5", route="/healthz"}   117.0
gym_http_request_duration_seconds_bucket{le="0.75",route="/healthz"}   142.0
gym_http_request_duration_seconds_count{route="/healthz"}              142.0
gym_http_request_duration_seconds_sum{route="/healthz"}                12.552
```

117 requests under a quarter of a second, **nothing at all** between 0.25s and
0.5s, then 25 more between 0.5s and 0.75s. Bimodal — two populations, not one
slow average. The baseline for the same route has all 225 observations in the
smallest bucket (`le="0.005"`).

**The logs agree with the metrics independently.** Counted in Elasticsearch over
the same three windows:

| Query | baseline | fault | recovery |
|---|---|---|---|
| `fault.kind: "latency"` | 0 | **120** | 0 |
| `event.duration_ms > 500` | 0 | **120** | 0 |
| `log.level: "error"` | 0 | **0** | 0 |

120 fault warnings, 120 slow requests, zero errors — matching the 120 counted by
Prometheus over the same window, from a completely separate pipeline. Two
independent systems, one number.

![Kibana showing the injected-fault warnings](screenshots/05-kibana-injected-faults.png)

*The saved search "07 - Injected faults", `fault.kind:"latency"`, over
15:59:00–16:01:30 UTC (20:59–21:01:30 in the browser's UTC+5). This wider window
returns **121**, and the histogram shows where the extra one comes from: a single
isolated fault at 15:59:10 UTC, then a gap, then the continuous block from
15:59:16 when the load generator started. Tracing that one
document by its request id, its access-log line reads `127.0.0.1 - "GET /healthz"`:
the caller was **Docker's own container health check** (`HEALTHCHECK` in the
`Dockerfile`, every 10s, from inside the container), not the experiment script,
whose requests arrive from the bridge gateway `172.28.77.1`. It was the fifth
request the new process served, so it drew the first delay, and it really was
held 500ms (15:59:10.386 → 10.887). It is the same 121 that
`gym_faults_injected_total` reports in `02-fault-metrics.txt`: the metric and the
log disagree with the stage window by the same one event, for the same reason.*

*I first assumed that extra event came from the script's readiness probe, and
wrote that down. Following the request id showed otherwise. It is a small case,
but it is the pattern this assignment is about: the guess was plausible, and the
log settled it in one query.*

The same experiment on the Grafana side is the application dashboard screenshot
in Part B.

#### What changed, and what it means

**The prediction held.** Every one:

- **p95 rose by 18×** (29.1ms → 524.3ms) while **p50 stayed in single-digit
  milliseconds** on the server side (2.5ms → 3.1ms). This is the entire argument
  for percentiles. A dashboard showing only the median would have reported a
  perfectly healthy service while one request in five took half a second.
- **The mean is the worst of both** — 156.1ms describes no actual request. No
  request took 156ms; they took either ~6ms or ~520ms. An average over a bimodal
  distribution is a number with no referent.
- **Throughput fell 31%** (4.99 → 3.45 rps) with the offered load unchanged. The
  client is closed-loop: it waits for each response, so latency converts directly
  into lost throughput. On a real service this is how a latency problem becomes a
  capacity problem.
- **The error rate stayed at 0%.** Every request returned 200. A monitoring setup
  watching only errors and uptime would have seen nothing at all.
- **`gym_faults_injected_total{kind="latency"}` went absent → 120 → absent**,
  which is what makes this evidence rather than an assertion that a variable was
  set.

**Cause.** `faults.py` delays every 5th request by 500ms inside the timed
region. `fault.sequence` in the logs confirms the sampling was exactly
deterministic: 5, 10, 15, 20, 25 …

**Effect on users.** One request in five takes half a second longer. But a page
load involves several requests, so in practice *most page loads feel slow* rather
than one in five — which is why p95 predicts how an app feels far better than an
average does.

**Recovery.** Stage 3 returns to baseline within measurement noise — server-side
p95 is **4.8ms in both** the baseline and recovery stages, identical to three
significant figures — and `/healthz` goes back to `{"status": "ok"}`. Verified,
not assumed.

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

**2. Server-side p95 (687.1ms) and client-side p95 (524.3ms) disagree by 163ms.**

Not an error — a property of histograms, and the effect reproduced almost exactly
when the experiment was re-run against the live stack (686.2ms vs 506.1ms on the
first run, 687.1ms vs 524.3ms on this one). The real observations cluster just
above 0.5s, but the surrounding buckets are `le="0.5"` and `le="0.75"`, so
`histogram_quantile` interpolates linearly across that 250ms gap and lands near
687ms. The raw buckets above show why the interpolation has nothing better to go
on: 117 observations at `le="0.25"`, still 117 at `le="0.5"`, then 142 at
`le="0.75"`. Every delayed request is somewhere in that one 250ms-wide bucket and
the histogram cannot say where.

**A histogram's percentile is only as precise as its bucket boundaries.** The
client, holding every raw sample, can report the true value; Prometheus trades
that precision for the ability to aggregate across instances and any time window.
Knowing which number is which matters before anyone sets an SLO on it — an SLO of
"p95 under 600ms" would be reported as breached by Prometheus and as met by the
client, on the same requests.

That this reproduced to within 1ms across two runs on different days, on
different infrastructure (direct uvicorn the first time, the full container stack
the second), is the strongest evidence that it is a systematic property of the
bucket layout rather than noise.

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

**Through Prometheus (`--stack`)** — the same comparison, measured end to end
against the running server rather than by counting lines of exposition text:

| Stage | `count(demo_requests_total)` | `count(demo_requests_safe_total)` |
|---|---|---|
| With `request_id` label | **100** | 1 |
| Label removed, app restarted | *no data* | 1 |

#### What I expected here, and what actually happened

I expected the second row to still read 100 — that removing a label stops new
series being created but leaves the existing ones in place until retention
expires. That is the usual way this is explained, and it is what an earlier draft
of this report asserted. **The measurement contradicted it**, and the reason is
worth more than the original point.

`count(demo_requests_total)` as an *instant query* returns **no data** once the
label is removed. But the same query evaluated at a past timestamp still returns
100. Running it at a series of `time=` values maps the boundary exactly:

| Query time (UTC) | `count(demo_requests_total)` |
|---|---|
| 16:04:30 | *no data* (demo not yet scraped) |
| **16:04:59** | **100** |
| **16:05:10** | **100** |
| 16:05:30 | *no data* (label removed, app restarted) |
| 16:09:00 | *no data* |

![count(demo_requests_total) in the Prometheus graph](screenshots/06-prometheus-cardinality.png)

*`count(demo_requests_total)` in Prometheus's own graph view, 16:03–16:07 UTC.
The line sits at exactly 100 from about 16:04:55 to 16:05:20 and does not exist
either side of it: nothing before the demo was first scraped, and nothing after
the restart without the label. No slow fade over the 5-minute lookback — the cut
is at a single scrape.*

Both results are true and they are not in conflict. The history is intact —
every one of those 100 series is still on disk and still queryable at any
timestamp where it was live, until the 15-day retention window drops it. What
changed is only what a query evaluated *now* can see.

The cutoff is also **sharper than I expected**. An instant query normally carries
the last sample forward for up to 5 minutes, so I predicted the series would
linger until ~16:10. It does not: it disappears between 16:05:10 and 16:05:30,
at the first scrape after the restart. When a target stops exporting a series
Prometheus writes an explicit **stale marker**, and that marker terminates the
series immediately rather than letting the lookback window carry it. The 5-minute
lookback applies when a target goes missing *unexpectedly*, not when it is
scraped successfully and simply no longer reports that series.

So the correct statement is narrower than the one I started with:

- **What disappears quickly:** the series from *current* dashboards and alerts.
- **What does not:** the stored samples, the index entries, and the disk they
  occupy — for the full retention period.
- **What never un-happens:** the memory pressure while those series were active.
  If a cardinality explosion kills a Prometheus server, removing the label brings
  the *next* one back up; it does not recover the data lost while it was down.

The practical lesson is unchanged and arguably stronger: you cannot verify a
cardinality fix by watching `count()` go down, because it goes down either way —
whether you fixed the label or merely stopped the app. Cardinality is a
code-review question, not an incident-response one.

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
./scripts/experiment_anomaly.sh              # Part E1 — writes observability/results/
./scripts/experiment_cardinality.sh          # Part E2, in-process
./scripts/experiment_cardinality.sh --stack  # Part E2, through Prometheus
```

Both scripts honour `RESULTS_DIR`, and on Windows `PYTHON` needs to be `python`
rather than the default `python3`. The results quoted in this report were
produced with:

```bash
PYTHON=python RESULTS_DIR=observability/results-live ./scripts/experiment_anomaly.sh
PYTHON=python RESULTS_DIR=observability/results-live ./scripts/experiment_cardinality.sh --stack
```

The `--stack` form of the cardinality experiment starts the `cardinality-demo`
container under the `experiment` compose profile and removes it again at the end,
so it leaves nothing running.

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

- **1038 automated tests pass** (18 skipped), including 63 covering metrics, JSON log
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

### The full stack, verified on 19 September 2026

An earlier version of this report said the containerised stack had never been
started, because the development environment blocked Docker registry pulls. **It
has now been run end to end on Docker Desktop for Windows (WSL2 backend)**, and
this section records what that changed. The original prediction — that the
version-sensitive parts, "the Kibana saved-object schema and the Filebeat ILM
setup", were the most likely to need fixing — turned out to be half right. Kibana
imported all nine saved objects unchanged on the first attempt. The log pipeline
needed three separate fixes, one of them in exactly the predicted place (the ILM
setup) and two that were not predicted at all.

Now observed running:

- **Prometheus scraping** the app and node-exporter. All targets `up`.
- **Grafana** rendering all three provisioned dashboards; both datasources report
  healthy.
- **Filebeat** tailing Docker logs, with `decode_json_fields` producing real
  queryable fields.
- **Elasticsearch** indexing, with the ILM policy attached to the write index.
- **Kibana** importing all nine saved objects (`"successCount":9`).
- **Part E1** re-run against the live stack, with server-side percentiles
  **queried from Prometheus** via `histogram_quantile` rather than replicated in
  Python.
- **Part E2** re-run through Prometheus with `count(demo_requests_total)` against
  a running server — which is what disproved this report's own earlier claim
  about what happens after a label is removed.
- **The full suite, 1038 tests**, passes inside the container image.
- **Grafana and Kibana screenshots** of the live stack are in `docs/screenshots/`
  and placed in Parts B, C and E. Every panel shown was checked to return data;
  two that read "No data" when the app was healthy were fixed (Part B).
- **The business dashboard has real data**: entries submitted through the review
  form, recorded by the app's own save path (Part B says exactly how).

#### What had to be fixed to get there

Five configuration defects, none of which any amount of file-level validation
would have caught, because every one of these files was syntactically valid:

| # | Symptom | Cause | Fix |
|---|---|---|---|
| 1 | `docker compose up` aborts: *path / is mounted on / but it is not a shared or slave mount* | The `rslave` mount propagation flag on node-exporter's `/:/host/root` mount is not supported on Docker Desktop's VM | Dropped the flag |
| 2 | Filebeat starts, finds the container, harvests **nothing** | Autodiscover condition written as `docker.container.labels.co_elastic_logs/enabled`. Filebeat's *event* fields de-dot label names, but the autodiscover *condition* matches the original dotted form | `docker.container.labels.co.elastic.logs/enabled` |
| 3 | `lifecycle policy gym-tracker-logs creation failed: unknown field [_comment]` | A JSON file cannot hold comments, and Elasticsearch rejects unknown top-level fields | Removed the `_comment` key; the explanation now lives in this report |
| 4 | `no matching index template found for data stream [filebeat-gym-tracker]` | Filebeat 8.x writes to a **data stream**, and the template name, the `index:` target and the ILM rollover alias must all agree. They did not | All three renamed to `filebeat-gym-tracker-logs` |
| 5 | Host dashboard's root-disk panel silently empty | `mountpoint="/"` does not exist on the Docker Desktop VM; the data disk is `/var/lib` | Matcher widened to `mountpoint=~"/\|/var/lib"` |

Plus one environment-specific change that is not a defect in the original
config: the node-exporter scrape target moved from `host.docker.internal:9100` to
the pinned bridge gateway `172.28.77.1:9100`, for the WSL2 reason explained in
Part B.

**What I take from this.** Every one of these files parsed. `docker compose
config` validated the compose file. The JSON was well-formed and the YAML was
well-formed. Four of the five defects were *semantic agreements between two
different systems* — a label spelling shared between Docker and Filebeat, three
names that Filebeat and Elasticsearch both have to agree on, a mountpoint that
exists on one kernel and not another. Validation checks a file against a grammar;
only running the thing checks it against reality. The single most misleading
failure was #2, because Filebeat reported no error at all: it started cleanly,
connected to Elasticsearch successfully, and quietly collected nothing. A
pipeline that is silently empty looks exactly like a pipeline with nothing to
report.

### Still not verified

- **ILM rollover and deletion have not been observed happening.** The policy is
  attached to the write index and reports `phase: hot`, but rollover needs 1GB or
  1 day and deletion needs 7 more. What is verified is that the policy exists, is
  valid, and is bound to the index — not that Elasticsearch has actually executed
  either transition.
- **Log rotation by the Docker json-file driver** (10MB × 3) has likewise not
  been reached in practice.
- **The app's LLM extraction path** was not exercised: this machine has no
  `GROQ_API_KEY`, so `gym_llm_extraction_duration_seconds`, the extraction
  counters and the weekly-report counter have no data, and the three panels
  under "The language model" on the business dashboard are empty. What *is*
  verified is that the app now behaves correctly without a key (Part A).
- **The tests do not run in the host Python** on this machine — `rapidfuzz` is
  absent from it. They pass in the container image, which is where the
  application actually runs. This is an environment gap, not a code defect.

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
