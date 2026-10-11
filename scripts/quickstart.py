#!/usr/bin/env python3
"""One command to start the whole observability stack, fill it, and check it.

Works the same on Windows (PowerShell or cmd), macOS and Linux. It needs only
Docker (Desktop or Engine, with Compose v2) and Python 3.8+, and uses nothing
outside the standard library.

    python scripts/quickstart.py              build, start, seed demo data, verify
    python scripts/quickstart.py --check      only verify a stack that is already up
    python scripts/quickstart.py --load-seconds 120

What it does, in order:

 1. Checks that Docker is running and the ports the stack publishes are free.
 2. `docker compose up -d --build`.
 3. Waits until the app, Prometheus, Grafana, Elasticsearch and Kibana answer.
 4. Creates a demo account (demo / demo-password) and saves a few reviewed
    journal entries, so the business dashboard has data without a Groq key.
 5. Generates a minute of steady traffic, so the rate and latency panels have a
    curve rather than a single point.
 6. Imports the Kibana data view and the eight saved searches.
 7. Verifies every piece end to end and prints where to look.

Running it again is safe: the build is cached, the Kibana import overwrites in
place, and the demo entries are saved again only when the app has restarted
since they were saved (its counters start at 0 again, which would leave the
business dashboard empty). `--check` waits for every service, then verifies.
"""

from __future__ import annotations

import argparse
import base64
import http.cookiejar
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import NoReturn

ROOT = Path(__file__).resolve().parent.parent

APP = "http://localhost:8000"
PROMETHEUS = "http://localhost:9090"
GRAFANA = "http://localhost:3000"
ELASTICSEARCH = "http://localhost:9200"
KIBANA = "http://localhost:5601"

DEMO_USER = os.getenv("DEMO_USERNAME", "demo")
DEMO_PASSWORD = os.getenv("DEMO_PASSWORD", "demo-password")
GRAFANA_AUTH = (os.getenv("GRAFANA_USER", "admin"), os.getenv("GRAFANA_PASSWORD", "admin"))

# Published on the host by docker-compose.yml. node-exporter's 9100 is left out:
# it runs in the Docker VM's network namespace and never binds on Windows/macOS.
PORTS = {8000: "app", 9090: "prometheus", 3000: "grafana", 9200: "elasticsearch", 5601: "kibana"}

# Every target that must be up. cardinality-demo is down unless Part E2 is running.
REQUIRED_TARGETS = ("gym-tracker", "node-exporter", "prometheus")
DASHBOARDS = (
    "Gym Tracker — Application performance",
    "Gym Tracker — Business metrics",
    "Gym Tracker — Host (node-exporter)",
)


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------
# Plain ASCII on purpose: an older Windows console code page cannot print ✔.

def step(message: str) -> None:
    print(f"\n==> {message}", flush=True)


def note(message: str) -> None:
    print(f"    {message}", flush=True)


def fail(message: str, hint: str = "") -> NoReturn:
    print(f"\nERROR: {message}", file=sys.stderr)
    if hint:
        print(f"       {hint}", file=sys.stderr)
    sys.exit(1)


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def http_get(url: str, auth: tuple[str, str] | None = None, timeout: float = 5) -> tuple[int, bytes]:
    request = urllib.request.Request(url)
    if auth:
        token = base64.b64encode(f"{auth[0]}:{auth[1]}".encode()).decode()
        request.add_header("Authorization", f"Basic {token}")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    except (urllib.error.URLError, OSError, ValueError):
        return 0, b""


def get_json(url: str, auth: tuple[str, str] | None = None) -> dict | list | None:
    status, body = http_get(url, auth)
    if status != 200:
        return None
    try:
        return json.loads(body)
    except ValueError:
        return None


def wait_until(name: str, ready, timeout: float) -> bool:
    print(f"    waiting for {name:<14}", end="", flush=True)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if ready():
            print(" ready", flush=True)
            return True
        print(".", end="", flush=True)
        time.sleep(3)
    print(" TIMED OUT", flush=True)
    return False


def es_healthy() -> bool:
    health = get_json(f"{ELASTICSEARCH}/_cluster/health")
    return bool(health) and health.get("status") in ("yellow", "green")


def kibana_available() -> bool:
    status = get_json(f"{KIBANA}/api/status")
    return bool(status) and status.get("status", {}).get("overall", {}).get("level") == "available"


def log_document_count() -> int:
    data = get_json(f"{ELASTICSEARCH}/filebeat-gym-tracker-*/_count")
    return int(data.get("count", 0)) if isinstance(data, dict) else 0


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------

def compose(*args: str, check: bool = True, capture: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["docker", "compose", *args], cwd=ROOT, check=check,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.STDOUT if capture else None,
        text=True,
    )


def preflight() -> None:
    step("Checking Docker")
    if shutil.which("docker") is None:
        fail("the `docker` command is not on PATH.",
             "Install Docker Desktop (Windows/macOS) or Docker Engine (Linux), then open a new terminal.")
    info = subprocess.run(["docker", "info", "--format", "{{.ServerVersion}}"],
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if info.returncode != 0:
        fail("Docker is installed but the engine is not running.",
             "Start Docker Desktop and wait until it reports 'Engine running', then re-run this.")
    version = subprocess.run(["docker", "compose", "version", "--short"],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if version.returncode != 0:
        fail("`docker compose` (Compose v2) is not available.",
             "Update Docker Desktop, or install the docker-compose-plugin on Linux.")
    note(f"Docker engine {info.stdout.strip()}, Compose {version.stdout.strip()}")

    # Ports only matter if this stack is not already the one holding them.
    running = compose("ps", "-q", check=False, capture=True).stdout.strip()
    if running:
        note("The stack is already running; leaving it up and checking it.")
        return
    busy = []
    for port, service in PORTS.items():
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            try:
                probe.bind(("0.0.0.0", port))
            except OSError:
                busy.append(f"{port} ({service})")
    if busy:
        fail("these ports are already in use on this machine: " + ", ".join(busy),
             "Stop whatever is using them (or another copy of this stack), then re-run.")
    note("Ports 8000, 9090, 3000, 9200 and 5601 are free.")


def start(build: bool) -> None:
    step("Starting the stack (the first run downloads about 5 GB of images; allow a few minutes)")
    args = ["up", "-d"] + (["--build"] if build else [])
    try:
        compose(*args)
    except subprocess.CalledProcessError:
        fail("`docker compose up` failed; the reason is in the output above.",
             "Docker Hub rate limits ('429 Too Many Requests') clear after a while; "
             "`docker login` raises the limit.")


def wait_for_services() -> None:
    step("Waiting for every service to answer")
    checks = [
        ("app", lambda: http_get(f"{APP}/healthz")[0] == 200, 180),
        ("prometheus", lambda: http_get(f"{PROMETHEUS}/-/ready")[0] == 200, 120),
        ("grafana", lambda: http_get(f"{GRAFANA}/api/health")[0] == 200, 120),
        # The two Elastic services are the slow ones, especially on a first start.
        ("elasticsearch", es_healthy, 300),
        ("kibana", kibana_available, 420),
    ]
    late = [name for name, ready, timeout in checks if not wait_until(name, ready, timeout)]
    if late:
        fail("not every service came up: " + ", ".join(late),
             "See why with:  docker compose logs " + " ".join(late))


def app_counter(name: str) -> float:
    """An unlabelled counter's current value, read straight from the app's /metrics."""
    status, body = http_get(f"{APP}/metrics")
    if status != 200:
        return 0.0
    for line in body.decode("utf-8", "replace").splitlines():
        if line.startswith(name + " "):
            try:
                return float(line.split()[1])
            except (IndexError, ValueError):
                return 0.0
    return 0.0


def logged_in(username: str, password: str) -> bool:
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    body = urllib.parse.urlencode({"username": username, "password": password}).encode()
    try:
        opener.open(f"{APP}/login", body, timeout=15).read()
    except (urllib.error.URLError, OSError):
        return False
    return len(jar) > 0


def seed_demo_data() -> None:
    step(f"Demo account and business data (login: {DEMO_USER} / {DEMO_PASSWORD})")
    if logged_in(DEMO_USER, DEMO_PASSWORD):
        if app_counter("gym_sets_written_total") > 0:
            note("The demo account already exists and this app process has counted its entries.")
            return
        # Counters live in the app's memory, so they start again at 0 whenever the
        # app container restarts (Part D of the report walks through exactly this).
        # The entries are still in Postgres, but the business dashboard reads the
        # counters, so it would be empty until something is saved again.
        note("The demo account exists, but the app has restarted since its entries were saved,")
        note("so the business counters are back at 0. Saving the entries again to refill the")
        note("business dashboard (this adds a second copy to the demo account's history).")
    result = subprocess.run(
        [sys.executable, "scripts/submit_reviewed_entries.py",
         "--base-url", APP, "--username", DEMO_USER, "--password", DEMO_PASSWORD, "--signup"],
        cwd=ROOT,
    )
    if result.returncode != 0:
        fail("could not create the demo account or save its entries (see above).")


def generate_load(seconds: int) -> None:
    if seconds <= 0:
        return
    step(f"Generating {seconds}s of traffic at 5 requests/second (scripts/load_generator.py)")
    subprocess.run(
        [sys.executable, "scripts/load_generator.py", "--base-url", APP,
         "--duration", str(seconds), "--rps", "5", "--label", "quickstart",
         "--username", DEMO_USER, "--password", DEMO_PASSWORD],
        cwd=ROOT, stdout=subprocess.DEVNULL, check=False,
    )
    note("Done. Prometheus scrapes every 15s, so the newest points appear within a scrape or two.")


def import_kibana_objects() -> None:
    step("Kibana data view and saved searches")
    # Kibana builds the data view's field list from documents it can see.
    if not wait_until("logs indexed", lambda: log_document_count() > 0, 120):
        fail("no log documents reached Elasticsearch.",
             "Check Filebeat:  docker compose logs filebeat")
    payload = (ROOT / "observability/kibana/saved-objects.ndjson").read_bytes()
    boundary = uuid.uuid4().hex
    body = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="file"; filename="saved-objects.ndjson"\r\n'
        "Content-Type: application/ndjson\r\n\r\n"
    ).encode() + payload + f"\r\n--{boundary}--\r\n".encode()
    request = urllib.request.Request(
        f"{KIBANA}/api/saved_objects/_import?overwrite=true", data=body, method="POST",
        headers={"kbn-xsrf": "true", "Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            result = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        fail(f"Kibana rejected the import (HTTP {exc.code}).", exc.read().decode("utf-8", "replace")[:300])
    if not result.get("success"):
        fail("Kibana reported a problem with the import.", json.dumps(result)[:300])
    note(f"Imported {result.get('successCount')} objects (1 data view + 8 saved searches).")


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------

def verify() -> bool:
    step("Verifying the stack end to end")
    # (status, what, detail) where status is "OK", "WARN" or "FAIL". A WARN is
    # worth reading but does not mean the stack is broken.
    results: list[tuple[str, str, str]] = []

    def record(ok: bool, what: str, detail: str = "", warn_only: bool = False) -> None:
        results.append(("OK" if ok else "WARN" if warn_only else "FAIL", what, detail))

    ps = compose("ps", "--format", "json", check=False, capture=True).stdout.strip()
    containers = []
    for line in ps.splitlines():
        try:
            item = json.loads(line)
        except ValueError:
            continue
        containers.extend(item if isinstance(item, list) else [item])
    unhealthy = [c.get("Name") for c in containers
                 if c.get("State") != "running" or c.get("Health") in ("unhealthy", "starting")]
    record(len(containers) >= 8 and not unhealthy, "8 containers running",
           f"{len(containers)} running" + (f"; not ready: {', '.join(unhealthy)}" if unhealthy else ""))

    status, body = http_get(f"{APP}/metrics")
    record(status == 200 and b"gym_http_requests_total" in body, "app exposes /metrics", f"HTTP {status}")

    targets = get_json(f"{PROMETHEUS}/api/v1/targets") or {}
    health = {t["labels"].get("job"): t["health"]
              for t in targets.get("data", {}).get("activeTargets", [])}
    for job in REQUIRED_TARGETS:
        record(health.get(job) == "up", f"Prometheus target {job} is up", health.get(job, "missing"))

    alerts = get_json(f"{PROMETHEUS}/api/v1/alerts") or {}
    firing = sorted({a["labels"]["alertname"] for a in alerts.get("data", {}).get("alerts", [])
                     if a.get("state") == "firing"})
    # Only the availability alerts mean the stack itself is broken. The others
    # are real signals about this machine or this process -- HostLowDisk means
    # the disk really is under 10% free, FaultInjectionActive means a Part E
    # fault is still armed -- so they are shown, not failed on.
    availability = [name for name in firing if name in ("TargetDown", "HighErrorRate")]
    record(not availability, "no availability alerts firing", ", ".join(availability) or "none")
    others = [name for name in firing if name not in availability]
    record(not others, "no other alerts firing",
           ", ".join(others) + " (see Prometheus -> Alerts)" if others else "none", warn_only=True)

    # Up to two scrape intervals: with --load-seconds 0 the seeded sets may not
    # have been scraped yet when this runs.
    value = "0"
    for _ in range(11):
        sets = get_json(f"{PROMETHEUS}/api/v1/query?query=gym_sets_written_total") or {}
        value = (sets.get("data", {}).get("result") or [{}])[0].get("value", [0, "0"])[1]
        if float(value) > 0:
            break
        time.sleep(3)
    # A WARN, not a FAIL: the counters restart at 0 with the app, so a stack that
    # was restarted after seeding is healthy but shows 0 until something is saved.
    hint = "" if float(value) > 0 else (
        " (counters restart at 0 with the app; run the quick start again to re-seed)")
    record(float(value) > 0, "business metrics recorded",
           f"gym_sets_written_total = {value}{hint}", warn_only=True)

    found = get_json(f"{GRAFANA}/api/search?type=dash-db", GRAFANA_AUTH) or []
    titles = {d.get("title") for d in found if isinstance(d, dict)}
    missing = [t for t in DASHBOARDS if t not in titles]
    record(not missing, "3 Grafana dashboards provisioned", "missing: " + ", ".join(missing) if missing else "")

    count = log_document_count()
    record(count > 0, "logs indexed in Elasticsearch", f"{count} documents")

    searches = get_json(f"{KIBANA}/api/saved_objects/_find?type=search&per_page=100") or {}
    record(searches.get("total", 0) >= 8, "8 Kibana saved searches", f"{searches.get('total', 0)} found")

    width = max(len(what) for _, what, _ in results)
    for state, what, detail in results:
        print(f"    {'[' + state + ']':<6} {what:<{width}}  {detail}")
    return all(state != "FAIL" for state, _, _ in results)


def print_guide() -> None:
    print(f"""
==> Where to look

  App            {APP}            log in as {DEMO_USER} / {DEMO_PASSWORD}
  Grafana        {GRAFANA}            admin / admin, then Dashboards -> Gym Tracker
                   - Application performance   (report Part B, Screenshot 1)
                   - Business metrics          (Screenshot 2)
                   - Host (node-exporter)      (Screenshot 3)
  Prometheus     {PROMETHEUS}            Status -> Targets, and Alerts
  Kibana         {KIBANA}            Discover -> Open -> "01 - All gym-tracker logs"
                                                 (Part C, Screenshot 4)
  Raw metrics    {APP}/metrics

  Trace one request in Kibana (Part C):
    copy the x-request-id response header from any page, e.g. in PowerShell
      (Invoke-WebRequest {APP}/healthz).Headers['x-request-id']
    or in a Unix shell
      curl -si {APP}/healthz | grep -i x-request-id
    then search   http.request.id: "<that value>"   in Discover.

  Experiments (Part E, run from Git Bash on Windows, or any macOS/Linux shell):
    ./scripts/experiment_anomaly.sh              E1, about 8 minutes
    ./scripts/experiment_cardinality.sh --stack  E2, about 2 minutes

  Stop, keeping the data:     docker compose --profile experiment down
  Stop and delete everything: docker compose --profile experiment down -v
""")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="only verify a running stack")
    parser.add_argument("--no-build", action="store_true", help="skip rebuilding the app image")
    parser.add_argument("--load-seconds", type=int, default=60,
                        help="seconds of generated traffic (0 to skip; default 60)")
    args = parser.parse_args()

    if args.check:
        # Right after `docker compose up -d`, Kibana in particular needs a minute
        # or two; checking before it answers would report a healthy stack as broken.
        wait_for_services()
    else:
        preflight()
        start(build=not args.no_build)
        wait_for_services()
        seed_demo_data()
        generate_load(args.load_seconds)
        import_kibana_objects()
    ok = verify()
    print_guide()
    if not ok:
        print("Some checks failed; the lines marked FAIL say which. "
              "A target or alert can take up to a minute to settle after a start, "
              "so `python scripts/quickstart.py --check` is worth one retry.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
