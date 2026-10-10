"""The gRPC service: WorkoutService.LogSet (Assignment 2, Part B).

Its own process and its own container, beside the FastAPI app:

    python grpc_server.py          gRPC on :50051, Prometheus metrics on :9102

LogSet is the gRPC form of POST /api/sessions/{session_id}/sets. Both call
`sessions.log_set` with a connection from `pipeline.get_engine()` on the same
DATABASE_URL, so the validation, the ownership checks, the record detection and
the idempotency rule are one piece of code reached two ways. This server never
calls the HTTP API.

Telemetry, kept consistent with the HTTP app:

  * Logs are the same ECS JSON on stdout (logging_setup), with
    service.name = gym-tracker-grpc, so Filebeat ships them to the same index.
  * An `x-request-id` in the call metadata is reused as the log correlation id,
    exactly as the HTTP middleware reuses an inbound X-Request-ID, and it is
    returned in the trailing metadata. One id therefore follows a call from the
    client, across the gRPC boundary, into every log line it produced.
  * Metrics (metrics.GrpcMetrics) are measured at the server, around the
    handler, on their own registry and port.
"""

from __future__ import annotations

import base64
import binascii
import logging
import os
import re
import signal
import sys
import time
from concurrent import futures
from pathlib import Path
from typing import Any, Callable, Optional

import grpc

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
# The generated modules import each other by bare name ("import workout_pb2").
sys.path.insert(0, str(ROOT / "grpc_gen"))

import auth  # noqa: E402
import logging_setup  # noqa: E402
import metrics as app_metrics  # noqa: E402
import pipeline  # noqa: E402
import sessions as sessions_module  # noqa: E402
import workout_pb2  # noqa: E402
import workout_pb2_grpc  # noqa: E402

SERVICE = "gymtracker.v1.WorkoutService"
logger = logging.getLogger("gym_tracker.grpc")

# Same rule as app._inbound_request_id: bounded, and only characters safe in a
# log field. Anything else is replaced with a fresh id, not sanitised.
MAX_INBOUND_REQUEST_ID = 64
_REQUEST_ID = re.compile(r"[A-Za-z0-9._:-]+")


class RpcFailure(Exception):
    """A failure with a chosen status code and a message safe to send back."""

    def __init__(self, code: grpc.StatusCode, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def status_for(exc: Exception) -> RpcFailure:
    """The gRPC status for a data-layer error - the twin of api._fail().

    Decided by the exception class, so HTTP and gRPC agree by construction:
    SessionNotFound is 404 / NOT_FOUND, SessionConflict 409 /
    FAILED_PRECONDITION, any other SessionError 400 / INVALID_ARGUMENT.
    """
    if isinstance(exc, sessions_module.SessionNotFound):
        return RpcFailure(grpc.StatusCode.NOT_FOUND, str(exc))
    if isinstance(exc, sessions_module.SessionConflict):
        return RpcFailure(grpc.StatusCode.FAILED_PRECONDITION, str(exc))
    return RpcFailure(grpc.StatusCode.INVALID_ARGUMENT, str(exc))


def _metadata(context: grpc.ServicerContext) -> dict[str, str]:
    return {key.lower(): value for key, value in (context.invocation_metadata() or ())}


def _inbound_request_id(metadata: dict[str, str]) -> Optional[str]:
    raw = (metadata.get("x-request-id") or "").strip()
    if not raw or len(raw) > MAX_INBOUND_REQUEST_ID or not _REQUEST_ID.fullmatch(raw):
        return None
    return raw


class WorkoutService(workout_pb2_grpc.WorkoutServiceServicer):
    def __init__(self, get_engine: Callable[[], Any], metrics: app_metrics.GrpcMetrics) -> None:
        self._get_engine = get_engine
        self._metrics = metrics

    def LogSet(self, request: Any, context: grpc.ServicerContext) -> Any:  # noqa: N802
        return self._observed("LogSet", context, lambda: self._log_set(request, context))

    def _observed(
        self, method: str, context: grpc.ServicerContext, handle: Callable[[], Any]
    ) -> Any:
        """Run one call with its request id, metrics, log line and status mapping.

        The gRPC twin of the HTTP middleware in app.py: the one place per-call
        telemetry is produced, so a handler only does its job and raises.
        """
        request_id = logging_setup.set_request_id(_inbound_request_id(_metadata(context)))
        context.set_trailing_metadata((("x-request-id", request_id),))
        in_flight = self._metrics.in_flight.labels(method)
        in_flight.inc()
        started = time.perf_counter()
        failure: Optional[RpcFailure] = None
        try:
            return handle()
        except RpcFailure as exc:
            failure = exc
        except Exception:  # noqa: BLE001 - anything unplanned is INTERNAL
            logger.exception("rpc failed", extra={"rpc.method": method})
            failure = RpcFailure(grpc.StatusCode.INTERNAL, "Something went wrong on our side.")
        finally:
            code = failure.code if failure is not None else grpc.StatusCode.OK
            elapsed = time.perf_counter() - started
            in_flight.dec()
            self._metrics.handling_seconds.labels(SERVICE, method).observe(elapsed)
            self._metrics.handled_total.labels(SERVICE, method, code.name).inc()
            logger.info(
                "rpc served",
                extra={
                    "rpc.system": "grpc",
                    "rpc.service": SERVICE,
                    "rpc.method": method,
                    "rpc.grpc.status_code": code.name,
                    "event.duration_ms": round(elapsed * 1000, 1),
                },
            )
            # Worker threads are reused; the next call must not inherit this id.
            logging_setup.request_id_var.set("")
        # Reached only on failure: abort() raises, ending the call with this
        # status and message. Outside the try, so its exception is not caught
        # above and counted a second time.
        context.abort(failure.code, failure.message)

    def _authenticate(self, context: grpc.ServicerContext) -> Any:
        """The account named by Basic credentials in the `authorization` metadata."""
        header = _metadata(context).get("authorization", "")
        if not header.lower().startswith("basic "):
            raise RpcFailure(grpc.StatusCode.UNAUTHENTICATED, "Send Basic credentials.")
        try:
            decoded = base64.b64decode(header.split(" ", 1)[1], validate=True).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError, IndexError):
            raise RpcFailure(grpc.StatusCode.UNAUTHENTICATED, "Malformed credentials.") from None
        username, _, password = decoded.partition(":")
        with self._get_engine().begin() as conn:
            user = auth.authenticate(conn, username, password)
        if user is None:
            raise RpcFailure(grpc.StatusCode.UNAUTHENTICATED, "Invalid credentials.")
        return user

    def _log_set(self, request: Any, context: grpc.ServicerContext) -> Any:
        user = self._authenticate(context)
        if request.session_id <= 0:
            raise RpcFailure(grpc.StatusCode.INVALID_ARGUMENT, "session_id is required.")

        which = request.WhichOneof("exercise")
        try:
            with self._get_engine().begin() as conn:
                written, breaks = sessions_module.log_set(
                    conn,
                    user_id=user.user_id,
                    session_id=request.session_id,
                    exercise_id=request.exercise_id if which == "exercise_id" else None,
                    exercise_name=request.exercise_name if which == "exercise_name" else None,
                    weight_kg=request.weight_kg if request.HasField("weight_kg") else None,
                    reps=request.reps if request.HasField("reps") else None,
                    cheat_reps=request.cheat_reps,
                    is_warmup=request.is_warmup,
                    rpe=request.rpe if request.HasField("rpe") else None,
                    rir=request.rir if request.HasField("rir") else None,
                    idempotency_key=request.idempotency_key or None,
                )
        except sessions_module.SessionError as exc:
            raise status_for(exc) from None

        self._metrics.live_sets_total.labels(
            "grpc", "replayed" if written.replayed else "written"
        ).inc()
        return to_response(written, breaks)


def to_response(written: sessions_module.LoggedSet, breaks: list[Any]) -> Any:
    body = written.to_dict()
    logged = workout_pb2.LoggedSet(
        log_id=body["log_id"],
        exercise_id=body["exercise_id"],
        exercise_name=body["exercise_name"],
        set_number=body["set_number"],
        cheat_reps=body["cheat_reps"],
        is_warmup=body["is_warmup"],
        volume_kg=body["volume_kg"],
        display=body["display"],
        logged_at=body["logged_at"] or "",
    )
    if body["weight_kg"] is not None:
        logged.weight_kg = body["weight_kg"]
    if body["reps"] is not None:
        logged.reps = body["reps"]
    response = workout_pb2.LogSetResponse(set=logged, replayed=written.replayed)
    for item in breaks:
        record = item.to_dict()
        entry = response.records.add(
            record_type=record["record_type"], label=record["label"], value=record["value"]
        )
        if record["previous_value"] is not None:
            entry.previous_value = record["previous_value"]
    return response


def build_server(
    get_engine: Callable[[], Any],
    metrics: app_metrics.GrpcMetrics,
    address: str = "[::]:50051",
    max_workers: int = 8,
) -> tuple[grpc.Server, int]:
    """A configured, unstarted server and the port it bound."""
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=max_workers))
    workout_pb2_grpc.add_WorkoutServiceServicer_to_server(
        WorkoutService(get_engine, metrics), server
    )
    port = server.add_insecure_port(address)
    return server, port


def main() -> None:
    logging_setup.configure_logging(service="gym-tracker-grpc")
    from prometheus_client import start_http_server

    metrics = app_metrics.GrpcMetrics()
    metrics_port = int(os.getenv("GRPC_METRICS_PORT", "9102"))
    start_http_server(metrics_port, registry=metrics.registry)

    engine = pipeline.get_engine()
    port = int(os.getenv("GRPC_PORT", "50051"))
    server, bound = build_server(lambda: engine, metrics, f"[::]:{port}")
    server.start()
    logger.info(
        "grpc server started",
        extra={"event.action": "grpc_started", "server.port": bound, "metrics.port": metrics_port},
    )

    # Docker stops a container with SIGTERM. Finishing the calls in flight -
    # for up to five seconds - before exiting means a deploy does not turn a
    # set that was being written into an UNAVAILABLE for the client.
    def stop(*_args: Any) -> None:
        logger.info("grpc server stopping", extra={"event.action": "grpc_stopping"})
        server.stop(grace=5)

    signal.signal(signal.SIGTERM, stop)
    server.wait_for_termination()


if __name__ == "__main__":
    main()
