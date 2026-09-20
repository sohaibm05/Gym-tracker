"""The cut plan for scripts/shorten_report_docx.py.

Keys are block numbers in the long version of docs/Assignment1-Observability.docx.
D deletes, P rewrites a paragraph, CELL/ROWS/COL edit a table.

What this plan does, and why:
  * Deletes the bordered "callout" boxes. Where a callout carried an explanation
    the assignment brief actually asks for, one tight sentence survives in the
    body text or in a much shorter box.
  * Deletes the Final Verification Checklist. 113 tick-box rows that restate
    Parts A to E line by line; it was a submission worksheet, not report content.
  * Cuts the transcribed Grafana queries to the ones each dashboard is judged on.
  * Rewrites every prose paragraph shorter.
  * Drops the "Why it matters here" column from B.3 and B.4, which repeats the
    Purpose column of the B.2 inventory.
  * Leaves untouched: the 7 screenshots, the ASCII architecture and pipeline
    diagrams, every measured number, every table of results, the Verification
    Status section, and the 19 "My Comments" boxes (one blank line each trimmed).
"""

from shorten_report_docx import CELL, COL, D, P, ROWS

PLAN: dict = {}

# --------------------------------------------------------------------------- #
# Front matter
# --------------------------------------------------------------------------- #
PLAN[6] = P(
    "**Submission contents.** The repository, including `observability/` — Prometheus, "
    "3 Grafana dashboards, Filebeat, the Elasticsearch ILM policy and 9 Kibana saved "
    "objects. `README.md` covers start / use / test / clean-up. This document is the "
    "short report, Parts A to E."
)
PLAN[7] = D
PLAN[8] = D
PLAN[11] = P('Right-click the contents field and choose "Update Field" in Word to fill in page numbers.')

# --------------------------------------------------------------------------- #
# Part A — Project
# --------------------------------------------------------------------------- #
PLAN[15] = P("Almost nobody keeps a training journal in a form a computer can read. A real entry looks like this:")
PLAN[21] = P(
    "Enough for whoever wrote it. Useless to everything else. Answering \"is my bench "
    "actually going up?\" means reading back through months of it by hand. The structured "
    "alternatives make you tap through exercise, sets, reps and weight for every single "
    "set — accurate, and tedious enough that people quit mid-session."
)
PLAN[22] = D
PLAN[24] = P(
    "One person's own training log, with accounts so several people can share one "
    "deployment. Every query is scoped by `user_id`, and ownership comes from the session "
    "cookie, never from anything the browser posted."
)
PLAN[27] = P(
    "A FastAPI web app. Paste the entry exactly as written, a language model pulls out the "
    "rows, you check every row before anything is saved, and the saved data drives progress "
    "charts and a weekly report."
)
PLAN[29] = P(
    "The pipeline scores how well each proposed row is grounded in the source text, and the "
    "review screen marks anything missing or implausible in red. The weekly report works the "
    "same way: every figure is computed in code, and the model only writes prose around "
    "numbers it did not calculate."
)
PLAN[35] = P("The right-hand column separates what pre-dates this assignment from the observability layer added for it.")
PLAN[37] = P(
    "**Known coverage gap.** The live set-by-set workout app came after this assignment. Its "
    "routes go through the same middleware, so application metrics and request logs cover it. "
    "The business metrics do not — `gym_sets_written_total` is incremented in the journal save "
    "path only. A real gap, not a design choice."
)
PLAN[38] = P(
    "**Without a Groq API key.** Only pasting an entry needs `GROQ_API_KEY`. `POST /log` now "
    "explains itself and logs a warning (`event.action: extraction_unavailable`) instead of the "
    "unhandled 500 it returned until 20 September — a `MissingApiKeyError` subclassing "
    "`ExtractionError`, so callers that already handled a failed extraction handle this too."
)
PLAN[41] = P("Docker and Docker Compose, and roughly 4 GB of free RAM. No Groq key is needed for any of the observability work.")
PLAN[43] = P("One command brings up all eight services — app, Postgres, Prometheus, Node Exporter, Grafana, Elasticsearch, Kibana, Filebeat:")
PLAN[60] = P(
    "**Note:** the suite runs inside the container image, which is where the application runs. "
    "On a host Python without `rapidfuzz`, collection fails before any test executes."
)
PLAN[67] = D
PLAN[71] = D

# --------------------------------------------------------------------------- #
# Part B — Metrics
# --------------------------------------------------------------------------- #
PLAN[74] = P("**Prometheus pulls.** Nothing in this stack pushes a metric. Each target exposes `/metrics`, and Prometheus fetches it on a timer.")
PLAN[84] = P(
    "**Why pull rather than push:** a target going down is itself visible — the synthetic `up` "
    "series drops to 0. A push-based system just stops receiving, and cannot tell silence from health."
)
PLAN[85] = [
    CELL(1, 2, "The resolution of every chart downstream. A spike shorter than one scrape is not in the data at all."),
    CELL(2, 2, "Below the interval, so a slow target cannot overlap its own next scrape."),
    CELL(4, 2, "A recent-history store, deliberately not an archive."),
    CELL(5, 2, "Attached to every series, so these metrics stay distinguishable if they are ever federated."),
]
PLAN[88] = P(
    '19 metrics, declared in one place — `metrics.py` — so this table has a single source of truth '
    'to be checked against. Call sites import a name; they never build a metric inline. Unit "1" is a '
    'dimensionless count.'
)
PLAN[90] = [
    CELL(1, 1, "Every response served. Numerator and denominator of the error rate."),
    CELL(2, 1, "Requests that raised out of the handler. Separates a crash from a deliberate 500."),
    CELL(9, 1, "Authentication outcomes. Failures without successes is the shape of credential stuffing."),
    CELL(10, 1, "Part E only. Proves from metrics alone when a fault was live."),
]
PLAN[95] = P(
    "Bucket boundaries are per metric, because the three live on different scales: HTTP latency keeps "
    "resolution from 5 ms to 10 s, the LLM from 0.25 s to 60 s, the database from 5 ms to 5 s. Buckets "
    "are the resolution limit of every percentile taken from them — Part E shows exactly that in the numbers."
)
PLAN[98] = P(
    "**On Python summaries.** `prometheus_client` implements no streaming quantiles, so a Summary exposes "
    "only `_sum` and `_count`. The average is the only question it can answer, which is why every p95 here "
    "comes from a Histogram. Asserted in `test_summary_exposes_sum_and_count_but_no_quantiles`, not merely stated."
)
PLAN[99] = D
PLAN[103] = D
PLAN[105] = P("These measure the machinery — fast, up, correct. The application dashboard is laid out on the RED method: Rate, Errors, Duration.")
PLAN[106] = COL(2)
PLAN[108] = P(
    "These measure what the product is actually for. Every one of them can move while the application "
    "metrics stay perfectly healthy, which is exactly why they are kept separate."
)
PLAN[109] = COL(2)
PLAN[111] = P(
    "`gym_review_drafts_open` rises when an entry is parsed at `POST /log` and falls when it is saved at "
    "`POST /save`. The interesting case is somebody parsing an entry and then closing the tab."
)
PLAN[112] = P(
    "**A gauge that can only rise is worse than no gauge at all.** A naive counter drifts upward until the "
    "process restarts, and a dashboard reads that as a real, growing backlog. So an open draft carries a "
    "timestamp, ages out after 30 minutes, and the whole map is capped (`DraftTracker` in `metrics.py`). "
    "Neither the user id nor the session date becomes a label — they are dict keys, and the gauge publishes "
    "only the count."
)
PLAN[114] = P(
    "`gym_sets_per_entry` — a Summary, in no tutorial for this stack, added because the obvious metrics "
    "cannot answer this. If extraction silently starts dropping rows, entries saved and sets written both "
    "still look healthy: the same people save the same number of entries. The ratio is what moves."
)
PLAN[118] = D
PLAN[120] = P(
    "Every percentile here is over a sliding 5-minute window. Ten minutes for the LLM, which is rarer and "
    "would otherwise have too few samples to interpolate from."
)
PLAN[129] = P(
    '"95% of requests completed in this time or less, over the last 5 minutes." Part E1 is the argument for '
    'using it: during the fault, server-side p95 rose from 4.8 ms to 687.1 ms while p50 moved only from '
    '2.5 ms to 3.1 ms. A dashboard showing the median alone would have called the service healthy while one '
    'request in five took half a second.'
)
PLAN[130] = P(
    "**Two constraints, both easy to get wrong.** The window has to hold at least four scrapes, so at a "
    "15-second interval nothing below `[1m]` is trustworthy. And a percentile cannot be averaged: summing "
    "the buckets first and taking the quantile of the sum, as above, is correct — `avg(p95)` across "
    "instances is not the p95 of the whole."
)
PLAN[131] = D
PLAN[133] = P(
    "Three dashboards, 41 panels, 52 queries, all provisioned from version-controlled JSON in "
    "`observability/grafana/dashboards/` rather than clicked together in the UI. A dashboard that only "
    "exists in Grafana's database dies with the volume and cannot be reviewed in a diff."
)
PLAN[136] = P(
    'Screenshot 1 — Grafana "Application performance", captured live across the Part E1 window. Proves '
    'Part B (Prometheus and Grafana, application metrics, Counter / Gauge / Histogram in one view, p95 and '
    'p99) and Part E1 (the fault is visible).'
)
PLAN[137] = P(
    "**What the chart shows.** The overview row reads left to right as RED: rate, errors, duration. The "
    "latency panels show the fault plainly — flat until 21:00, then p95 and p99 step to ~504 ms and ~701 ms "
    "and hold, while p50 stays at 2.6 ms. The heatmap makes the bimodality literal: two bands, one at "
    "5–10 ms and one at 500–750 ms, nothing in between."
)
PLAN[144] = D
PLAN[145] = D
PLAN[146] = D
PLAN[147] = D
PLAN[149] = P(
    "**Interpretation.** Error rate stayed at 0.00% for the whole window — every request returned 200. "
    "Monitoring that watched only errors and uptime would have seen nothing at all. The `clamp_min` in the "
    "error-rate query keeps a no-traffic window from rendering as `NaN`."
)
PLAN[153] = D
PLAN[156] = P(
    'Screenshot 2 — Grafana "Business metrics", with real data written through the review form. Proves '
    'Part B business metrics, and Counter / Gauge / Summary / Histogram all on one page.'
)
PLAN[157] = P(
    '**What the chart shows.** 18 sets logged and 5 entries saved in the last 24 hours, 0 drafts awaiting '
    'review, average entry size 20 B. "Entry outcomes" puts all five entries on `outcome="saved"`, with zero '
    'blocked and zero duplicate_held. Average sets per saved entry reaches 3.60; database commit p95 is '
    '85 ms against a p50 of 17.5 ms.'
)
PLAN[163] = D
PLAN[164] = D
PLAN[166] = P(
    '**Three panels read "No data", and that is disclosed rather than hidden.** The capture machine has no '
    '`GROQ_API_KEY`, so the extraction path never ran: "Extraction outcomes", "Extraction latency '
    'percentiles" and "Weekly reports generated" have no series. The instrumentation exists and is '
    'unit-tested; it has no data behind it here.'
)
PLAN[170] = D
PLAN[175] = P(
    "**Why not `host.docker.internal`?** On Docker Desktop it resolves to Windows, not to the Linux VM the "
    "exporter runs in. The bridge gateway `172.28.77.1` is the VM itself, so the subnet is pinned in "
    "`docker-compose.yml` and Prometheus scrapes that address. Every container here is a process on that "
    "one kernel, so their CPU and memory use is in these totals."
)
PLAN[177] = P(
    'Screenshot 3 — Grafana "Host (node-exporter)". Proves the Part B Node Exporter requirement: CPU, '
    'memory, disk and network, collected by Prometheus, drawn in Grafana, with the machine named in the header.'
)
PLAN[179] = P(
    'CPU busy 2.98%, load (1m) 0.02, 12 cores, uptime 8.73 minutes. "CPU time by mode" splits the total into '
    'idle (71.18% mean, 97.02% last), iowait and irq — the shape that tells a CPU-bound process from an '
    'I/O-bound one.'
)
PLAN[181] = D
PLAN[183] = D
PLAN[185] = P(
    '38.15% used of 7.56 GiB. The series plots total, used and cached separately, because "used" on Linux '
    'means little without knowing how much is reclaimable cache.'
)
PLAN[187] = D
PLAN[188] = D
PLAN[189] = D
PLAN[190] = D
PLAN[192] = P(
    "Root filesystem 6.29% used, 944 GiB available on /mnt/docker-desktop-disk. Throughput and I/O time are "
    "plotted per device (sda, sdb, sdc). I/O time is the saturation signal, and it is not throughput."
)
PLAN[195] = D
PLAN[196] = D
PLAN[197] = D
PLAN[199] = P(
    'The mountpoint matcher is `=~"/|/var/lib"` rather than a plain `"/"` because `mountpoint="/"` does not '
    'exist on the Docker Desktop VM — the data disk is at /var/lib. This panel was silently empty until '
    'running it found that.'
)
PLAN[201] = P(
    "Throughput per interface peaks at 46.6 kB/s on the bridge carrying the stack's traffic; docker0 averages "
    "555 B/s. Errors and drops are flat at zero on every interface for the whole window."
)
PLAN[203] = D
PLAN[205] = D
PLAN[210] = D
PLAN[212] = P(
    "Nine rules in `observability/prometheus/alerts.yml`. No Alertmanager is wired in, so they surface at "
    "Prometheus `/alerts` and as Grafana annotations. They are here because writing the alert is what forces "
    'a number onto the word "slow".'
)
PLAN[214] = P(
    "Every rule uses `for:`, so a condition has to persist before it fires. Without it, one slow scrape "
    "raises an alert, people learn the alerts are noise, and the next real one gets ignored."
)
PLAN[218] = D

# --------------------------------------------------------------------------- #
# Part C — Logs
# --------------------------------------------------------------------------- #
PLAN[228] = [
    CELL(2, 1, "The json-file driver wraps each line in its own envelope and rotates the file at 10 MB × 3."),
    CELL(3, 1, "Autodiscover watches the Docker API and starts a harvester for any container carrying the label."),
    CELL(4, 1, "`decode_json_fields` parses the app's JSON string and promotes its keys to the root of the event."),
    CELL(5, 1, "Bulk-indexed into filebeat-gym-tracker-logs, with the ILM policy bound to the write index."),
    CELL(6, 1, "Data view gym-tracker-logs over filebeat-gym-tracker-*, plus 8 saved searches."),
]
PLAN[229] = P(
    "**Step 4 is the one that turns text into data.** Without `decode_json_fields`, a Kibana search for a "
    "request id is a full-text match against a blob. With it, a term query against an indexed keyword field."
)
PLAN[231] = P(
    "Elastic Common Schema (ECS) field names, so Kibana's built-in columns and the `http.*`, `url.*` and "
    "`event.*` field groups work with no custom mappings."
)
PLAN[235] = P(
    "**The filter is a backstop, not the policy.** The policy is that call sites log ids and counts, never "
    "content. `SensitiveDataFilter` sits on the handler rather than a logger, so library output is covered "
    "too — a dependency that helpfully logs its own request with the Authorization header attached is exactly "
    "the case this catches."
)
PLAN[240] = P(
    "One JSON object per line matters: Filebeat reads newline-delimited records, so a pretty-printed object "
    "arrives as N unparseable lines. `ensure_ascii=False` keeps non-ASCII readable in Kibana instead of escaped."
)
PLAN[248] = D
PLAN[249] = D
PLAN[252] = D
PLAN[256] = D
PLAN[262] = D
PLAN[264] = P(
    "`default=str`, so a date, a Decimal or a model object degrades to its string form instead of killing the "
    "log call it appeared in. **A logging failure must never take a request down with it.**"
)
PLAN[266] = P(
    "`request_id_var` is a contextvar, set once per request by the middleware. Every line emitted while "
    "handling that request picks it up automatically, from any module at any depth, so one Kibana search on "
    "`http.request.id` returns the whole story of a single click."
)
PLAN[267] = P(
    "**Why a contextvar and not a thread-local.** The app is async: several requests share a thread, and a "
    "thread-local would cross their ids over."
)
PLAN[273] = D
PLAN[276] = P(
    "Autodiscover rather than a static path. Containers get new ids every time they are recreated, so a "
    "hardcoded path breaks on the first `docker compose up --build`."
)
PLAN[290] = P(
    "Only containers carrying that label are collected. Elasticsearch and Kibana are chatty, and indexing "
    "their logs into the same cluster is a feedback loop: an error produces a log line, which is indexed, "
    "which can produce another error. Filebeat's own container is deliberately unlabelled too."
)
PLAN[309] = P(
    "The app emits JSON rather than plain text, which moves the parsing to the producer — the only place that "
    "still knows the types. It used to log `event=request method=POST path=/save status=200`. Readable in a "
    "terminal, miserable downstream: every consumer re-parses it with a grok pattern, and one unescaped space "
    "inside a value breaks that pattern silently."
)
PLAN[310] = P(
    "**The payoff is typed fields.** Because status code and duration arrive as numbers, Kibana can answer "
    '`url.path: "/save" and http.response.status_code >= 500 and event.duration_ms > 1000` — a question you '
    "cannot ask of a plain-text log without a grok pattern somebody has to maintain."
)
PLAN[319] = P(
    "Filebeat 8.x writes to a data stream named `filebeat-<version>` by default. Without the explicit index, "
    "events go there and bypass both the template and the ILM policy. The index name, the template name and "
    "the rollover alias all have to agree — a defect found only by running the stack."
)
PLAN[326] = D
PLAN[327] = D
PLAN[339] = P("Table C.5 — The stored document. The two bold rows are the point: numbers arrive as numbers, so range queries work.")
PLAN[343] = D
PLAN[345] = P(
    "Data view gym-tracker-logs over filebeat-gym-tracker-*, plus eight saved searches shipped in "
    "`observability/kibana/saved-objects.ndjson` and imported by `scripts/setup_kibana.sh`."
)
PLAN[351] = P(
    "Screenshot 4 — Kibana Discover tracing one `POST /save` by request id. Proves Part C (a working Kibana "
    "search, parsed searchable fields, time / service / severity / message / request id) and Part D.3."
)
PLAN[352] = P(
    "**What this proves, field by field.** Three documents share the id 8d59a31cdc6f454c and the same "
    "millisecond (01:26:02.896): the business event (`event.action: entry_saved`), the middleware's own line "
    "(`url.path: /save`, status 200, `event.duration_ms` 9.4), and uvicorn's access line. All three in the "
    "same JSON shape, because uvicorn's handlers were cleared and its records propagate to the root handler."
)
PLAN[353] = P(
    "The left rail shows 102 fields discovered from the index, with type icons: `k` for keyword on "
    "`log.level`, `event.action` and `url.path`, `t` for text on `message`, `#` for number on "
    "`http.response.status_code` and `event.duration_ms`. Those icons are the visual evidence that "
    "`decode_json_fields` worked — an unparsed pipeline would show one text blob and nothing else."
)
PLAN[357] = D
PLAN[363] = P(
    "Writes always go to the newest index, so deleting an old one never interrupts ingestion. Dropping a "
    "whole index is also far cheaper than deleting documents, which in Lucene only marks them until a merge."
)
PLAN[364] = P(
    "**The real data-loss window in this design.** Docker deletes the oldest log file whether or not Filebeat "
    "has read it. If Elasticsearch is down long enough for a rotation to get ahead of Filebeat, those lines "
    "are gone. Hence the persistent registry, and a cluster that stays up."
)
PLAN[365] = P(
    "Seven days suits a course project. Production retention is a legal and financial question, not a "
    "technical one: long enough to investigate an incident nobody noticed for a week, short enough that the "
    "storage bill and the personal-data exposure stay bounded."
)
PLAN[366] = P(
    "**Not verified by observation.** The policy is attached to the write index and reports `phase: hot`. "
    "Rollover needs 1 GB or 1 day and deletion 7 more, so neither transition has been watched happening. "
    "Docker's 10 MB × 3 rotation has not been reached either."
)
PLAN[370] = D

# --------------------------------------------------------------------------- #
# Part D — System Design
# --------------------------------------------------------------------------- #
PLAN[415] = P(
    "Figure D.1 — Full architecture. Solid arrows are push; the Prometheus arrows are pull. [STORE] marks a "
    "persistent volume. Mermaid source at `docs/architecture.mmd`."
)
PLAN[417] = [
    CELL(1, 1, "Serves the app, exposes `/metrics`, writes JSON to stdout"),
    CELL(4, 1, "Scrapes `/metrics` every 15s, stores in its TSDB, evaluates alert rules"),
    CELL(5, 1, "Reads /proc and /sys, exposes host metrics"),
    CELL(6, 1, "Queries Prometheus and Elasticsearch, draws dashboards"),
]
PLAN[419] = [
    CELL(1, 2, "Relational, and needs transactions, foreign keys and exact answers."),
    CELL(2, 2, "Numeric time series compress enormously as delta-of-delta."),
    CELL(3, 2, "An inverted index is what makes arbitrary field search fast."),
    CELL(4, 2, "Reviewable in a diff, recreatable on any machine, not lost with a volume."),
]
PLAN[420] = P(
    '**The load-bearing design decision.** Metrics answer "how many, how fast, right now" cheaply for '
    'unlimited events, but only across a few bounded dimensions. Logs answer "what exactly happened to this '
    'one request" at a cost per event. Using one for the other\'s job is how both bills get large: a request '
    'id in a metric label is a cardinality explosion (Part E2), and counting log documents to get a request '
    'rate is expensive and imprecise.'
)
PLAN[423] = P(
    "**Observability failures degrade observability, not the application.** The app does not know whether "
    "Prometheus is scraping it, and writes to stdout whether or not anything reads. That is why `/metrics` is "
    "a plain endpoint and logging is a stdout write — the two cheapest, least coupled things they could be."
)
PLAN[429] = D
PLAN[441] = P("Every 15 seconds Prometheus fetches that endpoint and appends a sample, attaching the labels from the scrape config:")
PLAN[443] = P('                       service="gym-tracker", environment="development"}')
PLAN[444] = D
PLAN[445] = D
PLAN[452] = P(
    "**Why `increase()` and not the raw value.** A counter resets to 0 when the process restarts. `increase()` "
    "and `rate()` detect the reset and account for it; the raw value would show the total dropping to zero on "
    "every deploy."
)
PLAN[461] = P(
    "142 requests to /healthz taking 12.55 seconds in total — a mean of **88.4 ms**, from a route whose real "
    "cost is under 3 ms, because 25 of those 142 were held half a second each. The same arithmetic in PromQL, "
    "which is what the panel runs:"
)
PLAN[467] = D
PLAN[478] = P(
    "The formatter adds `@timestamp`, `service.name`, `log.level` and — from the contextvar — "
    "`http.request.id`, **without the call site mentioning it**."
)
PLAN[490] = P(
    "The container input strips the envelope, `decode_json_fields` promotes the app's keys to the root, and "
    "`add_docker_metadata` attaches `container.name`."
)
PLAN[491] = P(
    "**The format changes twice on this journey.** Flat JSON, then wrapped in Docker's envelope, then "
    'unwrapped and expanded into a nested document — `http.request.id` becoming `{"http": {"request": '
    '{"id": ...}}}` via `expand_keys`.'
)
PLAN[500] = P(
    "**The two paths meeting.** The metric says 12 sets were written in the last hour. The log says which "
    "entries, by whom, at what time, in which request. Neither answers the other's question, and "
    "`http.request.id` is what lets you walk from a spike on a chart to the clicks that caused it."
)
PLAN[504] = D

# --------------------------------------------------------------------------- #
# Part E — Experiments
# --------------------------------------------------------------------------- #
PLAN[507] = P(
    "Injected latency: a 500 ms delay on every 5th request, so 20% of traffic. Three stages of two minutes, "
    "which at a 15-second scrape interval is eight scrapes each."
)
PLAN[513] = P(
    "The load generator is deterministic and closed-loop — a fixed request mix at a fixed target rate, waiting "
    "for each response. The fault sampling is deterministic too, every 5th request rather than random, so a "
    "run injects a known number of faults and the numbers are reproducible rather than merely probable."
)
PLAN[519] = P("Screenshot 5 — the baseline is the flat left-hand portion, before 21:00. Proves Part E1 step 1, normal behaviour on a repeatable test.")
PLAN[522] = D
PLAN[524] = P(
    "Recorded in `observability/results-live/predictions.md` before the fault was armed. Writing the "
    "prediction down first is the point: an explanation that only arrives after the data is not an explanation."
)
PLAN[526] = P(
    "**Logs predicted to change:** `event.duration_ms` above 500 on the delayed requests only, one warning "
    "line per delayed request carrying `fault.sequence` and `fault.latency_ms`, and no error lines, because "
    "this fault degrades rather than breaks."
)
PLAN[528] = P(
    "The fault is configuration, not a code edit. Nothing in the request path changes. The only difference "
    "between a healthy run and a degraded one is an environment variable, and reverting means unsetting it "
    "and restarting."
)
PLAN[533] = [
    CELL(1, 1, "`FAULT_INJECTION_ENABLED` must be true. Two variables have to agree before any request is delayed"),
    CELL(2, 1, "Refuses to arm when `APP_ENV` is production, whatever else is set. A copied .env cannot degrade a real deployment"),
    CELL(3, 1, "Logs one loud warning at startup, naming exactly what is armed"),
    CELL(4, 1, "Every injected fault increments `gym_faults_injected_total`, so the experiment does not rest on trusting that a variable was set"),
    CELL(5, 1, "The delay is clamped to `MAX_LATENCY_MS` = 10,000, so a mistyped 5000-for-500 cannot pin a worker for a minute"),
    CELL(6, 1, "`asyncio.sleep`, never `time.sleep`. On one async worker, `time.sleep` would block every concurrent request and make the experiment measure queueing instead of latency"),
]
PLAN[541] = D
PLAN[542] = D
PLAN[543] = D
PLAN[547] = P(
    'Screenshot 6 — Kibana Discover, query `fault.kind: "latency"`, 20:59:00–21:01:30 local. 121 documents, '
    "all at warning level, each naming the delayed route. Proves Part E1 step 3 and Part C."
)
PLAN[549] = P(
    "**The metric and the log agree to within one event.** `gym_faults_injected_total` reports 121 while the "
    "stage window holds 120. The extra one is the first request the newly recreated process served, which "
    "drew the first delay at 15:59:10.386 and really was held 500 ms. I first assumed it came from the "
    "script's readiness probe, and wrote that down. Following the request id showed otherwise — a small case, "
    "and exactly the pattern this assignment is about."
)
PLAN[553] = P(
    "One request in five takes half a second longer. But a page load involves several requests, so most page "
    "loads feel slow rather than one in five — which is why p95 predicts how an app feels far better than an "
    "average does."
)
PLAN[554] = P(
    "**The mean is the worst of both.** 156.1 ms describes no actual request. None took 156 ms; they took "
    "either ~6 ms or ~520 ms. An average over a bimodal distribution is a number with no referent, and the "
    "heatmap in Screenshot 1 shows the two bands with nothing between them."
)
PLAN[557] = P(
    "Configuration is read once at construction, not per request, so settings cannot drift mid-experiment and "
    'one run is described by one set of values. Changing a fault means restarting the process — which is also '
    'what makes "remove the problem and repeat the test" an honest step rather than a mutation of live state.'
)
PLAN[563] = P(
    "**Recovery is verified, not assumed.** Server-side p95 is 4.76 ms in both the baseline and the recovery "
    'stages, identical to three significant figures, and /healthz returns `{"status": "ok"}`.'
)
PLAN[567] = P(
    "**1. The instrumentation was wrong, and the experiment caught it.** The first run produced a "
    "contradiction: the client measured a p95 of 506 ms while the server-side histogram put every request in "
    "the fastest bucket. The cause was ordering in my own middleware — the fault was applied *before* "
    "`started = time.perf_counter()`, so the delay fell outside the timed region. Prometheus was reporting a "
    "perfectly healthy service while every client waited half a second."
)
PLAN[568] = P(
    "**The general lesson.** Instrumentation that does not span the whole path is worse than none, because it "
    "produces confident wrong answers. Comparing two independent measurements is what found it."
)
PLAN[569] = P(
    "**2. Server-side p95 (687.1 ms) and client-side p95 (524.3 ms) disagree by 163 ms.** Not an error — a "
    'property of histograms. The real observations cluster just above 0.5 s, but the surrounding buckets are '
    '`le="0.5"` and `le="0.75"`, so `histogram_quantile` interpolates linearly across that 250 ms gap and '
    'lands near 687 ms. The raw buckets show why it has nothing better to go on: 117 observations at '
    '`le="0.25"`, still 117 at `le="0.5"`, then 142 at `le="0.75"`.'
)
PLAN[570] = P(
    "**A histogram's percentile is only as precise as its bucket boundaries.** The client, holding every raw "
    "sample, can report the true value; Prometheus trades that precision for the ability to aggregate across "
    'instances and any window. Knowing which number is which matters before anyone sets an SLO: "p95 under '
    '600 ms" would read as breached by Prometheus and met by the client, on the same requests. It reproduced '
    "to within 1 ms across two runs on different days and different infrastructure, so it is a property of "
    "the bucket layout rather than noise."
)
PLAN[574] = D
PLAN[577] = P(
    "Prometheus stores **one time series per unique combination of label values**. A label drawn from a small "
    "fixed set — an HTTP method, a status code — adds a handful. A label carrying an identifier adds one "
    "series for **every distinct value ever seen**, each with its own samples, index entries and memory in "
    "the head block."
)
PLAN[597] = P(
    "**Bounded by design, as the brief requires.** `MAX_UNIQUE_IDS` caps the labelled counter at 100 distinct "
    "ids; past the cap the script re-uses an id rather than minting new series, and `--max-ids` above 100 is "
    "refused outright. 100 series is nothing to a Prometheus server — the lesson is the shape of the growth "
    "curve, not the damage. This project does not try to crash Prometheus."
)
PLAN[601] = D
PLAN[602] = D
PLAN[613] = P(
    "Screenshot 7 — `count(demo_requests_total)` in Prometheus's own graph view, 16:03–16:07 UTC. Proves "
    "Part E2 steps 2–3: series growth through the exact query the brief names, and the effect of removing the label."
)
PLAN[614] = P(
    "The line sits at exactly **100** from about 16:04:55 to 16:05:20 and does not exist either side of it — "
    "nothing before the demo was first scraped, nothing after the restart without the label. No slow fade "
    "over the 5-minute lookback. The cut is at a single scrape."
)
PLAN[617] = D
PLAN[621] = D
PLAN[626] = P(
    "I expected the second row to still read 100 — that removing a label stops new series being created but "
    "leaves the existing ones until retention expires. That is the usual explanation, and it is what an "
    "earlier draft of this report asserted. **The measurement contradicted it**, and the reason is worth more "
    "than the original point."
)
PLAN[627] = P(
    "`count(demo_requests_total)` as an **instant query** returns no data once the label is removed. The same "
    "query evaluated at a past timestamp still returns 100. Running it across a series of `time=` values maps "
    "the boundary exactly:"
)
PLAN[629] = P(
    "Both results are true, and not in conflict. The history is intact: all 100 series are still on disk and "
    "still queryable at any timestamp where they were live, until the 15-day retention window drops them. "
    "What changed is what a query evaluated *now* can see."
)
PLAN[630] = P(
    "**Why the cutoff is sharper than the 5-minute lookback.** An instant query normally carries the last "
    "sample forward for up to 5 minutes, so the series was predicted to linger until ~16:10. It does not — it "
    "disappears between 16:05:10 and 16:05:30, at the first scrape after the restart. When a target stops "
    "exporting a series, Prometheus writes an explicit stale marker, and that marker ends the series "
    "immediately. The lookback applies when a target goes missing, not when it is scraped successfully and "
    "simply no longer reports that series."
)
PLAN[632] = P("This is the part the brief specifically warns about, and the measurement above makes it easy to draw the wrong conclusion.")
PLAN[634] = P(
    "**The practical lesson, stronger than the one I started with.** You cannot verify a cardinality fix by "
    "watching `count()` go down, because it goes down either way — whether you fixed the label or merely "
    "stopped the app. The timeline of a real incident is: deploy the bad label, memory climbs, notice, ship a "
    "fix, then wait days for the server to recover. There is no command that undoes it. Cardinality is a "
    "code-review question, not an incident-response one."
)
PLAN[638] = P(
    "A single Prometheus server is generally comfortable into the low millions of active series. At 100 req/s "
    "this design passes that **in one day**, and the failure is not graceful: memory climbs until the process "
    "is killed, taking the monitoring for everything else with it, at precisely the moment you need it."
)
PLAN[639] = P(
    "The cost is not only Prometheus's. Every one of those series is stored, indexed and replicated for the "
    'whole retention window while being queried exactly never: **nobody asks "how many requests had id '
    'a3f9c2?"**'
)
PLAN[643] = P(
    "So this app puts the request id in **every log line** and in **no metric label**. "
    '`gym_http_requests_total` is labelled by route *template* ("/weekly-report"), never the raw path — which '
    'is also why an unmatched path collapses to a single `route="unmatched"` series, so a scanner probing '
    "/wp-login.php and /.env cannot mint series from outside."
)
PLAN[644] = P(
    "**Enforced in the test suite, not just documented.** `test_labels_are_bounded_dimensions_only` fails if "
    "anyone adds `user_id`, `request_id`, `session_date` or `username` as a metric label. The cheapest moment "
    "to catch a cardinality mistake is before it merges."
)
PLAN[648] = D

# --------------------------------------------------------------------------- #
# Verification Status — kept, tightened
# --------------------------------------------------------------------------- #
PLAN[650] = P("Stated plainly, because a report that quietly implies more testing than happened is worse than one that draws the line.")
PLAN[655] = [
    CELL(6, 1, "Re-run against the live stack, with server-side percentiles queried from Prometheus via `histogram_quantile` rather than replicated in Python"),
    CELL(7, 1, "Re-run through Prometheus with `count(demo_requests_total)` against a running server — which is what disproved this report's own earlier claim about removing a label"),
    CELL(8, 1, 'All six in docs/screenshots/ are from this live stack. Every panel shown was checked for data; two that read "No data" while the app was healthy were fixed'),
]
PLAN[657] = P("Five configuration defects, none of which file-level validation would have caught, because every one of these files was syntactically valid.")
PLAN[659] = P(
    "Plus one environment-specific change that is not a defect in the original config: the node-exporter "
    "target moved from `host.docker.internal:9100` to the pinned bridge gateway `172.28.77.1:9100`, for the "
    "WSL2 reason in Part B.7."
)
PLAN[660] = P(
    "**What I take from this.** Every one of these files parsed, and `docker compose config` validated the "
    "compose file. Four of the five defects were semantic agreements between two different systems: a label "
    "spelling shared between Docker and Filebeat, three names Filebeat and Elasticsearch both have to agree "
    "on, a mountpoint that exists on one kernel and not another. Validation checks a file against a grammar; "
    "only running the thing checks it against reality. The most misleading was #2, because Filebeat reported "
    "no error at all — it started cleanly, connected successfully, and quietly collected nothing."
)
PLAN[662] = [
    CELL(1, 1, "The policy is attached to the write index and reports `phase: hot`. Verified: it exists, is valid, is bound to the index. Not verified: that Elasticsearch has executed either transition"),
    CELL(3, 1, "This machine has no `GROQ_API_KEY`, so the extraction metrics and the weekly-report counter have no data, and three business panels are empty. Verified: the app behaves correctly without a key"),
    CELL(4, 1, "`rapidfuzz` is absent from the host Python, so collection fails before any test executes. They pass in the container image, which is where the app runs. An environment gap, not a code defect"),
]
PLAN[666] = D

# --------------------------------------------------------------------------- #
# Final Verification Checklist — deleted whole (113 tick-box rows restating A–E)
# --------------------------------------------------------------------------- #
for _block in range(667, 687):
    PLAN[_block] = D

# --------------------------------------------------------------------------- #
# Credits
# --------------------------------------------------------------------------- #
PLAN[688] = [
    CELL(3, 1, "Claude (Anthropic) was used to implement the observability layer, the compose stack, the dashboards and this report, working from the brief and the existing codebase. All of it was reviewed, and the experiments were run and their results recorded"),
    CELL(4, 1, "Prometheus docs (metric types, naming, the label cardinality guidance the brief links), Grafana provisioning docs, Elastic's Filebeat / ECS / ILM docs, `prometheus_client` source for the Summary quantile question"),
    CELL(5, 1, "RED (Rate, Errors, Duration) for the application dashboard, after Tom Wilkie; USE (Utilisation, Saturation, Errors) informed the host dashboard"),
]
PLAN[690] = P(
    "**This is a local lab, not a production deployment.** Elasticsearch runs with security disabled and "
    "Grafana with a default password, because requiring a TLS handshake and a credential rotation to look at "
    "a chart on localhost teaches nothing about observability. Both would be wrong on any network anyone else "
    "can reach. The `/metrics` endpoint does support a bearer token (`METRICS_TOKEN`, compared with "
    "`hmac.compare_digest`) for a deployment where that matters."
)

# =========================================================================== #
# Pass 2 — harder cuts. Entries below replace the pass-1 entry for the same
# block, so each one lists every edit that block ends up with.
# =========================================================================== #

# --- Part A / B tables ----------------------------------------------------- #
PLAN[90] = [
    CELL(1, 1, "Every response served. Numerator and denominator of the error rate."),
    CELL(2, 1, "Raised out of the handler. Separates a crash from a deliberate 500."),
    CELL(3, 1, "BUSINESS. Entries reaching a terminal state at save."),
    CELL(4, 1, "BUSINESS. Individual sets inserted — the product's unit of value."),
    CELL(5, 1, "BUSINESS. Bodyweight readings inserted."),
    CELL(6, 1, "BUSINESS. How the duplicate prompt was answered."),
    CELL(7, 1, "BUSINESS. Groq extraction calls by outcome."),
    CELL(8, 1, "BUSINESS. Reports generated, by narration success."),
    CELL(9, 1, "Auth outcomes. Failures without successes is credential stuffing."),
    CELL(10, 1, "Part E only. Proves from metrics alone when a fault was live."),
]
PLAN[92] = [
    CELL(1, 1, "Concurrent requests. The saturation signal on one worker."),
    CELL(3, 1, "BUSINESS. Entries parsed but not yet confirmed."),
]
PLAN[94] = [CELL(2, 1, "BUSINESS. One Groq extraction, retries included.")]
PLAN[97] = [CELL(1, 1, "BUSINESS. Size of a pasted entry. Drives the token budget.")]
PLAN[174] = [
    CELL(1, 1, "The Docker Desktop Linux VM, not Windows"),
    CELL(3, 1, "12 cores, 7.56 GiB RAM"),
    CELL(5, 1, "Every container in the stack is a process on this kernel"),
]

# --- Part C tables --------------------------------------------------------- #
PLAN[228] = [
    CELL(1, 1, "One JSON object per line to stdout, in ECS field names."),
    CELL(2, 1, "The json-file driver wraps each line in an envelope, rotating at 10 MB × 3."),
    CELL(3, 1, "Autodiscover starts a harvester for any container carrying the label."),
    CELL(4, 1, "`decode_json_fields` parses the JSON and promotes its keys to the event root."),
    CELL(5, 1, "Bulk-indexed into filebeat-gym-tracker-logs, ILM bound to the write index."),
    CELL(6, 1, "Data view over filebeat-gym-tracker-*, plus 8 saved searches."),
]
PLAN[232] = [
    CELL(1, 1, "ISO-8601, UTC, milliseconds — when the event happened"),
    CELL(5, 1, "A short human sentence, not a key=value blob"),
    CELL(6, 1, "The correlation id, one per HTTP request"),
    CELL(9, 1, "Route template where known, raw path otherwise"),
    CELL(10, 1, "Integer, so numeric range queries work"),
    CELL(12, 1, "What happened, e.g. entry_saved"),
    CELL(13, 1, "On exceptions, all three scrubbed"),
    CELL(15, 1, "Added by Filebeat from the Docker API"),
]
PLAN[236] = [
    CELL(1, 1, "password, new_password, token, session, cookie, authorization"),
    CELL(1, 2, "test_sensitive_keys_are_replaced"),
    CELL(2, 2, "test_secret_shaped_values_are_scrubbed_from_free_text"),
    CELL(3, 1, "Bearer / Basic followed by 8+ credential characters"),
    CELL(3, 2, "(same)"),
    CELL(4, 2, "(same)"),
    CELL(5, 1, "`error.message` and `error.stack_trace` both pass through the scrubber"),
]
PLAN[269] = [
    CELL(1, 2, "The one place every request passes through, so coverage cannot drift"),
    CELL(2, 2, "The parse step is where the external dependency fails"),
    CELL(3, 2, "The terminal business event; pairs with `gym_sets_written_total`"),
    CELL(4, 2, "Security signal; the username would be a disclosure"),
    CELL(5, 2, "Diagnosing a slow LLM needs the retry structure, not just the outcome"),
    CELL(6, 2, "Reading a degraded process's log stream should make the reason obvious"),
]
PLAN[307] = [
    CELL(2, 2, "The app's `@timestamp` — when the event happened — replaces Filebeat's, which is when the line was read. They differ by the shipping delay, exactly the error you do not want when correlating a log against a metric spike"),
    CELL(3, 2, "Losing a malformed log is how a parsing bug hides for months"),
]
PLAN[346] = [
    CELL(1, 1, "Every request served, newest first"),
    CELL(3, 1, "Errors and warnings only"),
    CELL(8, 1, "One request end to end, by id"),
]

# --- Part D tables --------------------------------------------------------- #
PLAN[417] = [
    CELL(1, 1, "Serves the app, exposes `/metrics`, writes JSON to stdout"),
    CELL(4, 1, "Scrapes `/metrics` every 15s, stores in its TSDB, evaluates alerts"),
    CELL(5, 1, "Reads /proc and /sys, exposes host metrics"),
    CELL(6, 1, "Queries Prometheus and Elasticsearch, draws dashboards"),
    CELL(8, 1, "Tails container logs, parses, ships"),
    CELL(9, 1, "Indexes and stores documents, enforces ILM"),
]
PLAN[422] = [
    CELL(1, 2, "/healthz, /metrics, logs. `pool_pre_ping` avoids dead connections"),
    CELL(1, 3, "Restart; the volume persists. Visible as `gym_http_exceptions_total`"),
    CELL(2, 1, "Parsing fails at `POST /log`. Nothing was saved, so nothing is lost"),
    CELL(2, 2, "Everything else. Weekly report narration degrades; the numbers are computed in code"),
    CELL(2, 3, 'Retry. Visible as `gym_llm_extractions_total{outcome="error"}`'),
    CELL(3, 2, "Everything. The app does not know it is being scraped"),
    CELL(3, 3, "Restart. The gap in history is permanent"),
    CELL(5, 2, "Prometheus still collects and alerts; only the view is lost"),
    CELL(5, 3, "Restart. Dashboards re-provision from files"),
    CELL(6, 2, "The app logs to stdout; Docker keeps the files"),
    CELL(6, 3, "Restart. Filebeat resumes from its registry, unless Docker rotated the files away first — the real data-loss window"),
    CELL(9, 2, '`up{job="gym-tracker"} == 0` fires TargetDown within a minute'),
    CELL(9, 3, "The one failure the app cannot report itself, which is why `up` is synthesised by the scraper"),
]
PLAN[425] = [
    CELL(1, 1, "That ILM deletes whole indices because deleting documents only marks them until a merge"),
    CELL(1, 2, "The merge policy's scheduling, or how to tune it"),
    CELL(2, 1, 'The head block, the WAL and 2-hour compaction, at the level of "why a restart does not lose recent data"'),
    CELL(2, 2, "Predicting memory use from a series count with any precision"),
    CELL(3, 1, "That it interpolates linearly within a bucket, bounded by bucket width — the Part E numbers show this"),
    CELL(3, 2, "Its behaviour when observations cluster exactly at a boundary, or fall in +Inf"),
    CELL(4, 1, "That it retries and backs off when Elasticsearch rejects a bulk request"),
    CELL(4, 2, "How the queue and file rotation interact to decide when a log is lost rather than delayed"),
]

# --- Part E tables --------------------------------------------------------- #
PLAN[525] = [
    CELL(1, 2, "the 95th percentile lands inside the delayed group"),
    CELL(2, 2, "the median request is not a delayed one"),
    CELL(5, 2, "a closed-loop client issues fewer requests when each takes longer"),
    CELL(7, 2, "a delay is not a failure; every request still returns 200"),
]
PLAN[533] = [
    CELL(1, 1, "`FAULT_INJECTION_ENABLED` must be true. Two variables have to agree before any request is delayed"),
    CELL(2, 1, "Refuses to arm when `APP_ENV` is production. A copied .env cannot degrade a real deployment"),
    CELL(3, 1, "One loud warning at startup, naming what is armed"),
    CELL(4, 1, "Every fault increments `gym_faults_injected_total`, so the experiment does not rest on trusting a variable"),
    CELL(5, 1, "Clamped to `MAX_LATENCY_MS` = 10,000, so a mistyped 5000-for-500 cannot pin a worker"),
    CELL(6, 1, "`asyncio.sleep`, never `time.sleep`, which on one async worker would measure queueing instead of latency"),
]
PLAN[565] = [
    CELL(1, 2, "p95 rose 143× server-side; the SlowRequests alert is the rule this fault approaches"),
    CELL(2, 2, "0.00% throughout, `up == 1` throughout. Errors-and-uptime monitoring would have seen nothing"),
    CELL(3, 2, "6.3 → 20.2 ms client-side, 2.5 → 3.1 ms server-side"),
    CELL(4, 2, "−31% with offered load unchanged. This is how a latency problem becomes a capacity problem"),
    CELL(5, 2, "120 warning documents, searchable by `fault.kind`, correlatable by request id"),
    CELL(6, 2, "absent → 121 → absent, which makes this evidence rather than an assertion"),
]
PLAN[633] = [
    CELL(1, 1, "The series current dashboards and alerts see — an instant query drops it at the first scrape after the restart"),
    CELL(2, 1, "The stored samples, the index entries and the disk they occupy, for the full 15 days. A range query over a past window still returns all 100"),
    CELL(3, 1, "The memory pressure while those series were active. If a cardinality explosion kills a Prometheus server, removing the label brings the next one up; it does not recover what was lost while it was down"),
]

# --- Verification / Credits tables ---------------------------------------- #
PLAN[652] = [
    CELL(2, 1, "test_metrics_endpoint_serves_exposition_format, test_metrics_reports_all_four_types"),
    CELL(3, 1, "tests/test_observability.py — 17 tests across formatter and redaction"),
    CELL(4, 1, "scripts/experiment_anomaly.sh, three stages, results in observability/results-live/"),
    CELL(6, 1, "test_unique_ids_are_capped_at_one_hundred; reproduced at the command line"),
    CELL(7, 1, "docker compose config; every YAML/JSON parses; shell scripts pass bash -n"),
]
PLAN[655] = [
    CELL(2, 1, "Rendering all three provisioned dashboards; both datasources healthy"),
    CELL(3, 1, "Tailing Docker logs, `decode_json_fields` producing queryable fields"),
    CELL(6, 1, "Re-run live, with server-side percentiles queried from Prometheus rather than replicated in Python"),
    CELL(7, 1, "Re-run through Prometheus with `count(demo_requests_total)`, which is what disproved this report's own earlier claim about removing a label"),
    CELL(8, 1, 'All six in docs/screenshots/ are from this stack. Two panels reading "No data" while the app was healthy were fixed'),
    CELL(9, 1, "Real data: entries submitted through the review form"),
]
PLAN[658] = [
    CELL(1, 2, "The `rslave` mount propagation flag on node-exporter's `/:/host/root` mount is unsupported on Docker Desktop's VM"),
    CELL(2, 2, "Autodiscover condition written with de-dotted label names. Filebeat's event fields de-dot, but the autodiscover condition matches the dotted form"),
    CELL(3, 2, "A JSON file cannot hold comments, and Elasticsearch rejects unknown top-level fields"),
    CELL(4, 2, "Filebeat 8.x writes to a data stream; the template name, the `index:` target and the ILM alias must all agree. They did not"),
    CELL(5, 2, '`mountpoint="/"` does not exist on the Docker Desktop VM; the data disk is /var/lib'),
]
PLAN[662] = [
    CELL(1, 1, "Attached to the write index, reporting `phase: hot`. Verified: it exists, is valid, is bound. Not verified: that either transition has executed"),
    CELL(3, 1, "No `GROQ_API_KEY` on this machine, so the extraction metrics have no data and three business panels are empty. Verified: the app behaves correctly without a key"),
    CELL(4, 1, "`rapidfuzz` is absent from the host Python, so collection fails before any test runs. They pass in the container image, which is where the app runs"),
]
PLAN[688] = [
    CELL(2, 1, "metrics.py, logging_setup.py, faults.py, observability/, scripts/, Dockerfile, docker-compose.yml, tests/test_observability.py, and the instrumentation call sites"),
    CELL(3, 1, "Claude (Anthropic) was used to build the observability layer, the compose stack, the dashboards and this report. All of it was reviewed, and the experiments were run and recorded"),
    CELL(4, 1, "Prometheus docs (metric types, naming, label cardinality), Grafana provisioning docs, Elastic's Filebeat / ECS / ILM docs, `prometheus_client` source"),
    CELL(5, 1, "RED (Rate, Errors, Duration) for the application dashboard, after Tom Wilkie; USE informed the host dashboard"),
]

# --- remaining long prose -------------------------------------------------- #
PLAN[95] = P(
    "Boundaries are per metric, because the three live on different scales: HTTP latency 5 ms to 10 s, the "
    "LLM 0.25 s to 60 s, the database 5 ms to 5 s. Buckets are the resolution limit of every percentile taken "
    "from them, and Part E shows exactly that."
)
PLAN[112] = P(
    "**A gauge that can only rise is worse than no gauge at all.** A naive counter drifts upward until the "
    "process restarts, and the dashboard reads that as a real backlog. So a draft carries a timestamp, ages "
    "out after 30 minutes, and the map is capped (`DraftTracker`). No user id or session date becomes a label."
)
PLAN[129] = P(
    '"95% of requests finished in this time or less, over the last 5 minutes." Part E1 is the argument for it: '
    'during the fault, server-side p95 went from 4.8 ms to 687.1 ms while p50 moved from 2.5 ms to 3.1 ms. A '
    'median-only dashboard would have called the service healthy.'
)
PLAN[130] = P(
    "**Two constraints, both easy to get wrong.** The window must hold at least four scrapes, so at 15 seconds "
    "nothing below `[1m]` is trustworthy. And a percentile cannot be averaged — sum the buckets, then take the "
    "quantile of the sum."
)
PLAN[137] = P(
    "**What the chart shows.** The overview row reads left to right as RED. Latency shows the fault plainly: "
    "flat until 21:00, then p95 and p99 step to ~504 ms and ~701 ms and hold, while p50 stays at 2.6 ms. The "
    "heatmap makes the bimodality literal — two bands, 5–10 ms and 500–750 ms, nothing between."
)
PLAN[157] = P(
    '**What the chart shows.** 18 sets logged and 5 entries saved in 24 hours, 0 drafts awaiting review, '
    'average entry 20 B. All five entries land on `outcome="saved"`. Average sets per saved entry reaches 3.60; '
    'commit p95 is 85 ms against a p50 of 17.5 ms.'
)
PLAN[175] = P(
    "**Why not `host.docker.internal`?** On Docker Desktop it resolves to Windows, not the Linux VM the "
    "exporter runs in. The bridge gateway `172.28.77.1` is the VM itself, so the subnet is pinned in "
    "`docker-compose.yml`."
)
PLAN[235] = P(
    "**The filter is a backstop, not the policy.** Call sites log ids and counts, never content. "
    "`SensitiveDataFilter` sits on the handler rather than a logger, so library output is covered too."
)
PLAN[309] = P(
    "The app emits JSON rather than plain text, which puts the parsing at the producer — the only place that "
    "still knows the types. It used to log `event=request method=POST path=/save status=200`: readable in a "
    "terminal, miserable downstream, where one unescaped space breaks a grok pattern silently."
)
PLAN[352] = P(
    "**What this proves, field by field.** Three documents share the id 8d59a31cdc6f454c and the millisecond "
    "01:26:02.896: the business event (`event.action: entry_saved`), the middleware line (`url.path: /save`, "
    "status 200, 9.4 ms), and uvicorn's access line — all three in the same JSON shape, because uvicorn's "
    "handlers were cleared."
)
PLAN[353] = P(
    "The left rail shows 102 fields discovered from the index, with type icons: `k` for keyword, `t` for text "
    "on `message`, `#` for number on the status code and duration. Those icons are the evidence that "
    "`decode_json_fields` worked."
)
PLAN[420] = P(
    '**The load-bearing design decision.** Metrics answer "how many, how fast, right now" cheaply, across a '
    'few bounded dimensions. Logs answer "what happened to this one request" at a cost per event. Using either '
    "for the other's job is how both bills get large."
)
PLAN[423] = P(
    "**Observability failures degrade observability, not the application.** The app does not know whether "
    "Prometheus is scraping it, and writes to stdout whether or not anything reads. Hence a plain `/metrics` "
    "endpoint and a stdout write."
)
PLAN[500] = P(
    "**The two paths meeting.** The metric says 12 sets were written in the last hour. The log says which "
    "entries, by whom, in which request. `http.request.id` is what walks you from a spike to the clicks behind it."
)
PLAN[549] = P(
    "**The metric and the log agree to within one event.** `gym_faults_injected_total` reports 121 while the "
    "stage window holds 120. The extra one is the first request the recreated process served, delayed at "
    "15:59:10.386. I assumed it came from the readiness probe and wrote that down; following the request id "
    "showed otherwise."
)
PLAN[557] = P(
    "Configuration is read once at construction, not per request, so settings cannot drift mid-experiment. "
    'Changing a fault means restarting — which is what makes "remove the problem and repeat the test" honest.'
)
PLAN[567] = P(
    "**1. The instrumentation was wrong, and the experiment caught it.** The first run contradicted itself: "
    "the client measured a p95 of 506 ms while the server histogram put every request in the fastest bucket. "
    "The fault was applied *before* `started = time.perf_counter()`, so the delay fell outside the timed "
    "region. Prometheus reported a healthy service while every client waited half a second."
)
PLAN[569] = P(
    "**2. Server-side p95 (687.1 ms) and client-side p95 (524.3 ms) disagree by 163 ms.** Not an error — a "
    'property of histograms. The observations cluster just above 0.5 s, but the surrounding buckets are '
    '`le="0.5"` and `le="0.75"`, so `histogram_quantile` interpolates across that 250 ms gap. The raw buckets: '
    '117 at `le="0.25"`, still 117 at `le="0.5"`, then 142 at `le="0.75"`.'
)
PLAN[570] = P(
    "**A histogram's percentile is only as precise as its bucket boundaries.** The client holds every raw "
    'sample and can report the true value; Prometheus trades that for aggregation. An SLO of "p95 under 600 ms" '
    "would read as breached by Prometheus and met by the client, on the same requests. It reproduced to within "
    "1 ms across two runs on different infrastructure, so it is the bucket layout, not noise."
)
PLAN[577] = P(
    "Prometheus stores **one time series per unique combination of label values**. A label from a small fixed "
    "set adds a handful. A label carrying an identifier adds one series for **every distinct value ever seen**, "
    "each with its own samples, index entries and head-block memory."
)
PLAN[597] = P(
    "**Bounded by design, as the brief requires.** `MAX_UNIQUE_IDS` caps the labelled counter at 100; past the "
    "cap the script re-uses an id, and `--max-ids` above 100 is refused. 100 series is nothing to Prometheus — "
    "the lesson is the growth curve, not the damage."
)
PLAN[626] = P(
    "I expected the second row to still read 100 — that removing a label stops new series but leaves the "
    "existing ones until retention expires. That is the usual explanation, and what an earlier draft asserted. "
    "**The measurement contradicted it.**"
)
PLAN[630] = P(
    "**Why the cutoff is sharper than the 5-minute lookback.** An instant query normally carries the last "
    "sample forward for 5 minutes, so the series was predicted to linger until ~16:10. It disappears between "
    "16:05:10 and 16:05:30 instead, at the first scrape after the restart: when a target stops exporting a "
    "series, Prometheus writes a stale marker, and that ends the series immediately."
)
PLAN[634] = P(
    "**The practical lesson, stronger than the one I started with.** You cannot verify a cardinality fix by "
    "watching `count()` go down, because it goes down either way — fixed label or stopped app. There is no "
    "command that undoes it. Cardinality is a code-review question, not an incident-response one."
)
PLAN[638] = P(
    "A single Prometheus server is comfortable into the low millions of active series. At 100 req/s this "
    "design passes that **in one day**, and not gracefully: memory climbs until the process is killed, taking "
    "the monitoring for everything else with it."
)
PLAN[643] = P(
    "So the request id goes in **every log line** and in **no metric label**. `gym_http_requests_total` is "
    'labelled by route *template*, never the raw path — which is also why an unmatched path collapses to one '
    '`route="unmatched"` series, so a scanner cannot mint series from outside.'
)
PLAN[660] = P(
    "**What I take from this.** Every one of these files parsed. Four of the five defects were semantic "
    "agreements between two systems: a label spelling, three names Filebeat and Elasticsearch both have to "
    "agree on, a mountpoint that exists on one kernel and not another. Validation checks a file against a "
    "grammar; only running it checks against reality. #2 was the worst, because Filebeat started cleanly, "
    "connected successfully, and quietly collected nothing."
)
PLAN[690] = P(
    "**A local lab, not a production deployment.** Elasticsearch runs with security disabled and Grafana with "
    "a default password, because a TLS handshake to look at a chart on localhost teaches nothing about "
    "observability. Both would be wrong on a reachable network. `/metrics` does support a bearer token "
    "(`METRICS_TOKEN`, via `hmac.compare_digest`)."
)

# --- captions -------------------------------------------------------------- #
PLAN[136] = P(
    'Screenshot 1 — Grafana "Application performance", captured live across the Part E1 window. Proves Part B '
    "(Prometheus and Grafana, application metrics, Counter / Gauge / Histogram, p95 and p99) and Part E1."
)
PLAN[177] = P(
    'Screenshot 3 — Grafana "Host (node-exporter)". Proves the Node Exporter requirement: CPU, memory, disk '
    "and network, scraped by Prometheus, drawn in Grafana, machine named in the header."
)
PLAN[351] = P(
    "Screenshot 4 — Kibana Discover tracing one `POST /save` by request id. Proves Part C (working search, "
    "parsed fields, time / service / severity / message / request id) and Part D.3."
)
PLAN[415] = P(
    "Figure D.1 — Full architecture. Solid arrows push, the Prometheus arrows pull. [STORE] marks a persistent "
    "volume. Mermaid source at `docs/architecture.mmd`."
)
PLAN[547] = P(
    'Screenshot 6 — Kibana Discover, `fault.kind: "latency"`, 20:59:00–21:01:30 local. 121 documents, all '
    "warning level, each naming the delayed route. Proves Part E1 step 3 and Part C."
)
PLAN[613] = P(
    "Screenshot 7 — `count(demo_requests_total)` in Prometheus's graph view, 16:03–16:07 UTC. Proves Part E2 "
    "steps 2–3: series growth through the query the brief names, and the effect of removing the label."
)

# --- more code lines ------------------------------------------------------- #
for _block in (243, 244, 247, 250, 258, 259, 260, 282, 283, 288, 289, 302, 303, 304, 305,
               306, 316, 317, 318, 325, 330, 482, 483, 542, 582, 583, 584, 585, 586,
               592, 593, 594, 595, 596, 603, 604):
    PLAN[_block] = D

# =========================================================================== #
# Pass 3 — put back the code lines pass 2 cut mid-statement, drop the tables
# that duplicate the B.2 inventory, and trim the remaining tables telegraphically.
# =========================================================================== #

# Pass 2 deleted these and left a dangling `- condition:`, an unterminated
# Counter(, a for-loop with no header, and the index/template settings that the
# paragraph underneath them explains. Keep them.
for _block in (258, 259, 260, 282, 283, 316, 317, 318, 482, 483,
               582, 583, 584, 585, 586, 592, 593, 594, 595, 596):
    PLAN.pop(_block, None)

# B.3 and B.4 restated the Purpose column of the B.2 inventory, which already
# marks every business metric with "BUSINESS.". The prose and headings stay.
PLAN[106] = D
PLAN[109] = D
PLAN[105] = P(
    "These measure the machinery — fast, up, correct — and are the six marked without a BUSINESS prefix in "
    "B.2. The application dashboard is laid out on the RED method: Rate, Errors, Duration."
)
PLAN[108] = P(
    "These measure what the product is for: the nine marked BUSINESS in B.2. Every one can move while the "
    "application metrics stay perfectly healthy, which is why they are kept apart."
)

# Duplicates rows 1 and 2 of the "Still not verified" table.
PLAN[366] = D

# D.2's extra scrape dump repeats the mean-versus-percentile point Part E1 makes
# with the same numbers. The required trace (steps 1 to 5) stays.
for _block in range(455, 464):
    PLAN[_block] = D

PLAN[568] = D  # one-line moral under the finding it moralises

# --- telegraphic table cells ---------------------------------------------- #
PLAN[36] = [
    CELL(1, 0, "Paste a messy entry, get structured sets"),
    CELL(2, 0, "Review before save, with per-row confidence"),
    CELL(4, 0, 'Fuzzy matching, so "bench" and "Bench Press" stay one exercise'),
    CELL(5, 0, "Duplicate guard that asks rather than refuses"),
    CELL(12, 0, "Live set-by-set logging, routines, PRs, measurements"),
]
PLAN[422] = [
    CELL(1, 1, "/healthz, /metrics, logs. `pool_pre_ping` avoids dead connections"),
    CELL(1, 2, "Restart; the volume persists"),
    CELL(2, 1, "Parsing fails at `POST /log`. Nothing saved, nothing lost"),
    CELL(2, 2, "Everything else. Report narration degrades; numbers are computed in code"),
    CELL(2, 3, 'Retry. Visible as `gym_llm_extractions_total{outcome="error"}`'),
    CELL(3, 1, "No new metrics, blank dashboards. The app is unaffected"),
    CELL(3, 2, "Everything. The app does not know it is scraped"),
    CELL(3, 3, "Restart. The gap in history is permanent"),
    CELL(5, 2, "Prometheus still collects and alerts; only the view is lost"),
    CELL(5, 3, "Restart. Dashboards re-provision from files"),
    CELL(6, 1, "Filebeat cannot ship, backs off, retries"),
    CELL(6, 2, "The app logs to stdout; Docker keeps the files"),
    CELL(6, 3, "Restart. Resumes from the registry, unless Docker rotated the files away — the real data-loss window"),
    CELL(9, 2, '`up{job="gym-tracker"} == 0` fires TargetDown within a minute'),
    CELL(9, 3, "The one failure the app cannot report itself, which is why `up` is synthesised by the scraper"),
]
PLAN[425] = [
    CELL(1, 1, "That ILM deletes whole indices, because deleting documents only marks them"),
    CELL(1, 2, "The merge policy's scheduling, or how to tune it"),
    CELL(2, 1, "The head block, the WAL, 2-hour compaction"),
    CELL(2, 2, "Predicting memory use from a series count with any precision"),
    CELL(3, 1, "That it interpolates linearly within a bucket, bounded by bucket width"),
    CELL(3, 2, "Its behaviour at an exact boundary, or in +Inf"),
    CELL(4, 1, "That it retries and backs off on a rejected bulk request"),
    CELL(4, 2, "When the queue and file rotation lose a log rather than delay it"),
]
PLAN[658] = [
    CELL(1, 1, 'docker compose up aborts: "path / is mounted on / but it is not a shared or slave mount"'),
    CELL(1, 2, "`rslave` mount propagation is unsupported on Docker Desktop's VM"),
    CELL(2, 2, "Autodiscover condition written de-dotted. Event fields de-dot; the condition matches the dotted form"),
    CELL(3, 2, "JSON cannot hold comments, and Elasticsearch rejects unknown top-level fields"),
    CELL(4, 2, "Filebeat 8.x writes to a data stream; template name, `index:` target and ILM alias must agree"),
    CELL(5, 2, '`mountpoint="/"` does not exist on the VM; the data disk is /var/lib'),
]
PLAN[688] = [
    CELL(1, 1, "FastAPI app, extraction pipeline, review flow, insights, accounts — my own prior work here"),
    CELL(2, 1, "metrics.py, logging_setup.py, faults.py, observability/, scripts/, Dockerfile, docker-compose.yml, tests/test_observability.py, and the call sites"),
    CELL(3, 1, "Claude (Anthropic) built the observability layer, compose stack, dashboards and this report. All reviewed; the experiments were run and recorded"),
    CELL(4, 1, "Prometheus docs (types, naming, label cardinality), Grafana provisioning, Elastic Filebeat / ECS / ILM, `prometheus_client` source"),
    CELL(5, 1, "RED for the application dashboard, after Tom Wilkie; USE informed the host dashboard"),
    CELL(6, 1, "Lab 1 — Midnight Launch, as a reference for the stack layout"),
]
PLAN[655] = [
    CELL(1, 1, "Scraping app and node-exporter. All targets up"),
    CELL(2, 1, "All three dashboards rendering; both datasources healthy"),
    CELL(3, 1, "Tailing Docker logs, `decode_json_fields` producing queryable fields"),
    CELL(4, 1, "Indexing, ILM policy attached to the write index"),
    CELL(5, 1, 'All nine saved objects imported ("successCount":9)'),
    CELL(6, 1, "Re-run live, server-side percentiles queried from Prometheus"),
    CELL(7, 1, "Re-run through Prometheus, which disproved this report's own earlier claim about removing a label"),
    CELL(8, 1, 'All six from this stack. Two panels reading "No data" while healthy were fixed'),
    CELL(9, 1, "Real data, submitted through the review form"),
]
PLAN[652] = [
    CELL(1, 0, "1038 tests pass (18 skipped), 63 on the observability layer"),
    CELL(2, 0, "`/metrics` serves valid exposition format, all four types present"),
    CELL(2, 1, "test_metrics_endpoint_serves_exposition_format, test_metrics_reports_all_four_types"),
    CELL(3, 0, "JSON logging, request-id correlation, all four redaction paths"),
    CELL(3, 1, "tests/test_observability.py — 17 tests"),
    CELL(4, 0, "Fault injection end to end; every Part E1 number is measured"),
    CELL(4, 1, "scripts/experiment_anomaly.sh, three stages, observability/results-live/"),
    CELL(5, 0, "The cardinality demo, and the growth table as measured output"),
    CELL(6, 0, "The cap holds at 100; `--max-ids` above it is refused"),
    CELL(6, 1, "test_unique_ids_are_capped_at_one_hundred"),
    CELL(7, 0, "Configs valid; no duplicate panel ids or refIds"),
    CELL(7, 1, "docker compose config; every YAML/JSON parses; bash -n"),
]
PLAN[662] = [
    CELL(1, 1, "Attached to the write index, `phase: hot`. Verified: exists, valid, bound. Not verified: that either transition ran"),
    CELL(2, 1, "10 MB × 3 has not been reached"),
    CELL(3, 1, "No `GROQ_API_KEY` here, so the extraction metrics have no data and three business panels are empty. The app does behave correctly without a key"),
    CELL(4, 1, "`rapidfuzz` is absent from the host Python, so collection fails before any test runs. They pass in the container image"),
]
PLAN[417] = [
    CELL(1, 1, "Serves the app, exposes `/metrics`, writes JSON to stdout"),
    CELL(2, 1, "Stores workouts, users, reports"),
    CELL(3, 1, "LLM extraction. The one external dependency"),
    CELL(4, 1, "Scrapes every 15s, stores in its TSDB, evaluates alerts"),
    CELL(5, 1, "Reads /proc and /sys, exposes host metrics"),
    CELL(6, 1, "Queries Prometheus and Elasticsearch, draws dashboards"),
    CELL(7, 1, "Captures stdout to disk, rotates it"),
    CELL(8, 1, "Tails container logs, parses, ships"),
    CELL(9, 1, "Indexes and stores documents, enforces ILM"),
]
PLAN[307] = [
    CELL(1, 2, "`log.level` in the app's JSON lands as the document's `log.level`, not under a prefix"),
    CELL(2, 2, "The app's `@timestamp` — when the event happened — replaces Filebeat's, which is when the line was read. The difference is the shipping delay, exactly the error you do not want when correlating against a metric spike"),
    CELL(3, 2, "Losing a malformed log is how a parsing bug hides for months"),
]
PLAN[565] = [
    CELL(1, 2, "p95 rose 143× server-side; SlowRequests is the rule this fault approaches"),
    CELL(2, 2, "0.00% throughout, `up == 1` throughout. Errors-and-uptime monitoring sees nothing"),
    CELL(3, 2, "6.3 → 20.2 ms client-side, 2.5 → 3.1 ms server-side"),
    CELL(4, 2, "−31% at unchanged offered load. This is how latency becomes capacity"),
    CELL(5, 2, "120 warning documents, searchable by `fault.kind`, correlatable by id"),
    CELL(6, 2, "absent → 121 → absent, which makes it evidence rather than assertion"),
]
PLAN[269] = [
    CELL(1, 1, 'One "request served" line per response: method, route, status, duration'),
    CELL(1, 2, "The one place every request passes, so coverage cannot drift"),
    CELL(2, 1, "entry_drafted, or extraction_unavailable at warning"),
    CELL(2, 2, "Where the external dependency fails"),
    CELL(3, 1, "entry_saved, with inserted_sets and session date"),
    CELL(3, 2, "The terminal business event; pairs with `gym_sets_written_total`"),
    CELL(4, 1, "Authentication outcome, without the username"),
    CELL(4, 2, "Security signal; the username would be a disclosure"),
    CELL(5, 1, "Extraction attempts, token budget, rate-limit waits"),
    CELL(5, 2, "A slow LLM is diagnosed by the retry structure, not the outcome"),
    CELL(6, 1, "One warning per injected fault, with `fault.kind` and `fault.sequence`"),
    CELL(6, 2, "A degraded process's log stream should say why"),
]
PLAN[533] = [
    CELL(1, 1, "`FAULT_INJECTION_ENABLED` must be true. Two variables have to agree"),
    CELL(2, 1, "Refuses to arm when `APP_ENV` is production. A copied .env cannot degrade a real deployment"),
    CELL(3, 1, "One loud warning at startup, naming what is armed"),
    CELL(4, 1, "Every fault increments `gym_faults_injected_total`, so nothing rests on trusting a variable"),
    CELL(5, 1, "Clamped to `MAX_LATENCY_MS` = 10,000"),
    CELL(6, 1, "`asyncio.sleep`, never `time.sleep`, which on one async worker would measure queueing"),
]
PLAN[228] = [
    CELL(1, 1, "One JSON object per line to stdout, ECS field names"),
    CELL(2, 1, "The json-file driver wraps each line in an envelope, rotating at 10 MB × 3"),
    CELL(3, 1, "Autodiscover starts a harvester per labelled container"),
    CELL(4, 1, "`decode_json_fields` parses the JSON, promoting its keys to the event root"),
    CELL(5, 1, "Bulk-indexed into filebeat-gym-tracker-logs, ILM on the write index"),
    CELL(6, 1, "Data view over filebeat-gym-tracker-*, plus 8 saved searches"),
]
PLAN[232] = [
    CELL(1, 1, "ISO-8601, UTC, milliseconds — when it happened"),
    CELL(5, 1, "A short human sentence, not a key=value blob"),
    CELL(6, 1, "The correlation id, one per request"),
    CELL(9, 1, "Route template where known, raw path otherwise"),
    CELL(10, 1, "Integer, so range queries work"),
    CELL(12, 1, "What happened, e.g. entry_saved"),
    CELL(13, 1, "On exceptions, all three scrubbed"),
    CELL(14, 1, "Where in the code it came from"),
    CELL(15, 1, "Added by Filebeat from the Docker API"),
]
PLAN[174] = [
    CELL(1, 1, "The Docker Desktop Linux VM, not Windows"),
    CELL(2, 1, "172.28.77.1, the pinned bridge gateway"),
    CELL(3, 1, "12 cores, 7.56 GiB RAM"),
    CELL(4, 1, "/mnt/docker-desktop-disk, 944 GiB available"),
    CELL(5, 1, "Every container in the stack is a process on this kernel"),
]

# =========================================================================== #
# Pass 4 — structural cuts, where a whole block is duplicated elsewhere in the
# document rather than merely wordy.
# =========================================================================== #

# The B.2 inventory already carries a "Grafana Query" column for all 19 metrics,
# so transcribing the panel queries again per dashboard is a second copy. Keep
# the two per dashboard that the brief names specifically (error rate, p95/p99)
# and the ones with no metric of their own.
for _block in (139, 147, 148, 159, 160, 161, 162, 165, 180, 182, 186, 193, 194, 198, 202, 204):
    PLAN[_block] = D
PLAN[138] = P("Representative queries, with the full set in the dashboard JSON and in the B.2 inventory:")
PLAN[158] = D

# Figure D.1 in Part D shows the whole stack, including both of these.
for _block in range(75, 84):      # B.1 ASCII metrics diagram
    PLAN[_block] = D
for _block in range(221, 228):    # C.1 ASCII log-pipeline diagram, restated by Table C.1
    PLAN[_block] = D
PLAN[74] = P(
    "**Prometheus pulls.** Nothing in this stack pushes a metric: each target exposes `/metrics` and "
    "Prometheus fetches it on a timer, every 15 seconds. Figure D.1 in Part D shows the whole path."
)

# A.5 duplicates README.md, which is a submission item in its own right. The
# commands that matter stay; the rest points at the README.
for _block in (45, 46, 51, 52, 53, 64, 65, 66):
    PLAN[_block] = D
PLAN[43] = P(
    "One command brings up all eight services — app, Postgres, Prometheus, Node Exporter, Grafana, "
    "Elasticsearch, Kibana, Filebeat. `README.md` has the full start / use / test / clean-up walkthrough."
)
PLAN[50] = P("python scripts/load_generator.py --duration 120 --rps 5")

# Not a requirement in the brief; the SlowRequests rule it turns on is cited in
# Part E1 where it matters.
for _block in range(215, 219):
    PLAN[_block] = D

# One blank line of annotation space per box instead of three.
for _block in (70, 102, 117, 152, 169, 209, 217, 272, 342, 356, 369, 428, 466, 503, 573, 647, 665):
    PLAN[_block] = D

# The formatter listing keeps the four things the prose points at: the ECS keys,
# the contextvar request id, the scrubbed extras, and json.dumps.
for _block in (245, 246, 247, 250):
    PLAN[_block] = D

# =========================================================================== #
# Pass 5 — corrections to the passes above.
# =========================================================================== #

# Pass 4 emptied the formatter's dict literal, leaving `document = {` then `}`.
# The ECS keys are the whole point of the listing; keep a representative few.
for _block in (243, 244, 245, 246, 247, 250):
    PLAN.pop(_block, None)

# A.5 still has to tell a reader how to check the stack and import the Kibana
# saved objects, which C.6 refers to by name.
for _block in (45, 46):
    PLAN.pop(_block, None)

# =========================================================================== #
# Pass 6 — the callout boxes that are pure reflection. The ones that survive
# carry an explanation the brief asks for by name.
# =========================================================================== #
PLAN[267] = D   # contextvar vs thread-local — an aside on a choice already made
PLAN[423] = D   # "observability failures degrade observability" — a moral
PLAN[500] = D   # "the two paths meeting" — restates D.2 and D.3
PLAN[644] = D   # "enforced in the test suite" — the test is already in the table
PLAN[660] = D   # "what I take from this" — the defect table says it

# =========================================================================== #
# Pass 7 — correction. An earlier draft of B.3 and B.4 counted the metrics
# ("the six...", "the nine...") and got both counts wrong against the B.2
# inventory, which marks 10 metrics BUSINESS and 9 without. Point at the prefix
# instead of counting.
# =========================================================================== #
PLAN[105] = P(
    "These measure the machinery — whether the service is fast, up and correct. They are the metrics in B.2 "
    "that carry no BUSINESS prefix. The application dashboard is laid out on the RED method: Rate, Errors, "
    "Duration."
)
PLAN[108] = P(
    "These measure what the product is actually for: the metrics marked BUSINESS in B.2. Every one of them can "
    "move while the application metrics stay perfectly healthy, which is why they are kept apart."
)

# =========================================================================== #
# Pass 8 — the cover page claimed this document was a copy of docs/REPORT.md.
# After this transform it is not: REPORT.md is still the long version.
# =========================================================================== #
PLAN[4] = [CELL(7, 1, "docs/REPORT.md is the long-form source; this is the shortened submission copy")]
