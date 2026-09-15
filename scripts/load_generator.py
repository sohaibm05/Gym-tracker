#!/usr/bin/env python3
"""Repeatable load against the gym tracker — the "run a repeatable test" step.

An experiment is only worth anything if the *same* test runs before, during and
after the fault. This generates a fixed, deterministic workload so the three
stages differ in one variable — the fault — and not in how hard they were
driven.

What it drives
--------------
Routes that work without a Groq API key, so the experiment does not depend on an
external service or spend anyone's rate limit:

    GET /healthz    unauthenticated, trivial — the latency floor
    GET /metrics    the scrape endpoint, moderate serialisation cost
    GET /           requires a session; redirects to /login when signed out
    GET /login      renders a form
    GET /progress   requires a session; the heaviest read path

Signing in first (--username/--password) exercises the authenticated routes.
Without credentials it still produces useful traffic: the redirect path is a
real request the app serves and times.

Why not a load-testing tool
---------------------------
k6 or Locust would be the professional choice for real load testing, and for
anything concurrent they still are. This is deliberately a small dependency-free
script: it runs with the standard library alone, the pacing is explicit rather
than buried in a tool's scheduler, and the whole thing can be read in one sitting
by someone marking it.

Usage
-----
    python scripts/load_generator.py --duration 120 --rps 5
    python scripts/load_generator.py --duration 120 --rps 5 --label baseline
    python scripts/load_generator.py --requests 200 --rps 10 --json results.json
"""

from __future__ import annotations

import argparse
import http.cookiejar
import json
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

DEFAULT_BASE_URL = "http://localhost:8000"

# Weighted so the mix resembles real use: a lot of cheap page views, a few
# heavier ones. The weights are integers repeated in the rotation, which keeps
# the sequence deterministic — no RNG, so two runs issue identical request
# sequences and any difference between them is the fault, not the sampling.
ROUTES = [
    ("GET", "/healthz", 3),
    ("GET", "/", 3),
    ("GET", "/login", 2),
    ("GET", "/progress", 1),
    ("GET", "/metrics", 1),
]


@dataclass
class Results:
    label: str
    base_url: str
    started: float = 0.0
    finished: float = 0.0
    latencies_ms: list[float] = field(default_factory=list)
    statuses: dict[str, int] = field(default_factory=dict)
    errors: int = 0
    per_route: dict[str, list[float]] = field(default_factory=dict)

    def record(self, route: str, status: int | str, elapsed_ms: float) -> None:
        self.latencies_ms.append(elapsed_ms)
        self.per_route.setdefault(route, []).append(elapsed_ms)
        key = str(status)
        self.statuses[key] = self.statuses.get(key, 0) + 1
        if key == "error" or (key.isdigit() and int(key) >= 500):
            self.errors += 1

    def percentile(self, p: float) -> float:
        """Nearest-rank percentile.

        Deliberately the same definition Prometheus does NOT use: Prometheus
        interpolates within a histogram bucket, this takes an actual observed
        sample. The two will not agree exactly, and the report says so rather
        than pretending the client and the server measured the same thing.
        """
        if not self.latencies_ms:
            return 0.0
        ordered = sorted(self.latencies_ms)
        index = min(len(ordered) - 1, max(0, int(round(p / 100.0 * len(ordered))) - 1))
        return ordered[index]

    def summary(self) -> dict:
        total = len(self.latencies_ms)
        duration = max(self.finished - self.started, 1e-9)
        return {
            "label": self.label,
            "base_url": self.base_url,
            "requests": total,
            "duration_s": round(duration, 2),
            "achieved_rps": round(total / duration, 2),
            "errors": self.errors,
            "error_rate": round(self.errors / total, 4) if total else 0.0,
            "latency_ms": {
                "min": round(min(self.latencies_ms), 1) if total else 0.0,
                "mean": round(statistics.fmean(self.latencies_ms), 1) if total else 0.0,
                "p50": round(self.percentile(50), 1),
                "p95": round(self.percentile(95), 1),
                "p99": round(self.percentile(99), 1),
                "max": round(max(self.latencies_ms), 1) if total else 0.0,
            },
            "statuses": dict(sorted(self.statuses.items())),
            "per_route_p95_ms": {
                route: round(sorted(values)[min(len(values) - 1, int(0.95 * len(values)))], 1)
                for route, values in sorted(self.per_route.items())
            },
        }


def build_opener() -> urllib.request.OpenerDirector:
    """An opener with a cookie jar, so a login survives across requests."""
    jar = http.cookiejar.CookieJar()
    return urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(jar),
        # Redirects are followed by default; that is what we want, since a
        # signed-out GET / is a 303 to /login and both hops are real work.
    )


def login(opener, base_url: str, username: str, password: str) -> bool:
    data = urllib.parse.urlencode({"username": username, "password": password}).encode()
    request = urllib.request.Request(f"{base_url}/login", data=data, method="POST")
    try:
        with opener.open(request, timeout=30) as response:
            # A successful login redirects to "/" and the opener follows it, so
            # a 200 on a non-/login URL is the success signal.
            ok = "/login" not in response.geturl()
            print(f"[load] login {'succeeded' if ok else 'failed'} for {username}", file=sys.stderr)
            return ok
    except urllib.error.URLError as exc:
        print(f"[load] login error: {exc}", file=sys.stderr)
        return False


def run(
    base_url: str,
    duration: float | None,
    request_count: int | None,
    rps: float,
    label: str,
    username: str = "",
    password: str = "",
    timeout: float = 30.0,
) -> Results:
    opener = build_opener()
    if username and password:
        login(opener, base_url, username, password)

    rotation = [(method, path) for method, path, weight in ROUTES for _ in range(weight)]
    results = Results(label=label, base_url=base_url)

    interval = 1.0 / rps if rps > 0 else 0.0
    results.started = time.perf_counter()
    deadline = results.started + duration if duration else None
    issued = 0

    while True:
        if deadline is not None and time.perf_counter() >= deadline:
            break
        if request_count is not None and issued >= request_count:
            break

        method, path = rotation[issued % len(rotation)]
        url = f"{base_url}{path}"
        request = urllib.request.Request(url, method=method)
        started = time.perf_counter()
        try:
            with opener.open(request, timeout=timeout) as response:
                response.read()
                status: int | str = response.status
        except urllib.error.HTTPError as exc:
            # A 4xx/5xx is a real, timed response — not a failure of the test.
            exc.read()
            status = exc.code
        except Exception:  # noqa: BLE001 - connection refused, timeout, reset
            status = "error"
        elapsed_ms = (time.perf_counter() - started) * 1000
        results.record(path, status, elapsed_ms)
        issued += 1

        # Pace to the target rate, accounting for how long the request took, so
        # a slow response does not silently lower the offered load — which would
        # make a latency fault look like it reduced traffic.
        if interval:
            slack = interval - (time.perf_counter() - started)
            if slack > 0:
                time.sleep(slack)

    results.finished = time.perf_counter()
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--duration", type=float, help="Seconds to run for.")
    parser.add_argument("--requests", type=int, help="Stop after this many requests.")
    parser.add_argument("--rps", type=float, default=5.0, help="Target requests/second.")
    parser.add_argument("--label", default="run", help="Name for this stage, e.g. baseline.")
    parser.add_argument("--username", default="", help="Log in first, to reach authed routes.")
    parser.add_argument("--password", default="")
    parser.add_argument("--json", dest="json_path", help="Write the summary to this file.")
    args = parser.parse_args()

    if args.duration is None and args.requests is None:
        args.duration = 60.0

    print(
        f"[load] {args.label}: {args.base_url} at ~{args.rps} rps "
        f"for {args.duration or str(args.requests) + ' requests'}",
        file=sys.stderr,
    )

    results = run(
        base_url=args.base_url,
        duration=args.duration,
        request_count=args.requests,
        rps=args.rps,
        label=args.label,
        username=args.username,
        password=args.password,
    )
    summary = results.summary()
    print(json.dumps(summary, indent=2))

    if args.json_path:
        with open(args.json_path, "w") as handle:
            json.dump(summary, handle, indent=2)
        print(f"[load] wrote {args.json_path}", file=sys.stderr)

    # A non-zero exit if nothing succeeded at all, so a script driving this can
    # tell "the app was slow" from "the app was not there".
    return 1 if summary["requests"] == 0 or summary["error_rate"] == 1.0 else 0


if __name__ == "__main__":
    sys.exit(main())
