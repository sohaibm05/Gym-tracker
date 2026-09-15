"""Cardinality explosion, demonstrated safely — Assignment Part E2.

The point
---------
Prometheus stores one time series per *unique combination of label values*. A
label whose values are drawn from a small fixed set (an HTTP method, a status
code) adds a handful of series. A label carrying an identifier — a request id, a
user id, an email, a session token — adds one series for every distinct value
that has ever been seen, forever. That is a cardinality explosion, and it is the
single most common way people take down their own monitoring.

This script makes it visible at a harmless scale. It exposes one counter twice:

    demo_requests_total{...,request_id="..."}   one new series per request
    demo_requests_safe_total{...}               one series, no matter the load

Run it, scrape it, and compare `count(demo_requests_total)` against
`count(demo_requests_safe_total)`.

Safety
------
The brief says not to try to crash Prometheus, so this is bounded by design:

  * MAX_UNIQUE_IDS caps the labelled counter at 100 distinct ids. Past the cap
    the script stops minting new ones rather than running until something
    breaks. 100 series is nothing to a Prometheus server — the lesson is the
    *shape* of the growth curve, not the damage.
  * The labelled counter can be switched off entirely (--safe-only), which is
    step 3 of the experiment: restart without the label and watch the series
    count stop growing.
  * It runs as its own process on its own port, so the real app's metrics are
    never polluted with per-request series.

Note the asymmetry the experiment exposes: *adding* the label creates series
instantly, but *removing* it does not delete them. The series already written
stay in Prometheus until they fall out of the retention window, so a cardinality
mistake keeps costing memory and disk long after the code is fixed. That is why
this belongs in review, not in incident response.

Usage
-----
    python observability/cardinality_demo.py                 # serve on :8001
    python observability/cardinality_demo.py --safe-only     # label removed
    python observability/cardinality_demo.py --drive 100     # generate traffic

    curl localhost:8001/metrics | grep -c '^demo_requests_total'
"""

from __future__ import annotations

import argparse
import os
import sys
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer

from prometheus_client import CollectorRegistry, Counter, generate_latest
from prometheus_client.exposition import CONTENT_TYPE_LATEST

# The cap. The brief says to stop at 100 unique ids; nothing here will exceed it.
MAX_UNIQUE_IDS = 100

DEFAULT_PORT = 8001

# A registry of this script's own, so nothing here can reach the application's
# metrics even if both are imported into one process.
REGISTRY = CollectorRegistry()

# THE BAD ONE. `request_id` is unbounded: every distinct value is a new series,
# each with its own samples, its own index entries and its own memory in the
# Prometheus head block. Never do this in real code.
UNSAFE = Counter(
    "demo_requests",
    "Requests, labelled by request id. DELIBERATELY WRONG - one series per request.",
    ["endpoint", "request_id"],
    registry=REGISTRY,
)

# THE RIGHT ONE. Same information about *rates*, bounded series count. The
# request id still exists — it goes in the log line, where the storage cost is
# linear in the number of events rather than quadratic in the number of label
# combinations, and where it is actually searchable.
SAFE = Counter(
    "demo_requests_safe",
    "Requests, labelled only by bounded dimensions. One series per endpoint.",
    ["endpoint"],
    registry=REGISTRY,
)


class DemoState:
    """Tracks how many unique ids have been minted, and enforces the cap."""

    def __init__(self, safe_only: bool = False, max_ids: int = MAX_UNIQUE_IDS) -> None:
        self.safe_only = safe_only
        self.max_ids = max_ids
        self.seen_ids: set[str] = set()
        self.capped = False

    def record(self, endpoint: str = "/demo") -> str:
        """Count one request. Returns the request id it was given."""
        request_id = uuid.uuid4().hex[:12]

        # The safe counter is always incremented: it is what the labelled one is
        # being compared against.
        SAFE.labels(endpoint=endpoint).inc()

        if self.safe_only:
            return request_id

        if len(self.seen_ids) >= self.max_ids:
            # Cap reached. Keep serving, keep counting on the safe counter, but
            # mint no further series. Re-using an id already seen adds no new
            # series, which is the honest way to stay under the cap.
            if not self.capped:
                print(
                    f"[cardinality-demo] cap reached: {self.max_ids} unique "
                    f"request_id series created, minting no more.",
                    file=sys.stderr,
                )
                self.capped = True
            request_id = next(iter(self.seen_ids))
        else:
            self.seen_ids.add(request_id)

        UNSAFE.labels(endpoint=endpoint, request_id=request_id).inc()

        # The id belongs HERE — in a log line — not in a metric label. This is
        # the whole lesson in one statement: logs are indexed per event, metrics
        # are stored per series.
        print(
            f'{{"message":"demo request served","request.id":"{request_id}",'
            f'"url.path":"{endpoint}"}}'
        )
        return request_id

    @property
    def unsafe_series(self) -> int:
        return 0 if self.safe_only else len(self.seen_ids)


class DemoHandler(BaseHTTPRequestHandler):
    """Two routes: /metrics to scrape, anything else counts as a request."""

    state: DemoState

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's interface
        if self.path.startswith("/metrics"):
            body = generate_latest(REGISTRY)
            self.send_response(200)
            self.send_header("Content-Type", CONTENT_TYPE_LATEST)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        request_id = self.state.record(endpoint="/demo")
        body = (
            f"request_id={request_id} unique_series={self.state.unsafe_series} "
            f"safe_only={self.state.safe_only}\n"
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args: object) -> None:
        """Silence the default stderr access log; the JSON line above is ours."""
        return


def serve(port: int, safe_only: bool, max_ids: int) -> None:
    DemoHandler.state = DemoState(safe_only=safe_only, max_ids=max_ids)
    server = HTTPServer(("0.0.0.0", port), DemoHandler)
    mode = "SAFE (no request_id label)" if safe_only else "UNSAFE (request_id label)"
    print(
        f"[cardinality-demo] listening on :{port} in {mode}; cap={max_ids} unique ids",
        file=sys.stderr,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("[cardinality-demo] stopped", file=sys.stderr)
    finally:
        server.server_close()


def drive(count: int, safe_only: bool, max_ids: int) -> None:
    """Generate `count` requests in-process and print the resulting series count.

    Used by the experiment script to produce the before/after numbers without
    needing a running server or a load generator.
    """
    state = DemoState(safe_only=safe_only, max_ids=max_ids)
    for _ in range(count):
        state.record()

    exposition = generate_latest(REGISTRY).decode()
    unsafe = sum(1 for line in exposition.splitlines() if line.startswith("demo_requests_total{"))
    safe = sum(
        1 for line in exposition.splitlines() if line.startswith("demo_requests_safe_total{")
    )
    print(
        f"[cardinality-demo] requests={count} mode={'safe' if safe_only else 'unsafe'} "
        f"demo_requests_total series={unsafe} demo_requests_safe_total series={safe}",
        file=sys.stderr,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument(
        "--safe-only",
        action="store_true",
        # Defaults from the environment so docker-compose can switch modes by
        # setting CARDINALITY_SAFE_ONLY, without overriding the container's
        # command line.
        default=os.getenv("CARDINALITY_SAFE_ONLY", "").strip().lower()
        in {"1", "true", "yes", "on"},
        help="Drop the request_id label. Step 3 of the experiment.",
    )
    parser.add_argument(
        "--max-ids",
        type=int,
        default=MAX_UNIQUE_IDS,
        help=f"Cap on unique request_id series (default {MAX_UNIQUE_IDS}).",
    )
    parser.add_argument(
        "--drive",
        type=int,
        metavar="N",
        help="Generate N requests in-process, print the series counts, and exit.",
    )
    args = parser.parse_args()

    if args.max_ids > MAX_UNIQUE_IDS:
        # The brief says stop at 100. Refuse rather than quietly obey.
        parser.error(
            f"--max-ids is capped at {MAX_UNIQUE_IDS} by design; this demo is not "
            f"meant to stress a real Prometheus server."
        )

    if args.drive is not None:
        drive(args.drive, safe_only=args.safe_only, max_ids=args.max_ids)
        return

    serve(args.port, safe_only=args.safe_only, max_ids=args.max_ids)


if __name__ == "__main__":
    main()
