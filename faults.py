"""Deliberate, reversible fault injection — for the Part E anomaly experiment.

The experiment needs a problem that can be turned on, measured, and turned off
again with confidence that nothing was left behind. Editing a route to add a
`sleep` would do it, but leaves the fault in the source history and in whatever
gets deployed next. This module makes the fault a piece of *configuration*
instead: nothing in the request path changes, the only difference between a
healthy run and a degraded one is an environment variable, and reverting means
unsetting it and restarting.

Two faults, matching the brief's "slow requests, failures, or another problem":

    FAULT_LATENCY_MS       hold the response back this many milliseconds
    FAULT_ERROR_EVERY_N    fail every Nth matching request with a 503

Both are sampled deterministically — every Nth request, not randomly — so a run
of 200 requests injects a known number of faults and the experiment's numbers
are reproducible rather than merely probable.

Safety
------
This is the one piece of code in the repository whose entire purpose is to break
the application, so it is built to be hard to leave on by accident:

  * Off unless FAULT_INJECTION_ENABLED is explicitly true. A latency value on
    its own does nothing — two variables have to agree before a request is ever
    delayed.
  * Refuses to arm when APP_ENV is production, whatever else is set. A copied
    .env cannot degrade a real deployment.
  * Logs a loud warning at startup, once, naming exactly what is armed, so a
    running process never hides the fact.
  * Every injected fault increments gym_faults_injected_total, so the
    experiment can prove from the metrics alone when the fault was live —
    rather than trusting that the variable was set at the time.
  * Bounded: the delay is clamped, so a mistyped value cannot hang the app.

The delay is `asyncio.sleep`, never `time.sleep`. The app is async and runs on
one worker; `time.sleep` would block the event loop and stall *every* concurrent
request, which would make the experiment measure the wrong thing entirely —
queueing everywhere instead of latency on the sampled requests.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import os
from typing import Optional

logger = logging.getLogger("gym_tracker.faults")

# A single delay is clamped to this. Long enough for a latency experiment to be
# obvious on a chart, short enough that a typo (5000 meant as 500) cannot pin a
# worker for a minute.
MAX_LATENCY_MS = 10_000.0


def _flag(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "on"}


def _number(name: str, default: float = 0.0) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning(
            "ignoring non-numeric fault setting",
            extra={"setting": name, "value_type": type(raw).__name__},
        )
        return default


class FaultInjector:
    """Reads the fault configuration once and applies it per request.

    Configuration is read at construction, not per request, so the settings
    cannot drift mid-experiment and a run is described by one set of values.
    Changing a fault means restarting the process — which is also what makes
    "remove the problem and repeat the test" an honest step rather than a
    mutation of live state.
    """

    def __init__(self, metrics: Optional[object] = None) -> None:
        self.enabled = _flag("FAULT_INJECTION_ENABLED")
        self.environment = os.getenv("APP_ENV", "development").strip().lower()

        # A production deployment is never a test bed, regardless of what else
        # the environment says.
        if self.enabled and self.environment == "production":
            logger.error(
                "fault injection refused: APP_ENV is production",
                extra={"app_env": self.environment},
            )
            self.enabled = False

        self.latency_ms = max(0.0, min(_number("FAULT_LATENCY_MS"), MAX_LATENCY_MS))
        # Delay every Nth request. 1 (the default) means every request.
        self.latency_every_n = max(1, int(_number("FAULT_LATENCY_EVERY_N", 1)))
        # 0 disables error injection entirely.
        self.error_every_n = max(0, int(_number("FAULT_ERROR_EVERY_N", 0)))
        self.error_status = int(_number("FAULT_ERROR_STATUS", 503))
        # Restrict the fault to one part of the app, e.g. "/save".
        self.path_prefix = os.getenv("FAULT_PATH_PREFIX", "").strip()

        self._counter = itertools.count(1)
        self._metrics = metrics

        if self.armed:
            # Deliberately a warning: it should be impossible to read a log
            # stream from a degraded process and not notice why.
            logger.warning(
                "FAULT INJECTION ARMED - this process is deliberately degraded",
                extra={
                    "fault.latency_ms": self.latency_ms,
                    "fault.latency_every_n": self.latency_every_n,
                    "fault.error_every_n": self.error_every_n,
                    "fault.error_status": self.error_status,
                    "fault.path_prefix": self.path_prefix or "(all paths)",
                    "app_env": self.environment,
                },
            )

    @property
    def armed(self) -> bool:
        """Whether any fault would actually fire."""
        return self.enabled and (self.latency_ms > 0 or self.error_every_n > 0)

    def _applies_to(self, path: str) -> bool:
        return not self.path_prefix or path.startswith(self.path_prefix)

    def _record(self, kind: str) -> None:
        if self._metrics is not None:
            try:
                self._metrics.faults_injected_total.labels(kind=kind).inc()
            except AttributeError:  # pragma: no cover - metrics optional
                pass

    async def before_request(self, path: str) -> Optional[int]:
        """Apply any armed fault. Returns a status code to fail with, or None.

        Called by the HTTP middleware before the route runs. Returning a status
        makes the middleware short-circuit; returning None means the request
        proceeds, possibly after a delay.
        """
        if not self.armed or not self._applies_to(path):
            return None

        sequence = next(self._counter)

        if self.error_every_n and sequence % self.error_every_n == 0:
            self._record("error")
            logger.warning(
                "injected fault: failing this request deliberately",
                extra={
                    "fault.kind": "error",
                    "fault.sequence": sequence,
                    "http.response.status_code": self.error_status,
                    "url.path": path,
                },
            )
            return self.error_status

        if self.latency_ms > 0 and sequence % self.latency_every_n == 0:
            self._record("latency")
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

        return None

    def describe(self) -> dict[str, object]:
        """Current configuration, for /healthz and the experiment scripts."""
        return {
            "armed": self.armed,
            "enabled": self.enabled,
            "app_env": self.environment,
            "latency_ms": self.latency_ms,
            "latency_every_n": self.latency_every_n,
            "error_every_n": self.error_every_n,
            "error_status": self.error_status,
            "path_prefix": self.path_prefix or None,
        }
