"""The observability layer: metrics, structured logs, and fault injection.

Instrumentation is the code least likely to be noticed when it breaks. A counter
that silently stops incrementing looks exactly like a quiet system, and a
redaction rule that stops matching looks exactly like a log with nothing
sensitive in it. Both failures are invisible until the moment you need the data,
which is the worst possible moment to discover them — so they get tests.

Four areas:

  TestMetricDefinitions   every metric exists, with the type the report claims
  TestDraftTracker        the business gauge can come back down
  TestStructuredLogging   field shape, request correlation, and redaction
  TestFaultInjection      off by default, bounded, and refuses production
  TestObservabilityRoutes /metrics and /healthz end to end
  TestCardinalityDemo     the Part E2 cap actually holds
"""

from __future__ import annotations

import json
import logging
import re
import sys
from datetime import date
from io import StringIO
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app as app_module  # noqa: E402
import faults  # noqa: E402
import logging_setup  # noqa: E402
import metrics as metrics_module  # noqa: E402

prometheus_client = pytest.importorskip("prometheus_client")
from prometheus_client import CollectorRegistry  # noqa: E402


class IsolatedMetrics:
    """A metric set on a private registry, read through the public API.

    Values are read with `registry.get_sample_value`, which is what
    prometheus_client supports, rather than by reaching into `_value` or
    `_count`. Those private attributes differ between metric types — an
    unlabelled Histogram has no `_count` at all, its count is the +Inf bucket —
    so tests written against them break on details that are not the library's
    contract.
    """

    def __init__(self):
        self.registry = CollectorRegistry()
        self.metrics = metrics_module.build_metrics(registry=self.registry)

    def __getattr__(self, name):
        return getattr(self.metrics, name)

    def value(self, name, **labels):
        """One sample, defaulting to 0.0 when it has not been created yet."""
        found = self.registry.get_sample_value(name, labels)
        return 0.0 if found is None else found

    def bucket_counts(self, name, **labels):
        """Every bucket of a histogram, ordered by upper bound."""
        bounds = []
        for metric in self.registry.collect():
            for sample in metric.samples:
                if sample.name == f"{name}_bucket" and all(
                    sample.labels.get(k) == v for k, v in labels.items()
                ):
                    bound = sample.labels["le"]
                    bounds.append((float(bound), sample.value))
        return [count for _, count in sorted(bounds)]


@pytest.fixture
def isolated():
    """A metric set on its own registry, so one test cannot read another's."""
    return IsolatedMetrics()


@pytest.fixture(autouse=True)
def restore_logging_state():
    """Put the root logger and the request id back after every test.

    `configure_logging()` removes every existing root handler and installs its
    own — that is what it is for, and it makes a test that calls it destructive
    to everything after it in the session: pytest's own capture handler is gone,
    and the next test's log output goes to a StringIO some earlier test owns.
    `set_request_id()` leaks the same way, stamping one test's id onto every
    later record.

    Autouse, so it covers tests that call these indirectly through `import app`
    as well as the ones that call them on purpose.
    """
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level
    saved_request_id = logging_setup.request_id_var.get()
    try:
        yield
    finally:
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)
        logging_setup.request_id_var.set(saved_request_id)


# --------------------------------------------------------------------------


class TestMetricDefinitions:
    """All four Prometheus types are present, and behave like their type.

    The assignment requires a Counter, a Gauge, a Histogram and a Summary. The
    report claims specific types for specific metrics; these assertions are what
    stop that table drifting away from the code.
    """

    def test_all_four_metric_types_are_used(self, isolated):
        kinds = {type(metric).__name__ for metric in vars(isolated.metrics).values()}
        assert {"Counter", "Gauge", "Histogram", "Summary"} <= kinds

    def test_counter_only_goes_up(self, isolated):
        isolated.sets_written_total.inc(3)
        isolated.sets_written_total.inc(2)
        assert isolated.value("gym_sets_written_total") == 5
        # A Counter has no decrement in the client at all — the guarantee is
        # structural, not a convention.
        assert not hasattr(isolated.sets_written_total, "dec")

    def test_gauge_goes_both_ways(self, isolated):
        isolated.http_requests_in_flight.inc()
        isolated.http_requests_in_flight.inc()
        isolated.http_requests_in_flight.dec()
        assert isolated.value("gym_http_requests_in_flight") == 1

    def test_histogram_buckets_are_cumulative(self, isolated):
        for value in (0.001, 0.2, 3.0):
            isolated.http_request_duration.labels(method="GET", route="/x").observe(value)
        counts = isolated.bucket_counts(
            "gym_http_request_duration_seconds", method="GET", route="/x"
        )
        # Each bucket counts everything at or below its bound, so the series is
        # non-decreasing. This is why histogram_quantile can interpolate.
        assert counts == sorted(counts)
        assert counts[-1] == 3  # the +Inf bucket holds every observation
        assert isolated.value(
            "gym_http_request_duration_seconds_sum", method="GET", route="/x"
        ) == pytest.approx(3.201)

    def test_summary_exposes_sum_and_count_but_no_quantiles(self, isolated):
        """The assignment's point about Python summaries, asserted.

        prometheus_client implements no streaming quantiles, so a Summary can
        answer "what is the average" and nothing else. Anything needing p95 has
        to be a Histogram — which is exactly why latency is one.
        """
        isolated.entry_text_bytes.observe(100)
        isolated.entry_text_bytes.observe(300)
        assert isolated.value("gym_entry_text_bytes_sum") == 400
        assert isolated.value("gym_entry_text_bytes_count") == 2
        # No quantile samples are exposed at all, so the average is the only
        # thing this metric can answer.
        names = {
            sample.name
            for metric in isolated.registry.collect()
            for sample in metric.samples
            if sample.name.startswith("gym_entry_text_bytes")
        }
        assert not any("quantile" in name for name in names)

    def test_every_metric_name_is_prefixed(self, isolated):
        for metric in vars(isolated.metrics).values():
            assert metric._name.startswith("gym_"), metric._name

    def test_labels_are_bounded_dimensions_only(self, isolated):
        """No metric may carry an unbounded identifier as a label.

        This is the Part E2 lesson enforced in code: a user id, request id,
        session date or exercise name would each mint a series per distinct
        value. If someone adds one later, this test is what stops it.
        """
        forbidden = {
            "user_id", "user", "request_id", "session_date", "date", "exercise",
            "exercise_name", "username", "email", "path", "url", "ip",
        }
        for metric in vars(isolated.metrics).values():
            assert not (set(metric._labelnames) & forbidden), metric._name

    @pytest.mark.parametrize("name, label, values", [
        ("gym_workout_entries_total", "outcome", metrics_module.ENTRY_OUTCOMES),
        ("gym_duplicate_decisions_total", "decision", metrics_module.DUPLICATE_DECISIONS),
        ("gym_llm_extractions_total", "outcome", metrics_module.EXTRACTION_OUTCOMES),
        ("gym_weekly_reports_total", "narration", metrics_module.NARRATION_RESULTS),
        ("gym_logins_total", "result", metrics_module.LOGIN_RESULTS),
        ("gym_faults_injected_total", "kind", metrics_module.FAULT_KINDS),
    ])
    def test_labelled_counters_start_at_zero(self, isolated, name, label, values):
        """A series that first appears already at 1 is invisible to increase().

        So each known label value must be exported at 0 before anything
        happens. `get_sample_value` returns None for a series that does not
        exist, which is what this distinguishes from 0.0.
        """
        for value in values:
            assert isolated.registry.get_sample_value(name, {label: value}) == 0.0, value

    def test_call_sites_use_only_the_known_label_values(self):
        """A new label value at a call site would start uncounted again."""
        root = Path(__file__).resolve().parent.parent
        source = "".join((root / f).read_text(encoding="utf-8")
                         for f in ("app.py", "pipeline.py", "faults.py"))
        known = {
            "outcome": set(metrics_module.ENTRY_OUTCOMES) | set(metrics_module.EXTRACTION_OUTCOMES),
            "decision": set(metrics_module.DUPLICATE_DECISIONS),
            "result": set(metrics_module.LOGIN_RESULTS),
        }
        for label, allowed in known.items():
            used = set(re.findall(rf'\.labels\(\s*{label}="([^"]+)"', source))
            assert used and used <= allowed, (label, used - allowed)
        # The two computed ones, checked by their literals.
        assert '"failed" if report["narration_error"] else "ok"' in source
        assert '"rate_limited" if last_error' in source and 'else "error"' in source
        assert {"error", "latency"} <= set(re.findall(r'self\._record\("(\w+)"\)', source))

    def test_render_latest_produces_exposition_format(self, isolated):
        registry = CollectorRegistry()
        local = metrics_module.build_metrics(registry=registry)
        local.sets_written_total.inc(7)
        body = metrics_module.render_latest(registry).decode()
        assert "# TYPE gym_sets_written_total counter" in body
        assert "gym_sets_written_total 7.0" in body


class TestObserveDuration:
    def test_records_and_exposes_elapsed(self, isolated):
        with metrics_module.observe_duration(isolated.db_commit_duration) as timer:
            pass
        assert timer.seconds >= 0
        assert timer.duration_ms == pytest.approx(timer.seconds * 1000)
        assert isolated.value("gym_db_commit_duration_seconds_sum") >= 0
        assert isolated.value("gym_db_commit_duration_seconds_count") == 1

    def test_records_even_when_the_block_raises(self, isolated):
        """A failed operation still took time, and that time is data.

        If the timer only recorded successes, a service getting slower and then
        timing out would show its latency *improving* right as it broke.
        """
        with pytest.raises(ValueError):
            with metrics_module.observe_duration(isolated.db_commit_duration):
                raise ValueError("boom")
        assert isolated.value("gym_db_commit_duration_seconds_count") == 1


class TestRouteLabel:
    """Route labels must be templates, never raw paths."""

    class _FakeRoute:
        def __init__(self, path):
            self.path = path

    class _FakeRequest:
        def __init__(self, scope):
            self.scope = scope

    def test_uses_the_matched_route_template(self):
        request = self._FakeRequest({"route": self._FakeRoute("/weekly-report")})
        assert metrics_module.route_label(request) == "/weekly-report"

    def test_unmatched_requests_collapse_to_one_series(self):
        """A 404 from a scanner must not create a new time series.

        Without this, "/wp-login.php", "/.env" and every other probe would each
        become a permanent series — a cardinality explosion driven by strangers.
        """
        assert metrics_module.route_label(self._FakeRequest({})) == "unmatched"
        assert (
            metrics_module.route_label(self._FakeRequest({"route": None}))
            == "unmatched"
        )

    def test_object_without_a_scope_does_not_raise(self):
        assert metrics_module.route_label(object()) == "unmatched"


class TestDraftTracker:
    """The business gauge must be able to come back down."""

    def test_open_and_close(self, isolated):
        tracker = metrics_module.DraftTracker(isolated.review_drafts_open)
        tracker.opened(1, date(2026, 9, 15))
        tracker.opened(2, date(2026, 9, 15))
        assert isolated.value("gym_review_drafts_open") == 2
        tracker.closed(1, date(2026, 9, 15))
        assert isolated.value("gym_review_drafts_open") == 1

    def test_reopening_the_same_draft_does_not_double_count(self, isolated):
        tracker = metrics_module.DraftTracker(isolated.review_drafts_open)
        tracker.opened(1, date(2026, 9, 15))
        tracker.opened(1, date(2026, 9, 15))
        assert isolated.value("gym_review_drafts_open") == 1

    def test_abandoned_drafts_age_out(self, isolated):
        """A gauge that only ever rises is worse than no gauge.

        Someone who parses an entry and closes the tab never saves it. Without
        expiry the gauge would climb until the process restarted, and a
        dashboard would read that as a real and growing backlog.
        """
        tracker = metrics_module.DraftTracker(isolated.review_drafts_open, ttl_seconds=60)
        tracker.opened(1, date(2026, 9, 15), now=1000.0)
        assert isolated.value("gym_review_drafts_open") == 1
        # 61 seconds later, a second draft arrives and the first has expired.
        tracker.opened(2, date(2026, 9, 15), now=1061.0)
        assert isolated.value("gym_review_drafts_open") == 1

    def test_burst_is_capped(self, isolated):
        tracker = metrics_module.DraftTracker(
            isolated.review_drafts_open, ttl_seconds=3600, max_entries=10
        )
        for user_id in range(50):
            tracker.opened(user_id, date(2026, 9, 15), now=2000.0 + user_id)
        assert tracker.open_count == 10
        assert isolated.value("gym_review_drafts_open") == 10

    def test_closing_something_never_opened_is_harmless(self, isolated):
        tracker = metrics_module.DraftTracker(isolated.review_drafts_open)
        tracker.closed(99, date(2026, 9, 15))
        assert isolated.value("gym_review_drafts_open") == 0


# --------------------------------------------------------------------------


class TestStructuredLogging:
    """JSON shape, correlation and redaction."""

    def _capture(self, emit, level="INFO"):
        buffer = StringIO()
        logging_setup.configure_logging(level=level, log_format="json", stream=buffer)
        emit(logging.getLogger("gym_tracker.test"))
        lines = [line for line in buffer.getvalue().strip().splitlines() if line]
        return [json.loads(line) for line in lines]

    def test_emits_one_json_object_per_line(self):
        """Filebeat reads newline-delimited records.

        A pretty-printed object would arrive as several unparseable lines, so
        the single-line guarantee is load-bearing, not cosmetic.
        """
        records = self._capture(lambda log: [log.info("one"), log.info("two")])
        assert len(records) == 2
        assert records[0]["message"] == "one"

    def test_carries_the_fields_the_brief_requires(self):
        """time, service, severity, message — and the request id."""
        logging_setup.set_request_id("abc123")
        record = self._capture(lambda log: log.info("hello"))[0]
        assert record["@timestamp"].endswith("Z")
        assert record["service.name"] == "gym-tracker"
        assert record["log.level"] == "info"
        assert record["message"] == "hello"
        assert record["http.request.id"] == "abc123"

    def test_extra_fields_are_promoted_to_top_level(self):
        record = self._capture(
            lambda log: log.info("saved", extra={"entry.inserted_sets": 5})
        )[0]
        # A real integer, not text inside the message — which is what makes
        # `entry.inserted_sets > 3` a valid Kibana query.
        assert record["entry.inserted_sets"] == 5

    def test_exceptions_become_structured_error_fields(self):
        def emit(log):
            try:
                raise ValueError("bad input")
            except ValueError:
                log.exception("failed")

        record = self._capture(emit)[0]
        assert record["error.type"] == "ValueError"
        assert record["error.message"] == "bad input"
        assert "ValueError" in record["error.stack_trace"]

    def test_request_ids_are_unique(self):
        assert logging_setup.new_request_id() != logging_setup.new_request_id()

    def test_unserialisable_values_do_not_break_the_log_call(self):
        """A logging failure must never take a request down with it."""
        record = self._capture(
            lambda log: log.info("dated", extra={"session.date": date(2026, 9, 15)})
        )[0]
        assert record["session.date"] == "2026-09-15"


class TestRedaction:
    """Nothing secret or personal reaches the log, by key or by shape."""

    def _capture(self, emit):
        buffer = StringIO()
        logging_setup.configure_logging(level="INFO", log_format="json", stream=buffer)
        emit(logging.getLogger("gym_tracker.test"))
        return [json.loads(line) for line in buffer.getvalue().strip().splitlines() if line]

    @pytest.mark.parametrize(
        "key", ["password", "session_token", "authorization", "api_key", "raw_text"]
    )
    def test_sensitive_keys_are_replaced(self, key):
        record = self._capture(lambda log: log.info("event", extra={key: "s3cret"}))[0]
        assert record[key] == logging_setup.REDACTED
        assert "s3cret" not in json.dumps(record)

    def test_journal_text_is_never_logged(self):
        """Health data. Its length is operational; its content is not."""
        record = self._capture(
            lambda log: log.info(
                "drafted",
                extra={"raw_text": "bench 3x5 @ 80kg, shoulder hurt", "entry.text_bytes": 31},
            )
        )[0]
        assert record["raw_text"] == logging_setup.REDACTED
        assert "shoulder" not in json.dumps(record)
        # The safe substitute survives.
        assert record["entry.text_bytes"] == 31

    @pytest.mark.parametrize(
        "text",
        [
            "key is gsk_abcdefghijklmnop1234",
            "Authorization: Bearer abcdefgh12345678",
            "connecting to postgresql://user:hunter2@db.example.com/gym",
        ],
    )
    def test_secret_shaped_values_are_scrubbed_from_free_text(self, text):
        record = self._capture(lambda log: log.info(text))[0]
        assert logging_setup.REDACTED in record["message"]
        for secret in ("gsk_abcdefghijklmnop1234", "abcdefgh12345678", "hunter2"):
            assert secret not in record["message"]

    def test_redaction_recurses_into_nested_values(self):
        record = self._capture(
            lambda log: log.info("event", extra={"ctx": {"password": "p", "ok": 1}})
        )[0]
        assert record["ctx"]["password"] == logging_setup.REDACTED
        assert record["ctx"]["ok"] == 1

    def test_exception_text_is_scrubbed_too(self):
        def emit(log):
            try:
                raise RuntimeError("failed for postgresql://u:leaked@h/db")
            except RuntimeError:
                log.exception("db error")

        record = self._capture(emit)[0]
        assert "leaked" not in json.dumps(record)


# --------------------------------------------------------------------------


class TestFaultInjection:
    """Deliberately breaking things is only acceptable if it cannot happen by
    accident, and cannot be left on unnoticed."""

    def test_disabled_by_default(self, monkeypatch):
        for name in list(vars(faults)) + [
            "FAULT_INJECTION_ENABLED", "FAULT_LATENCY_MS", "FAULT_ERROR_EVERY_N",
        ]:
            monkeypatch.delenv(name, raising=False)
        injector = faults.FaultInjector()
        assert injector.enabled is False
        assert injector.armed is False

    def test_a_latency_value_alone_does_nothing(self, monkeypatch):
        """Two variables have to agree before a request is ever delayed."""
        monkeypatch.setenv("FAULT_LATENCY_MS", "500")
        monkeypatch.delenv("FAULT_INJECTION_ENABLED", raising=False)
        assert faults.FaultInjector().armed is False

    def test_refuses_to_arm_in_production(self, monkeypatch):
        """A copied .env must not be able to degrade a real deployment."""
        monkeypatch.setenv("FAULT_INJECTION_ENABLED", "true")
        monkeypatch.setenv("FAULT_LATENCY_MS", "500")
        monkeypatch.setenv("APP_ENV", "production")
        injector = faults.FaultInjector()
        assert injector.enabled is False
        assert injector.armed is False

    def test_latency_is_clamped(self, monkeypatch):
        """A mistyped value cannot pin a worker for a minute."""
        monkeypatch.setenv("FAULT_INJECTION_ENABLED", "true")
        monkeypatch.setenv("FAULT_LATENCY_MS", "999999")
        assert faults.FaultInjector().latency_ms == faults.MAX_LATENCY_MS

    def test_non_numeric_settings_are_ignored_not_fatal(self, monkeypatch):
        monkeypatch.setenv("FAULT_INJECTION_ENABLED", "true")
        monkeypatch.setenv("FAULT_LATENCY_MS", "half a second")
        assert faults.FaultInjector().latency_ms == 0.0

    @pytest.mark.anyio
    async def test_errors_fire_on_every_nth_request(self, monkeypatch):
        monkeypatch.setenv("FAULT_INJECTION_ENABLED", "true")
        monkeypatch.setenv("FAULT_ERROR_EVERY_N", "5")
        monkeypatch.setenv("FAULT_ERROR_STATUS", "503")
        injector = faults.FaultInjector()
        outcomes = [await injector.before_request("/") for _ in range(10)]
        # Deterministic, not random: requests 5 and 10 exactly.
        assert outcomes == [None, None, None, None, 503, None, None, None, None, 503]

    @pytest.mark.anyio
    async def test_path_prefix_scopes_the_fault(self, monkeypatch):
        monkeypatch.setenv("FAULT_INJECTION_ENABLED", "true")
        monkeypatch.setenv("FAULT_ERROR_EVERY_N", "1")
        monkeypatch.setenv("FAULT_PATH_PREFIX", "/save")
        injector = faults.FaultInjector()
        assert await injector.before_request("/healthz") is None
        assert await injector.before_request("/save") == 503

    @pytest.mark.anyio
    async def test_inert_injector_never_delays(self, monkeypatch):
        monkeypatch.delenv("FAULT_INJECTION_ENABLED", raising=False)
        injector = faults.FaultInjector()
        assert await injector.before_request("/") is None

    def test_describe_reports_the_configuration(self, monkeypatch):
        monkeypatch.setenv("FAULT_INJECTION_ENABLED", "true")
        monkeypatch.setenv("FAULT_LATENCY_MS", "250")
        described = faults.FaultInjector().describe()
        assert described["armed"] is True
        assert described["latency_ms"] == 250.0


@pytest.fixture
def anyio_backend():
    return "asyncio"


# --------------------------------------------------------------------------


@pytest.fixture
def client():
    return TestClient(app_module.app, raise_server_exceptions=False)


class TestObservabilityRoutes:
    def test_metrics_endpoint_serves_exposition_format(self, client):
        response = client.get("/metrics")
        assert response.status_code == 200
        assert "text/plain" in response.headers["content-type"]
        assert "# TYPE gym_http_requests_total counter" in response.text

    def test_metrics_reports_all_four_types(self, client):
        body = client.get("/metrics").text
        for declared in (
            "# TYPE gym_http_requests_total counter",
            "# TYPE gym_http_requests_in_flight gauge",
            "# TYPE gym_http_request_duration_seconds histogram",
            "# TYPE gym_entry_text_bytes summary",
        ):
            assert declared in body

    def test_requests_are_counted_by_route_template(self, client):
        before = _counter_value(client, 'route="/healthz",status="200"')
        client.get("/healthz")
        assert _counter_value(client, 'route="/healthz",status="200"') == before + 1

    def test_unmatched_paths_share_one_series(self, client):
        client.get("/nope-one")
        client.get("/nope-two")
        body = client.get("/metrics").text
        series = [
            line for line in body.splitlines()
            if line.startswith("gym_http_requests_total{") and 'route="unmatched"' in line
        ]
        # Two different 404 paths, still exactly one series.
        assert len(series) == 1

    def test_response_carries_a_request_id(self, client):
        response = client.get("/healthz")
        assert len(response.headers.get("x-request-id", "")) == 16

    def test_an_inbound_request_id_is_reused(self, client):
        """One id follows a request across process boundaries."""
        response = client.get("/healthz", headers={"x-request-id": "upstream-123"})
        assert response.headers["x-request-id"] == "upstream-123"

    @pytest.mark.parametrize(
        "hostile",
        ["a" * 200, "has spaces", 'quote"inject', "new\nline"],
    )
    def test_a_malformed_inbound_id_is_replaced_not_sanitised(self, client, hostile):
        """The header is attacker-controlled and ends up in every log line."""
        response = client.get("/healthz", headers={"x-request-id": hostile})
        returned = response.headers["x-request-id"]
        assert returned != hostile
        assert len(returned) == 16

    def test_healthz_is_ok_when_no_fault_is_armed(self, client):
        assert client.get("/healthz").json() == {"status": "ok"}

    def test_healthz_announces_a_degraded_process(self, client, monkeypatch):
        """A degraded process that looks healthy is the worst outcome of an
        experiment somebody forgot to finish."""
        monkeypatch.setenv("FAULT_INJECTION_ENABLED", "true")
        monkeypatch.setenv("FAULT_LATENCY_MS", "500")
        monkeypatch.setattr(app_module, "FAULTS", faults.FaultInjector())
        body = client.get("/healthz").json()
        assert body["status"] == "degraded"
        assert body["fault_injection"]["latency_ms"] == 500.0

    def test_metrics_token_is_enforced_when_set(self, client, monkeypatch):
        monkeypatch.setattr(app_module, "METRICS_TOKEN", "sekrit")
        assert client.get("/metrics").status_code == 401
        assert client.get(
            "/metrics", headers={"authorization": "Bearer wrong"}
        ).status_code == 401
        assert client.get(
            "/metrics", headers={"authorization": "Bearer sekrit"}
        ).status_code == 200

    def test_metrics_can_be_disabled(self, client, monkeypatch):
        monkeypatch.setattr(app_module, "METRICS_ENABLED", False)
        assert client.get("/metrics").status_code == 404

    def test_metrics_endpoint_needs_no_session(self, client):
        """Prometheus is not a browser and has no cookie jar."""
        assert client.get("/metrics").status_code == 200
        # While an actual data page does redirect to the login form.
        assert client.get("/progress", follow_redirects=False).status_code == 303

    def test_failed_logins_are_counted(self, client, monkeypatch):
        monkeypatch.setattr(app_module, "_LOGIN_LIMITER", None)

        class _Conn:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        class _Engine:
            def begin(self):
                return _Conn()

        monkeypatch.setattr(app_module, "get_engine", lambda: _Engine())
        monkeypatch.setattr(app_module.auth, "authenticate", lambda *a, **k: None)

        before = _login_counter(client, "failure")
        client.post("/login", data={"username": "nobody", "password": "wrong"})
        assert _login_counter(client, "failure") == before + 1


def _counter_value(client, label_fragment):
    for line in client.get("/metrics").text.splitlines():
        if line.startswith("gym_http_requests_total{") and label_fragment in line:
            return float(line.rsplit(" ", 1)[1])
    return 0.0


def _login_counter(client, result):
    for line in client.get("/metrics").text.splitlines():
        if line.startswith(f'gym_logins_total{{result="{result}"}}'):
            return float(line.rsplit(" ", 1)[1])
    return 0.0


# --------------------------------------------------------------------------


class TestCardinalityDemo:
    """Part E2. The demo must stay harmless."""

    @staticmethod
    def _demo():
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "observability"))
        import cardinality_demo

        return cardinality_demo

    def test_unique_ids_are_capped_at_one_hundred(self):
        demo = self._demo()
        state = demo.DemoState()
        for _ in range(250):
            state.record()
        # The brief says stop at 100; this is what stops it.
        assert len(state.seen_ids) == demo.MAX_UNIQUE_IDS == 100
        assert state.capped is True

    def test_safe_mode_creates_no_per_request_series(self):
        demo = self._demo()
        state = demo.DemoState(safe_only=True)
        for _ in range(100):
            state.record()
        assert state.unsafe_series == 0

    def test_series_growth_is_linear_in_requests(self):
        """The whole lesson, as an assertion."""
        demo = self._demo()
        state = demo.DemoState()
        for count in (1, 10, 25, 50):
            while len(state.seen_ids) < count:
                state.record()
            assert state.unsafe_series == count
