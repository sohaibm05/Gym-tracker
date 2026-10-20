# How to run the observability stack

This is the step-by-step guide for running Assignment 1 on your own machine and
seeing the same things as the report
([docs/Assignment report 2.docx](docs/Assignment%20report%202.docx)). It works on
Windows, macOS and Linux.

## 1. What you need

| | |
|---|---|
| **Docker** | Docker Desktop on Windows or macOS, or Docker Engine with the Compose plugin on Linux. Start it and wait until it says *Engine running*. |
| **Memory for Docker** | About 6 GB. Docker Desktop's default is enough on a 16 GB machine; otherwise raise it in *Settings → Resources*. |
| **Disk** | About 8 GB free (roughly 5 GB of images plus data). |
| **Python 3.8+** | For the helper scripts. They use only the standard library, so there is nothing to `pip install`. |
| **Free ports** | 8000, 3000, 9090, 9200 and 5601. |
| **Windows only** | **Git Bash** (installed with Git for Windows) or WSL, for the two Part E experiment scripts. Everything else runs in PowerShell. |

A Groq API key is **not** needed. Without one, only the journal paste-and-parse
step is unavailable; the whole metrics and logging pipeline works. If you do have
a key, set `GROQ_API_KEY` in your shell or in a `.env` file before starting.

## 2. Start everything: one command

From the repository folder:

```bash
python scripts/quickstart.py        # Windows (PowerShell or cmd)
python3 scripts/quickstart.py       # macOS / Linux
```

On Windows, `py scripts/quickstart.py` also works if `python` is not on PATH.

The first run downloads the images and takes **5 to 10 minutes**. Later runs take
about a minute. The script:

1. Checks that Docker is running and the ports are free.
2. Runs `docker compose up -d --build`, starting the app, Postgres, Prometheus,
   Grafana, node-exporter, Elasticsearch, Kibana and Filebeat.
3. Waits until every service answers.
4. Creates a demo account (**demo / demo-password**) and saves four reviewed
   journal entries, one duplicate and one invalid entry. This fills the business
   dashboard without a Groq key.
5. Sends a minute of steady traffic so the charts have a curve.
6. Imports the Kibana data view and the eight saved searches.
7. Checks every piece and prints a summary like this:

```text
==> Verifying the stack end to end
    [OK]   8 containers running                   8 running
    [OK]   app exposes /metrics                   HTTP 200
    [OK]   Prometheus target gym-tracker is up    up
    [OK]   Prometheus target node-exporter is up  up
    [OK]   Prometheus target prometheus is up     up
    [OK]   no availability alerts firing          none
    [OK]   no other alerts firing                 none
    [OK]   business metrics recorded              gym_sets_written_total = 18
    [OK]   3 Grafana dashboards provisioned
    [OK]   logs indexed in Elasticsearch          ... documents
    [OK]   8 Kibana saved searches                8 found
```

The same command runs on a clean GitHub machine for every change to the stack
(the **Stack** workflow in GitHub Actions, with 30 seconds of traffic instead of
60). If its badge at the top of the README is green, this sequence passed there.

It is safe to run again at any time, for example after stopping and restarting
the stack. Metric counters live in the app's memory, so they start again at 0
when the app restarts (Part D of the report explains this). If that has
happened, the quick start saves the demo entries again so the business
dashboard is not empty. To re-check a running stack without changing anything,
run `python scripts/quickstart.py --check`.

> **Prefer the exact commands from the report?** They work too:
> `docker compose up -d --build`, then `./scripts/setup_kibana.sh` (Git Bash on
> Windows), then `python scripts/load_generator.py --duration 120 --rps 5`. The
> business dashboard then stays empty until you save some entries, for example
> with `python scripts/submit_reviewed_entries.py --username demo --password demo-password --signup`.

## 3. Where to look, part by part

| Report section | Open | What you should see |
|---|---|---|
| **Part A**: the app | <http://localhost:8000> (log in as `demo` / `demo-password`) | The journal form. *Progress* shows the charts built from the seeded entries. |
| **Part B**: metrics | <http://localhost:8000/metrics> | The 19 `gym_*` metrics from Table B.1 in Prometheus text format. |
| **Part B**: dashboards | <http://localhost:3000> (log in as `admin` / `admin`; you can skip the password change), then *Dashboards → Gym Tracker* | **Application performance** (Screenshot 1): request rate, 0% errors, p95 of a few ms. **Business metrics** (Screenshot 2): 4 entries saved, 1 held as a duplicate, 1 blocked, 18 sets, 4.5 sets per entry. **Host (node-exporter)** (Screenshot 3): CPU, memory, disk and network. |
| **Part B**: Prometheus | <http://localhost:9090> → *Status → Targets* | `gym-tracker`, `node-exporter` and `prometheus` are UP. `cardinality-demo` is DOWN, which is correct: it only runs during Part E2. |
| **Part B**: alerts | <http://localhost:9090/alerts> | All rules are inactive on a healthy stack. |
| **Part C**: logs | <http://localhost:5601> → *Discover* → *Open* → **01 – All gym-tracker logs** | JSON log lines parsed into typed fields: `log.level`, `http.request.id`, `http.response.status_code` and so on. |
| **Part C**: trace one request (Screenshot 4) | Kibana, saved search **04 – Trace one request by id** | Get an id as shown below, then search `http.request.id: "<id>"`. You get the request's app log line and uvicorn's access line. |
| **Part D**: follow a metric | Prometheus → *Graph* → `gym_sets_written_total` | 18 after the quick start (5 + 4 + 6 + 3 sets). The Business dashboard's **Sets logged (24h)** shows the same value. |
| **Part E1**: slow requests | See section 4 | p95 jumps to about 690 ms during the fault and returns to a few ms afterwards. |
| **Part E2**: cardinality | See section 4 | `count(demo_requests_total)` reaches 100, then disappears once the label is removed. |

### Assignment 2

| Part | Run / open | What you should see |
|---|---|---|
| **A**: OpenAPI | <http://localhost:8000/api/docs> → *Authorize* (Basic, `demo` / `demo-password`) → `POST /api/sessions/{session_id}/sets` → *Try it out* | A 201 with the set; the same Idempotency-Key again gives 200 and `idempotent-replayed: true`. |
| **A**: traffic and 429 | `python scripts/a2_traffic.py` | Each round: 201 / 200 replayed / 400 / 404 over HTTP and OK / OK replayed / INVALID_ARGUMENT / NOT_FOUND over gRPC. Then rebuild x5: 200, 200, 200, 429, 429 with `Retry-After`. |
| **A**: reliability | `./scripts/demo_reliability.sh` (Git Bash) | Successes, timeouts retried, `breaker OPEN`, blocked calls, `breaker HALF_OPEN`, `breaker CLOSED`. Saved to `observability/results-a2/reliability.txt`. |
| **B**: gRPC | `python clients/grpc_client.py --session-id <id> --exercise "Bench Press (Barbell)" --weight 80 --reps 8`, then again with `--reps 0` | `OK` with the set, then `INVALID_ARGUMENT` with "Reps must be between 1 and 500." |
| **B**: one id across the boundary | add `--request-id a2-demo-1` to the gRPC call, then Kibana: `http.request.id: "a2-demo-1"` | The `rpc served` and `set logged` lines from `gym-tracker-grpc`. |
| **C**: dashboard | Grafana → *Gym Tracker - HTTP vs gRPC (log a set)* | HTTP and gRPC side by side: calls by status, error ratio, p50/p95, in-flight, sets written vs replayed, the 429s. |
| **C**: target | Prometheus → *Status → Targets* | `gym-tracker-grpc` is UP. |

A Postgres volume created before Assignment 2 needs migration 004 once:
`docker compose exec -T postgres psql -U gym -d gymtracker < migrations/004_idempotent_sets.sql`.

To get a request id for Part C:

```powershell
(Invoke-WebRequest http://localhost:8000/healthz).Headers['x-request-id']   # PowerShell
```

```bash
curl -si http://localhost:8000/healthz | grep -i x-request-id               # Git Bash / macOS / Linux
```

On Windows and macOS, the **Host** dashboard measures the Linux VM that Docker
Desktop runs the containers in, not the PC itself. The report explains this in
Part B, *Node Exporter*.

## 4. The experiments (Part E)

Run these from **Git Bash** on Windows, or from any terminal on macOS or Linux,
in the repository folder. Each writes its results to `observability/results/`.

### E1: slow requests (about 8 minutes)

```bash
./scripts/experiment_anomaly.sh
```

The script runs the same load three times: normal, then with every 5th request
delayed by 500 ms, then normal again. Each stage is 2 minutes at 5 requests per
second. At the end it prints the load generator's view, measured from outside
the app, together with the three time windows. A verification run of this
repository gave:

| Stage | Requests (rate) | p50 | p95 | Errors |
|---|---|---|---|---|
| 01-baseline | 600 (4.99/s) | 4.7 ms | 7.8 ms | 0 |
| 02-fault | 416 (3.47/s) | 5.0 ms | 507.3 ms | 0 |
| 03-recovery | 600 (4.99/s) | 4.3 ms | 7.1 ms | 0 |

The report's Part E1 table uses the server-side percentiles from Prometheus. To
get them, paste each stage's end time into Prometheus (*Graph* → *Evaluation
time*) with
`histogram_quantile(0.95, sum by (le) (rate(gym_http_request_duration_seconds_bucket[2m])))`.
The same run gave the following; the report's figures are in brackets:

| Stage | p50 | p95 | p99 | Faults injected |
|---|---|---|---|---|
| Baseline | 2.6 ms (2.5) | 4.9 ms (4.8) | 8.0 ms (5.0) | 0 (0) |
| Fault | 3.1 ms (3.3) | 687.3 ms (687.5) | 737.5 ms (737.5) | 121 (118) |
| Recovery | 2.5 ms (2.6) | 4.8 ms (5.6) | 5.0 ms (20.9) | 0 (0) |

Exact numbers vary a little from machine to machine; the shape should not. The
fault stage reads about 690 ms server-side but about 500 ms client-side because
`histogram_quantile` estimates inside the 0.5–0.75 s bucket. The report explains
this under *Cause and effect on users*. While the experiment runs you can watch it:

- **Grafana → Application performance:** p95 and p99 rise for the fault stage only.
- **Prometheus:** the alert `FaultInjectionActive` fires during the fault stage.
  The query `histogram_quantile(0.95, sum by (le) (rate(gym_http_request_duration_seconds_bucket[1m])))`
  reproduces Screenshot 5.
- **Kibana:** the saved search **07 – Injected faults** shows one warning per delayed request (Screenshot 6).

The script switches the fault off again at the end. `http://localhost:8000/healthz`
should then read `{"status":"ok"}`. `FaultInjectionActive` keeps firing for
about five more minutes, because the rule looks back over a 5-minute window.
That is expected, and it clears by itself.

### E2: cardinality explosion (about 2 minutes)

```bash
./scripts/experiment_cardinality.sh --stack
```

Expected: `count(demo_requests_total)` = **100** while the `request_id` label is
present, then **none** after the demo restarts without it.
`count(demo_requests_safe_total)` stays at **1** throughout.

To reproduce the report's step-by-step staircase (Screenshot 7) by hand:

```bash
docker compose --profile experiment up -d cardinality-demo
curl http://localhost:8001/demo        # each call adds one new request id; repeat 10, 25, 50, 100 times
```

Watch `count(demo_requests_total)` in Prometheus. It stops at 100 because the
demo caps it there.

## 5. Stopping and cleaning up

```bash
docker compose --profile experiment down       # stop everything, keep the data
docker compose --profile experiment down -v    # stop and delete all data (volumes)
```

## 6. If something goes wrong

| Symptom | Cause and fix |
|---|---|
| `port is already allocated` / quick start says a port is in use | Another program is using 8000, 3000, 9090, 9200 or 5601. Stop it and run the quick start again. |
| `toomanyrequests` / `429` while pulling images | Docker Hub's anonymous pull limit. Run `docker login` (a free account is enough), or wait a while and retry. |
| Elasticsearch keeps restarting (exit code 137) | Docker does not have enough memory. Give it at least 6 GB (*Docker Desktop → Settings → Resources*). |
| Kibana says *server is not ready yet* | It takes 1–2 minutes after Elasticsearch on a first start. The quick start waits for it. |
| Grafana panels show *No data* | Look at the last 15–30 minutes (time picker, top right), or generate traffic: `python scripts/load_generator.py --duration 120 --rps 5`. |
| `$'\r': command not found` when running a `.sh` script | The file was checked out with Windows line endings. Run `git config core.autocrlf false`, then `git checkout -- scripts`. The repository's `.gitattributes` prevents this on a fresh clone. |
| `python3: command not found` in Git Bash | The scripts try `python3`, `python` and `py` in turn. If none is found, install Python, or point them at it with `PYTHON=/c/Path/To/python.exe ./scripts/...`. |
| Anything else | `docker compose ps` shows what is running. `docker compose logs <service>` (app, prometheus, grafana, elasticsearch, kibana, filebeat) shows why a service stopped. |
