"""Assignment 2, Part B: the gRPC LogSet service.

A real grpc server on a free local port, the real generated stub, the real
`sessions.log_set` - and the same stateful fake database the HTTP tests use, so
"both transports apply the same rule" is checked against one implementation.
Only `auth.authenticate` is replaced, because password hashing needs a users
table the fake does not model.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import grpc
import pytest
from prometheus_client import CollectorRegistry

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "clients"))

import auth  # noqa: E402
import grpc_client  # noqa: E402
import grpc_server  # noqa: E402
import metrics as app_metrics  # noqa: E402
import workout_pb2  # noqa: E402
from live_db_fake import ACTIVE_SESSION, EXERCISE_ID, FINISHED_SESSION, FakeLiveDb  # noqa: E402

USER = SimpleNamespace(user_id=7, username="demo")


@pytest.fixture
def db():
    return FakeLiveDb()


@pytest.fixture(autouse=True)
def _isolated_root_logger(monkeypatch):
    """The server logs from its own worker threads. If an earlier test left a
    root handler on a captured stream pytest has since closed, every one of
    those lines prints a "Logging error" traceback. Start from no handlers."""
    import logging

    monkeypatch.setattr(logging.getLogger(), "handlers", [])


@pytest.fixture
def service(db, monkeypatch):
    monkeypatch.setattr(
        auth, "authenticate",
        lambda conn, username, password: USER if password == "demo-password" else None,
    )
    metrics = app_metrics.GrpcMetrics(CollectorRegistry())
    server, port = grpc_server.build_server(lambda: db, metrics, "localhost:0")
    server.start()
    yield SimpleNamespace(target=f"localhost:{port}", metrics=metrics, db=db)
    server.stop(grace=None)


def call(service, password="demo-password", request_id=None, **fields):
    fields.setdefault("session_id", ACTIVE_SESSION)
    fields.setdefault("exercise_id", EXERCISE_ID)
    request = workout_pb2.LogSetRequest(**fields)
    return grpc_client.log_set(service.target, request, "demo", password, 2.0, request_id)


def handled(service, code):
    return service.metrics.registry.get_sample_value(
        "gym_grpc_server_handled_total",
        {"grpc_service": grpc_server.SERVICE, "grpc_method": "LogSet", "grpc_code": code},
    )


class TestLogSet:
    def test_a_valid_set_is_written(self, service):
        response, code, _, _, _ = call(service, weight_kg=80, reps=8)
        assert code == grpc.StatusCode.OK
        assert response.set.reps == 8 and response.set.weight_kg == 80
        assert response.replayed is False
        assert service.db.inserts == 1

    def test_missing_reps_is_invalid_argument_with_the_shared_message(self, service):
        _, code, message, _, _ = call(service, weight_kg=80)
        assert code == grpc.StatusCode.INVALID_ARGUMENT
        assert message == "A set needs a rep count."
        assert service.db.inserts == 0

    def test_zero_reps_gets_the_same_message_as_http(self, service):
        _, code, message, _, _ = call(service, weight_kg=80, reps=0)
        assert code == grpc.StatusCode.INVALID_ARGUMENT
        assert message.startswith("Reps must be between")

    def test_an_unknown_workout_is_not_found(self, service):
        _, code, _, _, _ = call(service, session_id=999, reps=8)
        assert code == grpc.StatusCode.NOT_FOUND

    def test_a_finished_workout_is_failed_precondition(self, service):
        _, code, _, _, _ = call(service, session_id=FINISHED_SESSION, reps=8)
        assert code == grpc.StatusCode.FAILED_PRECONDITION

    def test_wrong_credentials_are_unauthenticated(self, service):
        _, code, _, _, _ = call(service, password="nope", reps=8)
        assert code == grpc.StatusCode.UNAUTHENTICATED
        assert service.db.inserts == 0

    def test_a_repeated_call_with_one_key_writes_once(self, service):
        first, *_ = call(service, reps=8, weight_kg=80, idempotency_key="k-1")
        second, code, *_ = call(service, reps=8, weight_kg=80, idempotency_key="k-1")
        assert code == grpc.StatusCode.OK
        assert second.replayed is True
        assert second.set.log_id == first.set.log_id
        assert service.db.inserts == 1


class TestTelemetry:
    def test_the_request_id_crosses_the_boundary_and_comes_back(self, service):
        _, _, _, returned, _ = call(service, reps=8, request_id="trace-abc-123")
        assert returned == "trace-abc-123"

    def test_a_hostile_request_id_is_replaced(self, service):
        _, _, _, returned, _ = call(service, reps=8, request_id="x" * 100)
        assert returned != "x" * 100 and len(returned) == 16

    def test_the_request_id_reaches_the_log_lines(self, service, caplog):
        import io
        import logging

        import logging_setup

        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(logging_setup.EcsJsonFormatter(service="gym-tracker-grpc"))
        caplog.set_level(logging.INFO)
        logging.getLogger().addHandler(handler)
        try:
            call(service, reps=8, request_id="trace-in-logs")
        finally:
            logging.getLogger().removeHandler(handler)
        lines = [line for line in stream.getvalue().splitlines() if "trace-in-logs" in line]
        assert any('"rpc served"' in line and '"rpc.grpc.status_code": "OK"' in line
                   for line in lines)
        assert any('"set logged"' in line for line in lines)

    def test_calls_are_counted_by_status_code(self, service):
        call(service, reps=8)
        call(service, reps=0)
        call(service, session_id=999, reps=8)
        assert handled(service, "OK") == 1
        assert handled(service, "INVALID_ARGUMENT") == 1
        assert handled(service, "NOT_FOUND") == 1
        assert handled(service, "INTERNAL") == 0  # zero-initialised, so it exists

    def test_duration_and_in_flight_are_recorded(self, service):
        call(service, reps=8)
        registry = service.metrics.registry
        labels = {"grpc_service": grpc_server.SERVICE, "grpc_method": "LogSet"}
        assert registry.get_sample_value("gym_grpc_server_handling_seconds_count", labels) == 1
        assert registry.get_sample_value(
            "gym_grpc_server_in_flight", {"grpc_method": "LogSet"}) == 0

    def test_live_sets_are_counted_as_grpc(self, service):
        call(service, reps=8, idempotency_key="k-2")
        call(service, reps=8, idempotency_key="k-2")
        get = service.metrics.registry.get_sample_value
        assert get("gym_live_sets_total", {"transport": "grpc", "outcome": "written"}) == 1
        assert get("gym_live_sets_total", {"transport": "grpc", "outcome": "replayed"}) == 1

    def test_no_unbounded_labels(self, service):
        forbidden = {"user_id", "user", "request_id", "session_id", "trace_id", "key"}
        for metric in service.metrics.registry.collect():
            for sample in metric.samples:
                assert not set(sample.labels) & forbidden, sample


def test_the_client_deadline_is_enforced(db, monkeypatch, caplog):
    """A server slower than the deadline: the client gives up on time."""
    import logging
    import threading

    release = threading.Event()
    served = threading.Event()

    class _Served(logging.Handler):
        def emit(self, record):
            if record.getMessage() == "rpc served":
                served.set()

    monkeypatch.setattr(
        auth, "authenticate",
        lambda conn, username, password: release.wait(5) and USER,
    )
    caplog.set_level(logging.INFO)
    logging.getLogger().addHandler(_Served())
    metrics = app_metrics.GrpcMetrics(CollectorRegistry())
    server, port = grpc_server.build_server(lambda: db, metrics, "localhost:0")
    server.start()
    try:
        request = workout_pb2.LogSetRequest(session_id=ACTIVE_SESSION, exercise_id=EXERCISE_ID,
                                            reps=8)
        _, code, _, _, elapsed = grpc_client.log_set(
            f"localhost:{port}", request, "demo", "demo-password", deadline=0.2)
    finally:
        # The handler is still running after the client gave up. Let it finish
        # and log before this test's logging setup is torn down.
        release.set()
        served.wait(5)
        server.stop(grace=None)
    assert code == grpc.StatusCode.DEADLINE_EXCEEDED
    assert elapsed < 900
