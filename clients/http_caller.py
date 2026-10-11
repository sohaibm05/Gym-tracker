"""A service calling the Gym Tracker HTTP API: logs sets with timeout, retry and
a circuit breaker (Assignment 2, Part A).

It plays another service - a watch app's sync job, say - that pushes a few sets
into a live workout. It is not a server; it runs, makes its calls, and exits.

    python clients/http_caller.py                       6 sets, 1s apart
    python clients/http_caller.py --calls 20 --interval 1.5

Settings (all flags; defaults shown):

    --timeout 2.0         seconds for one attempt, connect + response
    --retries 2           extra attempts after the first, so 3 at most
    --backoff 0.5         first wait; doubles each retry, plus up to 25% jitter
    --failures 3          consecutive failed attempts that open the breaker
    --recovery 10         seconds the breaker stays open before one trial call

What is retried, and why that is safe
-------------------------------------
A timeout, a refused or dropped connection, and a 5xx. Never a 4xx: the server
understood and said no, and asking again gets the same no.

Retrying a POST that writes is only safe because each set carries an
Idempotency-Key, generated once per set and resent unchanged on every attempt.
A timeout does not mean the set was not written - the server may have finished
after the client gave up - and without the key the retry would write it twice.
With it, the server recognises the retry and returns the set it already wrote
(200 and `Idempotent-Replayed: true`).

Circuit breaker
---------------
CLOSED   calls go through. Each failed attempt counts; a success resets.
OPEN     after --failures in a row. Calls fail immediately without touching
         the network, for --recovery seconds - a server that is down is not
         helped by every caller hammering it, and the caller learns at once
         instead of after a full timeout-and-retry cycle.
HALF_OPEN after the recovery period, one trial call is let through. Success
         closes the breaker; failure opens it for another period.
"""

from __future__ import annotations

import argparse
import os
import random
import time
import uuid
from datetime import datetime
from typing import Any, Callable, Optional

import httpx

RETRYABLE_STATUS = {500, 502, 503, 504}


def log(message: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S.%f')[:-3]}] {message}", flush=True)


class CircuitOpen(Exception):
    """Refused by the breaker without making a request."""


class CircuitBreaker:
    CLOSED, OPEN, HALF_OPEN = "CLOSED", "OPEN", "HALF_OPEN"

    def __init__(
        self,
        failure_threshold: int = 3,
        recovery_seconds: float = 10.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.failure_threshold = failure_threshold
        self.recovery_seconds = recovery_seconds
        self.clock = clock
        self.state = self.CLOSED
        self.failures = 0
        self.opened_at = 0.0

    def before_call(self) -> None:
        """Raise CircuitOpen if no call may be made now."""
        if self.state == self.OPEN:
            waited = self.clock() - self.opened_at
            if waited < self.recovery_seconds:
                raise CircuitOpen(
                    f"circuit open, {self.recovery_seconds - waited:.1f}s until a trial call"
                )
            self.state = self.HALF_OPEN
            log("breaker HALF_OPEN: letting one trial call through")

    def record_success(self) -> None:
        if self.state != self.CLOSED:
            log("breaker CLOSED: trial call succeeded, normal service resumed")
        self.state = self.CLOSED
        self.failures = 0

    def record_failure(self) -> None:
        self.failures += 1
        if self.state == self.HALF_OPEN or self.failures >= self.failure_threshold:
            if self.state != self.OPEN:
                log(
                    f"breaker OPEN after {self.failures} consecutive failure(s): "
                    f"blocking calls for {self.recovery_seconds:g}s"
                )
            self.state = self.OPEN
            self.opened_at = self.clock()


class ReliableCaller:
    """Every request goes through one breaker, with a timeout and bounded retries."""

    def __init__(
        self,
        client: httpx.Client,
        breaker: CircuitBreaker,
        retries: int = 2,
        backoff: float = 0.5,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.client = client
        self.breaker = breaker
        self.retries = retries
        self.backoff = backoff
        self.sleep = sleep

    def request(self, label: str, method: str, path: str, **kwargs: Any) -> httpx.Response:
        """One logical call. Returns the response, or raises the last failure.

        Only for requests that are safe to repeat - reads, or writes carrying an
        Idempotency-Key. The caller is responsible for that; this cannot tell.
        """
        # One request id for every attempt of this call, so Kibana shows the
        # retries of one call together.
        headers = {"X-Request-ID": uuid.uuid4().hex[:16], **kwargs.pop("headers", {})}
        last: Optional[Exception] = None
        for attempt in range(1, self.retries + 2):
            self.breaker.before_call()  # CircuitOpen propagates: no request made
            started = time.perf_counter()
            try:
                response = self.client.request(method, path, headers=headers, **kwargs)
            except httpx.TimeoutException:
                last = TimeoutError(f"timed out after {self.client.timeout.read}s")
            except httpx.TransportError as exc:
                last = ConnectionError(f"{type(exc).__name__}: {exc}")
            else:
                elapsed = (time.perf_counter() - started) * 1000
                if response.status_code not in RETRYABLE_STATUS:
                    self.breaker.record_success()
                    log(f"{label} attempt {attempt} -> {response.status_code} "
                        f"({elapsed:.0f} ms, request id {headers['X-Request-ID']})")
                    return response
                last = RuntimeError(f"HTTP {response.status_code}")

            self.breaker.record_failure()
            if attempt > self.retries:
                log(f"{label} attempt {attempt} -> {last}; no retries left")
                break
            wait = self.backoff * (2 ** (attempt - 1)) * (1 + random.uniform(0, 0.25))
            log(f"{label} attempt {attempt} -> {last}; retrying in {wait:.2f}s")
            self.sleep(wait)
        assert last is not None
        raise last


def run(args: argparse.Namespace) -> int:
    breaker = CircuitBreaker(args.failures, args.recovery)
    client = httpx.Client(
        base_url=args.base_url,
        auth=(args.username, args.password),
        timeout=httpx.Timeout(args.timeout),
    )
    caller = ReliableCaller(client, breaker, retries=args.retries, backoff=args.backoff)
    log(
        f"settings: timeout {args.timeout}s, {args.retries} retries "
        f"(backoff {args.backoff}s doubling, +25% jitter), breaker opens after "
        f"{args.failures} consecutive failures and tries again after {args.recovery}s"
    )

    # Starting is safe to repeat: it returns the workout already in progress.
    response = caller.request("start workout", "POST", "/api/sessions", json={})
    if response.status_code >= 400:
        log(f"could not start a workout: {response.text}")
        return 1
    session_id = response.json()["session"]["session_id"]
    log(f"workout {session_id} is live")

    outcome = {"written": 0, "replayed": 0, "blocked": 0, "failed": 0, "refused": 0}
    for number in range(1, args.calls + 1):
        payload = {"exercise_name": args.exercise, "weight_kg": 60 + number * 2.5, "reps": 8}
        key = str(uuid.uuid4())  # one per set, reused by every retry of it
        label = f"set {number}"
        try:
            response = caller.request(
                label, "POST", f"/api/sessions/{session_id}/sets",
                json=payload, headers={"Idempotency-Key": key},
            )
        except CircuitOpen as exc:
            outcome["blocked"] += 1
            log(f"{label} blocked: {exc}")
        except Exception as exc:  # noqa: BLE001 - reported, then the run goes on
            outcome["failed"] += 1
            log(f"{label} failed: {exc}")
        else:
            if response.status_code >= 400:
                outcome["refused"] += 1
                log(f"{label} refused: {response.json()['error']['message']}")
            else:
                replayed = response.headers.get("Idempotent-Replayed") == "true"
                outcome["replayed" if replayed else "written"] += 1
                written = response.json()["set"]
                log(f"{label} {'replayed' if replayed else 'written'}: log_id "
                    f"{written['log_id']}, {written['display']}")
        if number < args.calls:
            time.sleep(args.interval)

    log("summary: " + ", ".join(f"{k} {v}" for k, v in outcome.items()))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--base-url", default=os.getenv("GYM_BASE_URL", "http://localhost:8000"))
    parser.add_argument("--username", default=os.getenv("GYM_USERNAME", "demo"))
    parser.add_argument("--password", default=os.getenv("GYM_PASSWORD", "demo-password"))
    parser.add_argument("--exercise", default="Bench Press (Barbell)")
    parser.add_argument("--calls", type=int, default=6)
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument("--timeout", type=float, default=2.0)
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--backoff", type=float, default=0.5)
    parser.add_argument("--failures", type=int, default=3)
    parser.add_argument("--recovery", type=float, default=10.0)
    return run(parser.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
