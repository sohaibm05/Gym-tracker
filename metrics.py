"""Prometheus instrumentation for the gym tracker.

Every metric the application exports is declared here and nowhere else, so the
table in the report has exactly one source of truth to be checked against.
Call sites import a name from this module; they never build a metric inline.

Four families, because the four Prometheus metric types answer four different
questions:

    Counter    how many times has this happened since the process started?
    Gauge      how many are there right now?
    Histogram  how are the durations distributed? (bucketed, so p95/p99 can be
               computed across instances in PromQL)
    Summary    what is the average of this value? (client-side sum/count only —
               the Python client does not compute quantiles, so anything
               needing p95 uses a Histogram instead)

Both halves of the assignment brief are covered. Application metrics measure the
machinery (request latency, in-flight requests, failures). Business metrics
measure the thing the product is actually for (workouts parsed, sets written to
the database, drafts a person has not confirmed yet, weekly reports generated).

Cardinality
-----------
Every label in this file has a small, bounded set of values. `route` is the
FastAPI *route template* ("/weekly-report"), never the raw request path, and an
unmatched path collapses to a single "unmatched" series. A user id, a session
date, an exercise name or a request id would each be unbounded, so none of them
is ever a label — they belong in logs, which are indexed for search rather than
stored as one time series per distinct value. `observability/cardinality_demo.py`
demonstrates what happens when that rule is broken.

Degrading without the library
-----------------------------
`prometheus_client` is a real dependency (requirements.txt), but the serverless
loaders this app also deploys to have been known to ship an incomplete bundle.
A missing metrics library must not be the reason a workout cannot be logged, so
when the import fails every name below becomes a no-op with the same interface
and the app runs on unobserved. `/metrics` then reports that it is disabled.
"""

from __future__ import annotations

import os
import time
from typing import Any, Iterable, Optional

# --------------------------------------------------------------------------
# Library import, with a no-op fallback
# --------------------------------------------------------------------------

try:
    from prometheus_client import (
        CollectorRegistry,
        Counter,
        Gauge,
        Histogram,
        Summary,
        generate_latest,
    )
    from prometheus_client import CONTENT_TYPE_LATEST as _CONTENT_TYPE_LATEST
    from prometheus_client import REGISTRY as _DEFAULT_REGISTRY

    PROMETHEUS_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised only on a broken bundle
    PROMETHEUS_AVAILABLE = False
    CollectorRegistry = None  # type: ignore[assignment]
    _DEFAULT_REGISTRY = None  # type: ignore[assignment]
    _CONTENT_TYPE_LATEST = "text/plain; version=0.0.4; charset=utf-8"

    class _NoopMetric:
        """Same surface as a prometheus_client metric, minus the recording."""

        def labels(self, *_args: Any, **_kwargs: Any) -> "_NoopMetric":
            return self

        def inc(self, *_args: Any, **_kwargs: Any) -> None:
            return None

        def dec(self, *_args: Any, **_kwargs: Any) -> None:
            return None

        def set(self, *_args: Any, **_kwargs: Any) -> None:
            return None

        def observe(self, *_args: Any, **_kwargs: Any) -> None:
            return None

    def Counter(*_args: Any, **_kwargs: Any) -> _NoopMetric:  # type: ignore[misc]
        return _NoopMetric()

    Gauge = Histogram = Summary = Counter  # type: ignore[assignment]

    def generate_latest(*_args: Any, **_kwargs: Any) -> bytes:  # type: ignore[misc]
        return b""


CONTENT_TYPE_LATEST = _CONTENT_TYPE_LATEST

# The registry everything below is attached to. The module-level default is the
# right one in the running app; tests pass their own to `build_metrics` so one
# test's counters cannot be read by the next.
REGISTRY = _DEFAULT_REGISTRY


# --------------------------------------------------------------------------
# Buckets
# --------------------------------------------------------------------------

# Web request latency. The app is a phone-facing HTML form, so the interesting
# region is 10ms-1s; the tail above that is what the Part E latency experiment
# pushes requests into, which is why the buckets keep resolution up to 10s.
HTTP_DURATION_BUCKETS = (
    0.005, 0.01, 0.025, 0.05, 0.075, 0.1, 0.25, 0.5, 0.75, 1.0, 2.5, 5.0, 10.0,
)

# One Groq extraction. A different scale entirely: a fast call is ~1s and the
# client waits out a rate limit for up to GROQ_MAX_RETRY_WAIT_SECONDS before the
# second attempt, so the upper buckets are where a degraded LLM shows up.
LLM_DURATION_BUCKETS = (0.25, 0.5, 1.0, 2.0, 3.0, 5.0, 8.0, 13.0, 21.0, 34.0, 60.0)

# A database write of a handful of rows against Postgres. Anything past a second
# here means the connection, not the query.
DB_DURATION_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0)


# --------------------------------------------------------------------------
# Label values
# --------------------------------------------------------------------------

# Every value each labelled counter is incremented with. Metrics.__init__
# creates them all at 0; a test checks the call sites use nothing else.
ENTRY_OUTCOMES = ("saved", "blocked", "duplicate_held")
DUPLICATE_DECISIONS = ("held", "overridden")
EXTRACTION_OUTCOMES = ("success", "rate_limited", "error")
NARRATION_RESULTS = ("ok", "failed")
LOGIN_RESULTS = ("success", "failure", "rate_limited")
FAULT_KINDS = ("latency", "error")


# --------------------------------------------------------------------------
# Metric definitions
# --------------------------------------------------------------------------


class Metrics:
    """Every metric the app exports, bound to one registry.

    A class rather than bare module globals so a test can build a second,
    isolated set. The running app uses the module-level singleton created just
    below, and call sites import that.
    """

    def __init__(self, registry: Any = None) -> None:
        kw = {"registry": registry} if (registry is not None and PROMETHEUS_AVAILABLE) else {}

        # ------------------------------------------------------------------
        # COUNTER - monotonic totals. "How many, ever?"
        # ------------------------------------------------------------------

        # Application: every HTTP response, split by outcome. The numerator and
        # denominator of the error rate, and the basis for requests-per-second.
        self.http_requests_total = Counter(
            "gym_http_requests_total",
            "HTTP requests served, by method, matched route template and status code.",
            ["method", "route", "status"],
            **kw,
        )

        # Application: requests that raised out of the route and became a 500.
        # Separate from status="500" above because it counts the *unhandled*
        # ones specifically — a deliberate 500 and a crash need telling apart.
        self.http_exceptions_total = Counter(
            "gym_http_exceptions_total",
            "Requests that raised an unhandled exception, by route and exception class.",
            ["route", "exception"],
            **kw,
        )

        # Business: the core act of the product. One increment per journal entry
        # that reached a terminal state at POST /save.
        #   outcome=saved            rows were written
        #   outcome=blocked          the review form refused it; nothing written
        #   outcome=duplicate_held   the duplicate guard asked before writing
        self.entries_total = Counter(
            "gym_workout_entries_total",
            "Journal entries that reached a terminal state at save time, by outcome.",
            ["outcome"],
            **kw,
        )

        # Business: individual sets written to workout_logs. Entries vary wildly
        # in size, so this is the honest measure of how much training is being
        # recorded — the metric a "sets logged per week" chart is built on.
        self.sets_written_total = Counter(
            "gym_sets_written_total",
            "Individual workout sets inserted into the database.",
            **kw,
        )

        # Business: bodyweight readings picked out of the same entries.
        self.bodyweight_written_total = Counter(
            "gym_bodyweight_entries_written_total",
            "Bodyweight readings inserted into the database.",
            **kw,
        )

        # Business: how the duplicate-decision prompt was answered. The guard
        # exists because re-pasting a corrected entry looks identical to a
        # double-tapped submit; this counts which one it actually was.
        self.duplicate_decisions_total = Counter(
            "gym_duplicate_decisions_total",
            "Answers to the duplicate-submission prompt, by decision.",
            ["decision"],
            **kw,
        )

        # Business: extraction outcomes. The LLM is the one dependency that
        # fails in interesting ways, and `outcome` says which way.
        self.llm_extractions_total = Counter(
            "gym_llm_extractions_total",
            "Groq extraction calls, by outcome (success, rate_limited, error).",
            ["outcome"],
            **kw,
        )

        # Business: weekly report generation, split by whether the narration
        # step succeeded. A failed narration still produces a correct report —
        # every number is computed in code — so this is a quality signal, not an
        # error rate.
        self.weekly_reports_total = Counter(
            "gym_weekly_reports_total",
            "Weekly reports generated, by whether LLM narration succeeded.",
            ["narration"],
            **kw,
        )

        # Application/security: authentication outcomes. A spike in `failure`
        # without a matching spike in `success` is the shape of credential
        # stuffing. No username label — that would be both unbounded and a
        # disclosure of who has an account.
        self.logins_total = Counter(
            "gym_logins_total",
            "Login attempts, by result (success, failure, rate_limited).",
            ["result"],
            **kw,
        )

        # Experiment support: faults deliberately injected by faults.py. Counted
        # so the Part E write-up can prove from the metrics alone exactly when
        # the fault was live, instead of asking the reader to trust that an
        # environment variable was set at the time.
        self.faults_injected_total = Counter(
            "gym_faults_injected_total",
            "Deliberately injected faults, by kind. Always 0 in normal operation.",
            ["kind"],
            **kw,
        )

        # Every known label value is created at 0 before anything happens. A
        # labelled series otherwise first appears when it is first incremented,
        # so Prometheus's first sample of it already reads 1 and increase() sees
        # no rise: the dashboard silently drops the first blocked save, the first
        # duplicate, the first extraction. Found on the business dashboard, which
        # showed "blocked 0" beside a save the review form had just blocked.
        for counter, values in (
            (self.entries_total, ENTRY_OUTCOMES),
            (self.duplicate_decisions_total, DUPLICATE_DECISIONS),
            (self.llm_extractions_total, EXTRACTION_OUTCOMES),
            (self.weekly_reports_total, NARRATION_RESULTS),
            (self.logins_total, LOGIN_RESULTS),
            (self.faults_injected_total, FAULT_KINDS),
        ):
            for value in values:
                counter.labels(value)

        # ------------------------------------------------------------------
        # GAUGE - a value that goes up and down. "How many right now?"
        # ------------------------------------------------------------------

        # Application: concurrent requests. On a single free-tier worker this is
        # the saturation signal — it climbs while latency climbs.
        self.http_requests_in_flight = Gauge(
            "gym_http_requests_in_flight",
            "HTTP requests currently being served.",
            **kw,
        )

        # Application: extractions waiting on Groq right now.
        self.llm_extractions_in_flight = Gauge(
            "gym_llm_extractions_in_flight",
            "Groq extraction calls currently in progress.",
            **kw,
        )

        # Business: the assignment's "orders waiting to be prepared". A parsed
        # entry sits on the review screen until the person presses save, so this
        # rises at POST /log and falls at POST /save. See `DraftTracker` below
        # for how an abandoned draft is aged out rather than counted forever.
        self.review_drafts_open = Gauge(
            "gym_review_drafts_open",
            "Parsed entries shown for review and not yet saved or abandoned.",
            **kw,
        )

        # Build/version identity, the conventional 1-valued gauge. Makes a
        # dashboard able to say which build produced a given series.
        self.build_info = Gauge(
            "gym_build_info",
            "Always 1. Labels carry the build identity of the running process.",
            ["version", "commit"],
            **kw,
        )

        # ------------------------------------------------------------------
        # HISTOGRAM - bucketed observations. "How are they distributed?"
        # These are what p95/p99 are computed from.
        # ------------------------------------------------------------------

        # Application: the headline latency metric. Bucketed rather than a
        # Summary so percentiles can be taken over any time window and
        # aggregated across processes in PromQL.
        self.http_request_duration = Histogram(
            "gym_http_request_duration_seconds",
            "Wall-clock time to serve an HTTP request, by method and route template.",
            ["method", "route"],
            buckets=HTTP_DURATION_BUCKETS,
            **kw,
        )

        # Business: how long the LLM takes. Dominates the latency of POST /log,
        # so a slow /log is diagnosed by comparing these two histograms.
        self.llm_extraction_duration = Histogram(
            "gym_llm_extraction_duration_seconds",
            "Wall-clock time for one Groq extraction, including retries.",
            buckets=LLM_DURATION_BUCKETS,
            **kw,
        )

        # Application: time inside the committing transaction at POST /save.
        # Separates "the database is slow" from "the model is slow".
        self.db_commit_duration = Histogram(
            "gym_db_commit_duration_seconds",
            "Wall-clock time to commit a reviewed draft to the database.",
            buckets=DB_DURATION_BUCKETS,
            **kw,
        )

        # ------------------------------------------------------------------
        # SUMMARY - client-side sum and count only. "What is the average?"
        # The Python client does not implement streaming quantiles, so a Summary
        # here yields _sum and _count and nothing else. That is the right tool
        # when the average is the question and the distribution is not; anything
        # needing p95 is a Histogram above.
        # ------------------------------------------------------------------

        # Business: how much text people actually paste. Average entry size
        # drives the token budget, which is what the Groq rate limit is spent
        # on — so this is a cost metric as much as a product one.
        self.entry_text_bytes = Summary(
            "gym_entry_text_bytes",
            "Size in bytes of a pasted journal entry submitted for parsing.",
            **kw,
        )

        # Business: average sets per saved entry. Rising means people are
        # logging fuller sessions; a sudden fall alongside steady entry volume
        # means extraction started dropping rows.
        self.sets_per_entry = Summary(
            "gym_sets_per_entry",
            "Number of sets written per saved journal entry.",
            **kw,
        )


# The instance the application uses.
METRICS = Metrics()


def build_metrics(registry: Any = None) -> Metrics:
    """A second, isolated metric set. For tests; the app uses METRICS."""
    return Metrics(registry=registry)


# --------------------------------------------------------------------------
# Build identity
# --------------------------------------------------------------------------


def set_build_info(version: str = "", commit: str = "") -> None:
    """Publish the build identity as gym_build_info{version,commit} = 1."""
    METRICS.build_info.labels(
        version=version or os.getenv("APP_VERSION", "dev"),
        commit=(commit or os.getenv("GIT_COMMIT", "unknown"))[:12],
    ).set(1)


# --------------------------------------------------------------------------
# Route labels
# --------------------------------------------------------------------------

# Anything that did not match a route collapses to this one series. Without it
# every 404 from a scanner ("/wp-login.php", "/.env", ...) would mint a new
# time series, which is a cardinality explosion driven by strangers.
UNMATCHED_ROUTE = "unmatched"


def route_label(request: Any) -> str:
    """The route *template* for a request, e.g. "/weekly-report".

    Starlette puts the matched `route` object into the ASGI scope during
    routing, so this is only meaningful after the handler has run. A request
    that matched nothing has no route and collapses to UNMATCHED_ROUTE.
    """
    try:
        route = request.scope.get("route")
    except AttributeError:
        return UNMATCHED_ROUTE
    path = getattr(route, "path", None) if route is not None else None
    return path if isinstance(path, str) and path else UNMATCHED_ROUTE


# --------------------------------------------------------------------------
# Open review drafts (the business gauge)
# --------------------------------------------------------------------------


class DraftTracker:
    """Counts parsed-but-unconfirmed entries without leaking upwards forever.

    A gauge is only useful if it can come back down. Saving lowers it, but a
    person who parses an entry and closes the tab never saves, and a naive
    counter would drift up until the process restarted — a gauge that only ever
    rises is worse than no gauge, because a dashboard treats it as a real
    backlog.

    So an open draft carries a timestamp and is aged out after `ttl_seconds`.
    The dict is also capped: `max_entries` bounds the memory a burst of parses
    can hold, and the oldest are dropped first. Neither the user id nor the
    session date ever becomes a metric label — they are dict keys here, inside
    the process, and the gauge publishes only the count.
    """

    def __init__(self, gauge: Any, ttl_seconds: float = 1800.0, max_entries: int = 1000) -> None:
        self._gauge = gauge
        self._ttl = ttl_seconds
        self._max = max_entries
        self._open: "dict[tuple[int, str], float]" = {}

    def _prune(self, now: float) -> None:
        cutoff = now - self._ttl
        for key in [k for k, started in self._open.items() if started < cutoff]:
            del self._open[key]
        # A burst larger than the cap drops the oldest first — they are the ones
        # closest to expiring anyway.
        if len(self._open) > self._max:
            for key, _ in sorted(self._open.items(), key=lambda kv: kv[1])[
                : len(self._open) - self._max
            ]:
                del self._open[key]

    def _publish(self) -> None:
        self._gauge.set(len(self._open))

    def opened(self, user_id: int, session_date: Any, now: Optional[float] = None) -> None:
        """A draft was rendered for review."""
        now = time.time() if now is None else now
        self._open[(user_id, str(session_date))] = now
        self._prune(now)
        self._publish()

    def closed(self, user_id: int, session_date: Any, now: Optional[float] = None) -> None:
        """A draft was saved, or abandoned in a way we can see."""
        now = time.time() if now is None else now
        self._open.pop((user_id, str(session_date)), None)
        self._prune(now)
        self._publish()

    @property
    def open_count(self) -> int:
        return len(self._open)


DRAFTS = DraftTracker(METRICS.review_drafts_open)


# --------------------------------------------------------------------------
# Timing helper
# --------------------------------------------------------------------------


class observe_duration:
    """Context manager recording elapsed seconds into a histogram.

    `prometheus_client` ships `Histogram.time()`, which does the same thing.
    This exists because several call sites need the elapsed value themselves —
    to put it in a log line — and `time()` does not hand it back.

        with observe_duration(METRICS.llm_extraction_duration) as timer:
            ...
        logger.info("done", extra={"duration_ms": timer.duration_ms})
    """

    def __init__(self, histogram: Any, labels: Optional[Iterable[Any]] = None) -> None:
        self._target = histogram.labels(*labels) if labels else histogram
        self.seconds = 0.0

    def __enter__(self) -> "observe_duration":
        self._started = time.perf_counter()
        return self

    def __exit__(self, *_exc: Any) -> bool:
        self.seconds = time.perf_counter() - self._started
        self._target.observe(self.seconds)
        return False  # never swallow the exception

    @property
    def duration_ms(self) -> float:
        return self.seconds * 1000.0


# --------------------------------------------------------------------------
# Exposition
# --------------------------------------------------------------------------


def render_latest(registry: Any = None) -> bytes:
    """The scrape body, in Prometheus text exposition format."""
    if not PROMETHEUS_AVAILABLE:
        return b"# prometheus_client is not installed; metrics are disabled\n"
    return generate_latest(registry if registry is not None else REGISTRY)
