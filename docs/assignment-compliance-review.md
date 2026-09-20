# Assignment 1 — Observability: compliance review

A strict review of this repository against `Assignment-1 (1).pdf`, conducted 20 September 2026.

**Method.** Every claim below is traced to a file, a line, an executed command, or a screenshot
whose pixels were examined. Nothing is credited because a dependency, an image, a config file,
a metric name, a dashboard JSON, a README sentence or a screenshot *exists*.

**Execution limits.** The Docker daemon was not running on this machine
(`npipe:////./pipe/dockerDesktopLinuxEngine` unreachable), so the containerised stack
**could not be started or verified through execution in this session**. The host Python
lacks `rapidfuzz`, so the test suite **could not be executed** either — 12 of 13 test modules
fail at collection. What *was* executed is listed under "Independently reproduced".

---

## Status legend

| Status | Meaning |
|---|---|
| ✅ COMPLETE | Implemented, and proven by code, executed output, or examined screenshot |
| ⚠️ PARTIALLY COMPLETE | Present but with a stated gap |
| ❌ MISSING | Not present |
| 🔴 IMPLEMENTED BUT NOT DEMONSTRATED | Code exists; no evidence it ran |
| 🔍 CANNOT VERIFY | Could not be checked in this environment |

---

## Independently reproduced in this session

These are the only things I verified by running them, rather than by reading them.

| What | Command | Result |
|---|---|---|
| Part E2 growth curve | `python observability/cardinality_demo.py --drive {1,10,25,50,100}` | series = 1, 10, 25, 50, 100 — **exactly matches the report's table** |
| Part E2 safe mode | `--drive 100 --safe-only` | `demo_requests_total series=0 demo_requests_safe_total series=1` |
| Part E2 cap refusal | `--drive 10 --max-ids 500` | refused: *"--max-ids is capped at 100 by design"* |
| Shell script syntax | `bash -n scripts/*.sh` | all 4 pass |
| Every YAML/JSON config | parsed with Python | all valid |
| Dashboard structure | parsed all 3 JSON files | 41 panels, 52 queries, no duplicate ids |
| Kibana saved objects | parsed `saved-objects.ndjson` | 9 objects: 1 index-pattern + 8 searches |
| All 23 `file.py:line` references | cross-checked against source | 5 wrong on the first pass, **2 more found on a second pass**; all 7 now fixed and re-verified |
| All 12 secondary bare line numbers | cross-checked against source | 11 exact, 1 wrong (`DraftTracker`); now fixed |
| All 6 screenshots | opened and read | genuine live captures, mutually consistent |

---

## Part A — Working project and clear problem (10 marks)

| Requirement | Status | Evidence in Code | Location | Comments |
|---|---|---|---|---|
| A working project | ✅ COMPLETE | FastAPI app: `app.py` (67 KB), `pipeline.py`, `auth.py`, `sessions.py`, `schema.sql`, 13 test modules | repo root | Substantial real application, not a toy built for the assignment |
| Clearly stated problem | ✅ COMPLETE | Journal entries are unreadable by machines; structured apps are too tedious to sustain | `docs/REPORT.md:23–41` | Includes a concrete sample entry |
| Intended users | ✅ COMPLETE | One person's own log, multi-account; scoped by `user_id` from the session cookie | `docs/REPORT.md:43–49` | Explicitly states what it is *not* |
| Solution explanation | ✅ COMPLETE | Paste → LLM extract → score → **human review** → database | `docs/REPORT.md:51–70` | The "no unseen number reaches the database" principle is the load-bearing design claim |
| What works | ✅ COMPLETE | Capability table, 12 rows, separating pre-existing from assignment work | `docs/REPORT.md:72–111` | **Discloses a real gap**: the PWA workout app's sets do not increment `gym_sets_written_total` |
| Instructions for trying it | ✅ COMPLETE | `docker compose up -d --build`, `stack.sh status`, load generator | `docs/REPORT.md:113–127`, `README.md:161+`, `docs/REPORT.md:1355–1446` | Start, use, test and clean-up all covered |

**Part A verdict: 10/10 evidence present.** The disclosure of the workout-app metric gap is a
credit, not a debit — it is the kind of thing a marker looks for and rarely finds.

---

## Part B — Metrics and dashboards (30 marks)

| Requirement | Status | Evidence in Code | Location | Comments |
|---|---|---|---|---|
| Prometheus | ✅ COMPLETE | `prom/prometheus:v2.54.1`, 4 scrape jobs, 15s interval, 15d retention, `--web.enable-lifecycle` | `docker-compose.yml`, `observability/prometheus/prometheus.yml` | Screenshot 6 shows the Prometheus UI running and executing PromQL |
| Grafana | ✅ COMPLETE | `grafana/grafana:11.2.0`, datasources and dashboards provisioned from files | `docker-compose.yml`, `observability/grafana/provisioning/` | Screenshots 1–3 are live Grafana |
| Application metrics | ✅ COMPLETE | `gym_http_requests_total`, `_duration_seconds`, `_exceptions_total`, `_in_flight`, `gym_logins_total`, `gym_db_commit_duration_seconds` | `metrics.py`; call sites `app.py:377,416,419,422,447,851,874,889,1356` | Recorded in one middleware, so coverage cannot drift as routes are added |
| Business metrics | ✅ COMPLETE | 9 metrics: entries, sets, bodyweight, duplicates, LLM outcomes/duration, weekly reports, drafts open, entry bytes, sets/entry | `metrics.py`; `app.py:1264,1272,1332,1349,1374,1397,1400,1401,1402,1406,1509`, `pipeline.py:1050,1051,1187,1224` | Screenshot 2 shows real data: 18 sets, 5 entries |
| Counter | ✅ COMPLETE | 10 counters | `metrics.py` | Verified in exposition and on dashboards |
| Gauge | ✅ COMPLETE | 4 gauges | `metrics.py:269,277` + `DraftTracker` | `gym_http_requests_in_flight` visible on Screenshot 1 |
| Histogram | ✅ COMPLETE | 3 histograms with per-metric bucket layouts | `metrics.py` | Heatmap panel on Screenshot 1 renders the buckets directly |
| Summary | ✅ COMPLETE | 2 summaries | `metrics.py` | "Avg entry size 20 B" on Screenshot 2 |
| p95 / p99 | ✅ COMPLETE | `histogram_quantile(0.95\|0.99, sum by (le) (rate(..._bucket[5m])))` | `01-app-performance.json` | Screenshot 1 stat tiles read p95 503.958 ms, p99 700.792 ms |
| Time-window explanation | ✅ COMPLETE | Sliding 5m (10m for LLM); "must hold ≥4 scrapes"; "a percentile cannot be averaged" | `docs/REPORT.md:319–335` | Also printed on the dashboard itself in a text panel |
| Metric table (name/purpose/type/unit/labels/code location) | ✅ COMPLETE | 19 metrics in 4 typed tables, all six columns present | `docs/REPORT.md:155–226` | Three stale references (I1, I2) **fixed and re-verified** |
| Grafana queries | ✅ COMPLETE | 52 queries across 41 panels | `observability/grafana/dashboards/*.json`, transcribed at `docs/REPORT.md:227–318` | |
| Explanation of each chart | ✅ COMPLETE | Per-panel prose plus an on-dashboard "Reading this dashboard" text panel | `docs/REPORT.md:227–318` | |
| Important metrics, not class examples | ✅ COMPLETE | Duplicate decisions, LLM rate-limit outcomes, narration quality, drafts backlog | `metrics.py` | None of these appear in any tutorial for this stack |
| ≥1 metric explored myself | ✅ COMPLETE | `gym_sets_per_entry` — the ratio that catches silent extraction data loss | `docs/REPORT.md:301` | Explicitly labelled as such, with the reasoning |
| Node Exporter | ✅ COMPLETE | `prom/node-exporter:v1.8.2`, `network_mode: host`, `pid: host`, `/proc` `/sys` `/` mounted ro | `docker-compose.yml` | |
| CPU | ✅ COMPLETE | 4 panels | `03-host-node-exporter.json` | Screenshot 3: 2.98% busy, load 0.02, 12 cores, by-mode breakdown |
| Memory | ✅ COMPLETE | 2 panels | same | Screenshot 3: 38.15% of 7.56 GiB, total/used/available/cached |
| Disk | ✅ COMPLETE | 4 panels | same | Screenshot 3: 6.29% used, 944 GiB free, per-device I/O + saturation |
| Network | ✅ COMPLETE | 2 panels | same | Screenshot 3: per-device throughput to 46.6 kB/s, errors/drops flat at 0 |
| Prometheus collects node-exporter | ✅ COMPLETE | `job_name: node-exporter`, target `172.28.77.1:9100` | `prometheus.yml` | Data is rendering in Screenshot 3, which is the proof it is being scraped |
| Grafana visualisation | ✅ COMPLETE | Whole dashboard `uid: gym-host` | same | |
| Machine name | ✅ COMPLETE | `machine="docker-host"`, plus a text panel explaining it is the WSL2 VM | `prometheus.yml`, dashboard header | Visible in Screenshot 3 |

**Extra credit beyond the brief:** 9 alerting rules in `observability/prometheus/alerts.yml`,
each with a `for:` clause.

**Part B gaps:**
- ⚠️ Three of 23 code line references are stale (Important #1).
- ⚠️ Three business panels read "No data" for want of a `GROQ_API_KEY` — disclosed in the report.

---

## Part C — Logging pipeline (25 marks)

| Requirement | Status | Evidence in Code | Location | Comments |
|---|---|---|---|---|
| Application JSON logging | ✅ COMPLETE | `EcsJsonFormatter`, one JSON object per line, ECS field names | `logging_setup.py` | `json.dumps(..., default=str)` so a log call can never kill a request |
| Docker log handling | ✅ COMPLETE | `driver: json-file`, `max-size: 10m`, `max-file: 3` | `docker-compose.yml` | Rotation cap is explained as a self-protection measure |
| Filebeat | ✅ COMPLETE | `filebeat:8.15.0`, docker autodiscover on the `co.elastic.logs/enabled` label | `observability/filebeat/filebeat.yml` | Label-gated to avoid an ES→log→ES feedback loop |
| Elasticsearch | ✅ COMPLETE | `elasticsearch:8.15.0`, single-node, 512 MB heap, yellow-status healthcheck | `docker-compose.yml` | Healthcheck correctly waits for *yellow*, not green |
| Kibana | ✅ COMPLETE | `kibana:8.15.0` + 9 saved objects | `docker-compose.yml`, `observability/kibana/saved-objects.ndjson` | Screenshots 4 and 5 are live Kibana Discover |
| Correct pipeline | ✅ COMPLETE | App → stdout → json-file → Filebeat → ES → Kibana | across the above | Screenshot 4 is the whole path arriving intact |
| Parsing into searchable fields | ✅ COMPLETE | `decode_json_fields` with `target: ""`, `overwrite_keys`, `add_error_key`, `expand_keys` | `filebeat.yml` | **Screenshot 4 proves this**: 102 discovered fields with `k`/`t`/`#` type icons |
| Persistence / storage | ✅ COMPLETE | Three-lifetime table: file, Filebeat registry, ES document | `docs/REPORT.md:571–603` | |
| What survives restarts | ✅ COMPLETE | Registry on `filebeat-data`; documents on `elasticsearch-data` | same | Also names the real data-loss window (rotation outrunning a downed ES) |
| Retention / deletion | ⚠️ PARTIALLY COMPLETE | ILM: rollover at 1 GB/10M docs/1 day, delete 7 days after | `observability/elasticsearch/ilm-policy.json` | 🔴 **Neither transition has been observed executing** — the report says so plainly |
| Kibana search process | ✅ COMPLETE | Copy `x-request-id`, query `http.request.id: "..."` | `docs/REPORT.md:604–654` | 8 saved searches ship with the repo |
| Time field | ✅ COMPLETE | `@timestamp`, ISO-8601 UTC ms, app's value overwrites Filebeat's | `logging_setup.py`, `filebeat.yml` | Visible in Screenshots 4–5 |
| Service field | ✅ COMPLETE | `service.name: gym-tracker` | `logging_setup.py` | |
| Severity field | ✅ COMPLETE | `log.level` | `logging_setup.py` | Screenshot 5: 121 documents at `warning` |
| Message field | ✅ COMPLETE | `message`, a human sentence | `logging_setup.py` | |
| Request ID | ✅ COMPLETE | `http.request.id` from a **contextvar**, set once per request | `logging_setup.py`, `app.py:371` | Contextvar not thread-local, because async requests share threads |
| No secrets / personal data | ✅ COMPLETE | `SensitiveDataFilter` on the *handler*; 4 redaction paths; `raw_text` never logged | `logging_setup.py` | 7 tests cover it; no secrets found in `.env.example`; `.env` is gitignored |
| One original log shown | ✅ COMPLETE | Real line from the fault run | `docs/REPORT.md:655–668` | |
| Stored ES fields shown | ✅ COMPLETE | 12-row field/value/type table | `docs/REPORT.md:669–690` | Calls out `long` and `float` — the payoff of parsing |
| Working Kibana search | ✅ COMPLETE | `http.request.id: "8d59a31cdc6f454c"` | Screenshot 4 | Returns exactly 3 correlated documents for one `POST /save` |
| Explanation of parsing | ✅ COMPLETE | Why JSON-at-the-producer beats grok-at-the-consumer | `docs/REPORT.md:517–570` | |

**Part C gap:** ILM rollover/deletion is configured and bound but unobserved. This is honestly
disclosed and is not realistically fixable inside a marking window.

---

## Part D — System design and explanation (20 marks)

| Requirement | Status | Evidence in Code | Location | Comments |
|---|---|---|---|---|
| Architecture diagram | ✅ COMPLETE | Mermaid flowchart, 10 components, subgraphs for app / metrics / logs | `docs/architecture.mmd`, `docs/REPORT.md:708–758` | ASCII equivalent added to the .docx so it renders in Word |
| Shows the application | ✅ COMPLETE | `gym-tracker` FastAPI/uvicorn :8000 | same | |
| Shows dependencies | ✅ COMPLETE | Postgres 16, Groq (marked external) | same | |
| Shows monitoring tools | ✅ COMPLETE | Prometheus, node-exporter, Grafana, Docker driver, Filebeat, ES, Kibana | same | |
| Connections / arrows | ✅ COMPLETE | Dotted = pull, double = push; every edge labelled with its protocol | same | The pull/push distinction is the thing most diagrams get wrong |
| What each component does | ✅ COMPLETE | 10-row table: does / talks to / how | `docs/REPORT.md:759–770` | |
| How they communicate | ✅ COMPLETE | Same table — SQL/TCP, HTTPS, HTTP GET, bulk HTTP, unix socket | same | |
| Where data is stored | ✅ COMPLETE | 4-row table naming each volume | `docs/REPORT.md:772–780` | |
| Why this design | ✅ COMPLETE | The metrics-vs-logs cost-model argument, tied forward to Part E2 | same | This is the strongest paragraph in the report |
| If a component stops | ✅ COMPLETE | 9-row table: immediate effect / still works / recovery | `docs/REPORT.md:782–800` | Names `up == 0` as the only way the app's own death is reportable |
| Notes what is not understood | ✅ COMPLETE | 4 items: Lucene merges, TSDB memory, quantile edge cases, Filebeat back-pressure | `docs/REPORT.md:802–827` | The brief explicitly asks for this, and most submissions omit it |
| Metric trace: code → Prometheus → Grafana | ✅ COMPLETE | 5 steps on `gym_sets_written_total` | `docs/REPORT.md:829–890` | Cited `app.py:1278`; **fixed to `app.py:1400`** and re-verified (C1) |
| Metric trace: actual values | ✅ COMPLETE | Real scrape: 142 requests / 12.552 s / 121 faults | `observability/results-live/02-fault-metrics.txt` | I confirmed these values are in the file |
| Metric trace: queries | ✅ COMPLETE | `increase(...[24h])` and the sum/count mean | same | Explains *why* `increase()` and not the raw value |
| Log trace: code → Filebeat → ES → Kibana | ✅ COMPLETE | 6 steps on one `POST /save` | `docs/REPORT.md:892–950` | |
| Log trace: actual values | ✅ COMPLETE | Real ids `f1d4bb6745b64b3d`, `959d6bccd86742ab`, `8d59a31cdc6f454c` | report + Screenshot 4 | |
| Log trace: format changes | ✅ COMPLETE | flat JSON → Docker envelope → expanded nested document | same | Explicitly says "the format changes twice" |

---

## Part E1 — Reproduce a problem (10 marks)

| # | Requirement | Status | Evidence | Comments |
|---|---|---|---|---|
| 1 | Normal behaviour | ✅ COMPLETE | `01-baseline.json`: 599 req, 4.99 rps, p95 29.1 ms, 0 errors | |
| 2 | Repeatable test | ✅ COMPLETE | `scripts/load_generator.py --duration 120 --rps 5` | Closed-loop, fixed mix, deterministic |
| 3 | Prediction | ✅ COMPLETE | `observability/results-live/predictions.md` | **Written before the fault ran**, as a separate committed file — this is the right way to do it |
| 4 | Problem introduced | ✅ COMPLETE | `faults.py` + env vars; 500 ms on every 5th request | Config-only; no code edit, nothing left in history |
| 5 | Evidence of change | ✅ COMPLETE | Client p95 29.1→524.3 ms; server p95 4.8→687.1 ms; Screenshots 1 and 5 | |
| 6 | Cause | ✅ COMPLETE | `docs/REPORT.md:1117` + `fault.sequence` showing 5,10,15,20,25 | |
| 7 | Effect on users | ✅ COMPLETE | "a page load involves several requests, so most page loads feel slow" | The right framing |
| 8 | Problem removed | ✅ COMPLETE | `FAULT_INJECTION_ENABLED=false ... --force-recreate app` | |
| 9 | Test repeated | ✅ COMPLETE | `03-recovery.json`: 599 req, 4.99 rps, p95 31.8 ms | |
| 10 | Before/during/after comparison | ✅ COMPLETE | `results-live/summary.md` + `prometheus-percentiles.json` | Both client-side and server-side tables |
| 11 | Commands | ✅ COMPLETE | `scripts/experiment_anomaly.sh`, quoted in the report | |
| 12 | Time ranges | ✅ COMPLETE | Three explicit UTC windows, all 120 s | |
| 13 | Queries | ✅ COMPLETE | `histogram_quantile`, sum/count mean, `increase(gym_faults_injected_total[2m])` | |
| 14 | Results | ✅ COMPLETE | 4 result tables | |
| 15 | Several scrapes per stage | ✅ COMPLETE | 120 s ÷ 15 s = **8 scrapes per stage** | |
| 16 | Locally reproducible | ✅ COMPLETE | One script, no external dependency, no Groq key needed | |
| 17 | Safe undo | ✅ COMPLETE | 6 independent safety properties in `faults.py` | Refuses to arm in production; clamped; loud warning; counted |

**Two unplanned findings raise this above a competent experiment:**
1. The first run exposed a **real instrumentation bug** — the fault was applied before
   `time.perf_counter()`, so Prometheus reported a healthy service while every client waited
   500 ms. Found by comparing two independent measurements.
2. Server-side p95 (687.1 ms) and client-side p95 (524.3 ms) differ by 163 ms, correctly
   attributed to `histogram_quantile` interpolating across the 250 ms-wide `le=0.5`→`le=0.75`
   bucket — and **reproduced to within 1 ms across two runs on different infrastructure**.

---

## Part E2 — Cardinality explosion (5 marks)

| # | Requirement | Status | Evidence | Comments |
|---|---|---|---|---|
| 1 | `request_id` label on a local test counter | ✅ COMPLETE | `observability/cardinality_demo.py`, `UNSAFE` counter, own registry, own port | Paired with a `SAFE` counter for contrast |
| 2 | Stops at 100 unique IDs | ✅ COMPLETE | `MAX_UNIQUE_IDS = 100`; `--max-ids` above it is *refused* | **I executed this**: refusal message confirmed |
| 3 | Series growth demonstrated | ✅ COMPLETE | 1→1, 10→10, 25→25, 50→50, 100→100 | **I reproduced the entire table** |
| 4 | `count(demo_requests_total)` shown | ✅ COMPLETE | Screenshot 6 — Prometheus graph view, flat at exactly 100 | |
| 5 | Label removed | ✅ COMPLETE | `CARDINALITY_SAFE_ONLY=true` | **I executed this**: `series=0` |
| 6 | App restarted | ✅ COMPLETE | `--force-recreate cardinality-demo` | |
| 7 | Experiment repeated | ✅ COMPLETE | Same 100 requests, safe mode | |
| 8 | Results compared | ✅ COMPLETE | `results-live/cardinality-stack.md` + in-process table | |
| 9 | Cost at larger scale | ✅ COMPLETE | 10/100/1000 rps → 864 K / 8.64 M / 86.4 M series per day | Notes the failure is not graceful |
| 10 | Why IDs belong in logs | ✅ COMPLETE | 4-row cost-model comparison table | Enforced by `test_labels_are_bounded_dimensions_only` |
| 11 | No attempt to crash Prometheus | ✅ COMPLETE | Hard cap; refuses to exceed it | |
| 12 | Removing labels ≠ deleting history | ✅ COMPLETE | `docs/REPORT.md:1252–1305` | **Best section in the report** — see below |

**On requirement 12.** The report predicted `count()` would stay at 100 after the label was
removed, measured that it returns *no data*, mapped the boundary with timestamped queries
(16:04:59 → 100, 16:05:30 → none), and correctly explained the cause as an explicit **stale
marker** terminating the series rather than the 5-minute lookback carrying it. It then states
the narrower, correct conclusion: the *stored samples* persist for the full retention window
even though a *current* query cannot see them. This is a self-correction driven by measurement,
which is exactly what the brief is testing for.

---

## Missing / Incomplete Requirements

> **Resolution status, 20 September 2026.** Every code-reference defect below has been
> **fixed in `docs/REPORT.md`** and re-verified: all 23 `file.py:line` references and all 12
> secondary bare line numbers now resolve to a real metric or logging call site. A second
> verification pass — prompted by re-checking the two references my first pass had flagged but
> not scrutinised — found **two further wrong references in the Part C logging table** that the
> original review missed; those are recorded as C2 and are also fixed. The submission `.docx`
> was built from the verified line numbers and never contained any of these defects.
>
> The remaining open items (I3–I6) are evidence gaps, not code defects, and each needs a
> running stack or a Groq key to close.


### Critical — could cost marks

**C1. Wrong code line reference in the Part D metric trace.** — ✅ **RESOLVED**

- **What is missing:** `docs/REPORT.md:837` says
  `METRICS.sets_written_total.inc(result.inserted_sets)   # app.py:1278`.
  The real location is **`app.py:1400`**. Line 1278 is `"user.id": user.user_id,` inside an
  unrelated `entry_drafted` log call.
- **Why it matters:** Part D is 20 marks and its whole purpose is "trace one *actual* metric".
  A marker who clicks through to `app.py:1278` finds a logging statement, not a counter, and
  the step that is supposed to anchor the trace in real code fails at the first hop. It also
  undermines the adjacent (correct) references.
- **Where to fix:** `docs/REPORT.md`, line 837.
- **The change:**
  ```diff
  - METRICS.sets_written_total.inc(result.inserted_sets)   # app.py:1278
  + METRICS.sets_written_total.inc(result.inserted_sets)   # app.py:1400
  ```
- **How to test:** `sed -n '1400p' app.py` must print that exact line.
- **Status:** applied and verified. `app.py:1400` is
  `METRICS.sets_written_total.inc(result.inserted_sets)`.

**C2. Both Part C logging references were wrong.** — ✅ **RESOLVED**

*Found on a second pass, after the initial review. The first pass printed these two references
but only validated them against metric patterns, so it did not check what the report claimed
they were.*

- **What was missing:** the "What is logged, why, and where" table at `docs/REPORT.md:434–435`
  cited `app.py:429` for the `request served` line and `app.py:404` for `request failed`.
  Line 429 is `"url.path": request.url.path,` *inside the `request failed` extra dict*, and
  line 404 is `response = await call_next(request)` — neither is a logging call.
- **Why it matters:** Part C is 25 marks and the brief asks explicitly for "what you log, why,
  **and where in the code**". Both of the two rows that carried a precise location were wrong,
  including the one for `request served` — the line the report calls "the spine everything else
  hangs off".
- **The change:** `app.py:463` (the `logger.info("request served", ...)` call) and
  `app.py:425` (the `logger.exception("request failed", ...)` call).
- **Status:** applied and verified.

**C3. `DraftTracker` pointed at its instantiation, not its class.** — ✅ **RESOLVED**

- **What was missing:** `docs/REPORT.md:180` read "(`DraftTracker` at `453`)". Line 453 is
  `DRAFTS = DraftTracker(METRICS.review_drafts_open)`; the class is defined at **397**.
- **Why it matters:** minor, but the phrasing points a reader at the class and delivers them to
  a one-line assignment.
- **The change:** `DraftTracker` at `397`.
- **Status:** applied and verified.

### Important — implemented but under-evidenced

**I1. Two stale line references in the Part B gauge table.** — ✅ **RESOLVED**

- **What is missing:** `docs/REPORT.md:179` cites `pipeline.py:1175` / `1212` for
  `gym_llm_extractions_in_flight`. The real lines are **1187** (inc) and **1224** (dec).
  Line 1175 is `"llm.tpm_limit": GROQ_TPM_LIMIT,` inside a log `extra` dict.
- **Why it matters:** The brief explicitly requires "where and how it is recorded in the code".
  A reference that lands on an unrelated line fails that requirement for that metric.
- **Where to fix:** `docs/REPORT.md`, line 179.
- **The change:** `pipeline.py:1187` / `1224`.
- **How to test:** `sed -n '1187p;1224p' pipeline.py`.
- **Status:** applied and verified.

**I2. `pipeline.py:1042` points at a docstring, not a recording site.** — ✅ **RESOLVED**

- **What is missing:** Lines 169 and 198 of the report cite `pipeline.py:1042` for
  `gym_llm_extractions_total` and `gym_llm_extraction_duration_seconds`. Line 1042 is the
  docstring of `_record_extraction`; the actual `.inc()` and `.observe()` are at **1050** and
  **1051**.
- **Why it matters:** Defensible as a function-level pointer, but it is inconsistent with the
  exact-line style used for all 17 `app.py` references, and a marker checking one at random
  may land on this one.
- **Where to fix:** `docs/REPORT.md`, lines 169 and 198.
- **The change:** `pipeline.py:1050` (counter) and `pipeline.py:1051` (histogram).
- **Status:** applied and verified.

**I3. Three business panels have no data.**

- **What is missing:** `gym_llm_extractions_total`, `gym_llm_extraction_duration_seconds` and
  `gym_weekly_reports_total` are instrumented and unit-tested but have never recorded a
  production observation, because the capture machine has no `GROQ_API_KEY`. Screenshot 2
  shows "No data" on "Extraction outcomes" and "Weekly reports generated".
- **Why it matters:** Part B asks you to *show their data in Grafana*. Instrumentation without
  data is 🔴 IMPLEMENTED BUT NOT DEMONSTRATED for three of nine business metrics. The report
  discloses this, which substantially mitigates it — but a filled panel is worth more.
- **Where to fix:** No code change. Set a Groq key and exercise the path:
  ```bash
  export GROQ_API_KEY=gsk_...          # a free Groq key is sufficient
  docker compose up -d --force-recreate app
  # paste one entry through POST /log in the browser, then generate a weekly report
  ```
- **How to test:**
  ```bash
  curl -s localhost:8000/metrics | grep -E 'gym_llm_extractions_total|gym_weekly_reports_total'
  ```
  Both should show non-zero samples.
- **Evidence needed afterwards:** a re-capture of the business dashboard with the three LLM
  panels populated (Screenshot B-2 in the plan below).

**I4. The "1038 tests pass" claim cannot be verified on this machine.**

- **What is missing:** `rapidfuzz` is absent from the host Python, so 12 of 13 test modules
  fail at collection and only 65 tests are even collected. The claim of 1038 passing tests
  rests on an in-container run that I could not repeat.
- **Why it matters:** The figure appears twice in the report (Part A and the verification
  section) and is load-bearing for the "what is verified" claims.
- **Where to fix:** Nothing is broken — this is an environment gap the report already names.
- **How to test:**
  ```bash
  docker compose up -d
  docker compose exec app python -m pytest -q | tail -5
  ```
- **Evidence needed afterwards:** a terminal screenshot of the pytest summary line
  (Screenshot V-1 below). This converts a claim into evidence for very little effort.

**I5. ILM rollover and deletion are configured but never observed.**

- **What is missing:** The policy is attached and reports `phase: hot`, but neither the
  rollover (1 GB / 1 day) nor the delete (7 days later) has executed.
- **Why it matters:** Part C asks "when old logs are deleted". The *mechanism* is documented
  and correct; the *behaviour* is unwitnessed. This is realistically unfixable in a marking
  window and the report says so, which is the right call.
- **Optional demonstration:** temporarily set `max_age: "5m"` in `ilm-policy.json`, restart
  Filebeat, wait, then `GET _cat/indices/filebeat-gym-tracker-*`. Revert afterwards.

**I6. The PWA workout app's sets bypass the business counter.**

- **What is missing:** Sets logged through `/api/session/{session_id}/sets` do not increment
  `gym_sets_written_total`, because that counter lives only in the journal save path.
- **Why it matters:** It makes the headline business metric under-report the product's actual
  unit of value. The report already calls this "a real gap in coverage rather than a design
  choice", which is the honest framing and likely earns more credit than a silent fix would.
- **Where it would be fixed:** the set-insert handler in `sessions.py`, adding
  `METRICS.sets_written_total.inc(n)` alongside the existing insert.
- **Recommendation:** **leave it and keep the disclosure.** Changing instrumentation after the
  experiments were measured would invalidate the numbers already in the report, for a metric
  the brief does not require to be exhaustive.

### Minor — presentation only

- **M1.** The three dashboard JSON titles contain a UTF-8 en-dash that renders as `�` when read
  with a mis-detected encoding. Cosmetic; Grafana renders it correctly.
- **M2.** `Assignment-1 (1).pdf` and `docs/defending-this.md` are untracked in git. Add or
  ignore them deliberately before submitting, so the tree is clean.
- **M3.** The report's Mermaid diagram will not render in Word or a plain PDF export. The
  submission `.docx` carries an ASCII equivalent; consider also exporting a PNG of the Mermaid
  source via mermaid.live for the repo.

---

## Screenshot / evidence plan

Six screenshots already exist and are genuine. These are the **additional** captures that would
convert the remaining gaps into evidence, in priority order.

### Screenshot V-1 — Test suite passing (highest value, lowest effort)
- **Purpose:** Substantiates the "1038 tests pass, 63 covering observability" claim (Part A and
  the verification section).
- **What must be visible:** The command `docker compose exec app python -m pytest -q` and the
  final summary line showing the passed/skipped counts.
- **Where to capture:** Terminal.
- **Expected evidence:** `1038 passed, 18 skipped in NNs`.
- **Assignment requirement:** Part A ("explain what works"); supports every "verified" claim.

### Screenshot V-2 — Prometheus targets all up
- **Purpose:** Proves Prometheus is actually collecting from both the app and Node Exporter —
  currently inferred from Grafana rendering data, never shown directly.
- **What must be visible:** `http://localhost:9090/targets`, with `gym-tracker` and
  `node-exporter` both **UP**, their endpoints, and the "Last Scrape" ages.
- **Where to capture:** Browser (Prometheus UI).
- **Expected evidence:** Green UP badges on `app:8000/metrics` and `172.28.77.1:9100/metrics`.
- **Assignment requirement:** Part B — "Collect these with Prometheus".

### Screenshot V-3 — Business dashboard with LLM panels populated
- **Purpose:** Closes gap I3 — the only requirement currently sitting at 🔴.
- **What must be visible:** The "The language model" row of the business dashboard showing data
  in "Extraction outcomes", "Extraction latency percentiles" and "Weekly reports generated".
- **Where to capture:** Grafana, dashboard `gym-business`.
- **Expected evidence:** Non-empty series; at minimum `outcome="success"` on extractions.
- **Assignment requirement:** Part B — "show their data in Grafana".
- **Prerequisite:** a `GROQ_API_KEY` and one entry pasted through the review flow.

### Screenshot V-4 — `/metrics` raw exposition
- **Purpose:** Shows all four metric types in the *source* format, not just as rendered charts.
- **What must be visible:** `curl -s localhost:8000/metrics | grep -E '^# TYPE gym_'` output,
  with `counter`, `gauge`, `histogram` and `summary` all visible in one frame.
- **Where to capture:** Terminal.
- **Expected evidence:** Roughly 19 `# TYPE` lines spanning all four types.
- **Assignment requirement:** Part B — "Use all four metric types".

### Screenshot V-5 — Elasticsearch document as stored (`_source`)
- **Purpose:** Part C asks for "its stored fields". Screenshot 4 shows Kibana's *table* view;
  this shows the actual indexed JSON document.
- **What must be visible:** In Kibana Discover, expand one document and select the **JSON** tab,
  showing the nested `http.request.id`, `container.name`, and the numeric fields.
- **Where to capture:** Kibana Discover, expanded document.
- **Expected evidence:** The `_source` with dotted keys expanded into nested objects — the
  direct proof that `expand_keys: true` did its job.
- **Assignment requirement:** Part C — "Show one original log, its stored fields, and a working
  search."

### Screenshot V-6 — ILM policy attached to the write index
- **Purpose:** Partially closes I5. Cannot show deletion, but can show the policy is bound.
- **What must be visible:**
  `curl -s 'localhost:9200/filebeat-gym-tracker-*/_ilm/explain?pretty'` showing
  `"policy": "gym-tracker-logs"` and `"phase": "hot"`.
- **Where to capture:** Terminal.
- **Expected evidence:** The managed index with its policy name and current phase.
- **Assignment requirement:** Part C — "when old logs are deleted".

### Screenshot V-7 — Clean shutdown
- **Purpose:** The brief's submission requirement asks for instructions to "safely clean up".
- **What must be visible:** `./scripts/stack.sh down` followed by
  `docker compose ps -a` showing nothing running, and `docker volume ls | grep gym-tracker`
  showing the volumes **still present** (proving `down` preserves data).
- **Where to capture:** Terminal.
- **Expected evidence:** Empty container list, intact volume list.
- **Assignment requirement:** Submission — "instructions to start, use, test, and safely clean
  up your project".

**Not worth capturing:** a Grafana datasource health page (already implied by three rendering
dashboards), a `docker compose ps` of the running stack (implied by every other screenshot),
or the app's own UI (Part A is not marked on visual design).

---

## Final action plan

### Must fix
✅ **All done.** Seven line-reference corrections applied to `docs/REPORT.md` and re-verified:

| Report line | Was | Now | Defect |
|---|---|---|---|
| 169 | `pipeline.py:1042` | `pipeline.py:1050` | I2 |
| 179 | `pipeline.py:1175` / `1212` | `pipeline.py:1187` / `1224` | I1 |
| 180 | `DraftTracker` at `453` | `DraftTracker` at `397` | C3 |
| 198 | `pipeline.py:1042` | `pipeline.py:1051` | I2 |
| 434 | `app.py:429` | `app.py:463` | C2 |
| 435 | `app.py:404` | `app.py:425` | C2 |
| 837 | `app.py:1278` | `app.py:1400` | C1 |

Re-verification: all 23 `file.py:line` references and all 12 secondary bare line numbers now
resolve to a real metric or logging call site. The submission `.docx` was built from the
verified values and never carried any of these defects.

### Must test
4. `docker compose up -d --build` from a clean checkout, then `./scripts/stack.sh status` —
   confirm the stack still comes up after the September fixes.
5. `docker compose exec app python -m pytest -q` — confirm the 1038 figure.
6. `./scripts/experiment_cardinality.sh --stack` — confirm Part E2 still reproduces end to end.

### Must screenshot
7. V-1 (tests passing) — highest value per minute spent.
8. V-2 (Prometheus targets up).
9. V-4 (`/metrics` raw, all four `# TYPE` lines).
10. V-5 (Elasticsearch `_source` JSON).

### Must document
11. Nothing. Every requirement that is implemented is already explained, and the three gaps
    (ILM, LLM panels, PWA business coverage) are each disclosed in the report's own words.

### Optional improvements
12. Set a Groq key and re-capture the business dashboard (V-3).
13. Export the Mermaid diagram as a PNG for submission formats that do not render Mermaid.
14. Commit or ignore the two untracked files so the submitted tree is clean.

---

## Evaluator assessment

### Estimated requirement coverage

| Part | Marks | Coverage | Basis |
|---|---:|---:|---|
| A — Working project and clear problem | 10 | **100%** | All six sub-requirements evidenced; the coverage gap is disclosed rather than hidden |
| B — Metrics and dashboards | 30 | **98%** | All 23 sub-requirements present; stale code references now fixed. −2 for three business panels with no data |
| C — Logging pipeline | 25 | **96%** | All 21 sub-requirements present and screenshot-proven; the two wrong "where in the code" references now fixed (C2). −4 for ILM transitions being configured but unobserved |
| D — System design and explanation | 20 | **100%** | All 17 sub-requirements present; the wrong reference at the first hop of the metric trace is fixed |
| E1 — Anomaly experiment | 10 | **100%** | All 17 sub-requirements met, plus two unplanned findings that demonstrate genuine investigation |
| E2 — Cardinality experiment | 5 | **100%** | All 12 sub-requirements met; growth table independently reproduced; requirement 12 handled better than the brief asks |
| **Total** | **100** | **≈98%** | |

### Overall completion status: **98%** (was 96% before the reference fixes)

This is not an inflated figure. The seven stale code references that held the original
estimate down have been fixed and re-verified. What remains is held below 100% by three
evidence gaps, none of which is a code defect: three empty business panels (no Groq key on the
capture machine), one unobserved ILM transition, and one business metric that does not cover a
later feature. All three are explicitly disclosed in the report itself.

The brief says marks are awarded on "whether your setup works and whether you can explain it
using your results". Both halves are satisfied: the stack has been run end to end, the
experiments produced measured numbers rather than illustrative ones, and the explanations
are grounded in those numbers — including two places where the measurement **contradicted the
author's stated prediction** and the report was corrected rather than the data ignored.

### Highest-risk items

1. ~~**`app.py:1278` in the Part D metric trace**~~ — ✅ fixed.
2. **The 1038-test claim is unverified in any artifact.** One terminal screenshot fixes it.
   *This is now the single highest-risk open item.*
3. **Three business metrics at 🔴** — instrumented, never demonstrated.
4. **Prometheus target health is never shown directly** — inferred throughout, shown nowhere.
5. **ILM deletion unobserved** — correctly disclosed, but a marker may still deduct.
6. ~~**Two stale `pipeline.py` references**~~ and ~~**two wrong Part C logging references**~~ — ✅ all fixed.
7. **Mermaid will not render** in a Word or PDF submission without an exported image.
8. **Untracked files** (`Assignment-1 (1).pdf`, `docs/defending-this.md`) in the working tree.

### What a marker is most likely to reward

- The prediction file committed **before** the fault ran.
- The p95-vs-p50 argument made with measured numbers rather than asserted.
- The instrumentation bug the experiment itself exposed, and the lesson drawn from it.
- The E2 self-correction on stale markers, with the boundary mapped by timestamped queries.
- The "Things I do not fully understand yet" section, which the brief asks for and most
  submissions omit.
- The five configuration defects documented with symptom, cause and fix.
