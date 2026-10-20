"""Call WorkoutService.LogSet directly over gRPC (Assignment 2, Part B).

    python clients/grpc_client.py --session-id 12 --exercise "Bench Press (Barbell)" \\
        --weight 80 --reps 8

    python clients/grpc_client.py --session-id 12 --exercise-id 3 --reps 0     # fails

Every call has a deadline (--deadline, default 2s): past it, the client stops
waiting and gets DEADLINE_EXCEEDED, and gRPC propagates the deadline to the
server so it can tell the caller has gone. The result, or the status code and
its message, is printed with the request id the server logged it under.

Credentials go in the `authorization` metadata as HTTP Basic, the same account
as the web app (defaults: demo / demo-password, or GYM_USERNAME/GYM_PASSWORD).
"""

from __future__ import annotations

import argparse
import base64
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Optional

import grpc

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "grpc_gen"))

import workout_pb2  # noqa: E402
import workout_pb2_grpc  # noqa: E402


def build_request(args: argparse.Namespace) -> workout_pb2.LogSetRequest:
    """Only what was given is set, so the server sees "absent" as absent."""
    request = workout_pb2.LogSetRequest(
        session_id=args.session_id,
        cheat_reps=args.cheat_reps,
        is_warmup=args.warmup,
        idempotency_key=args.key or str(uuid.uuid4()),
    )
    if args.exercise_id is not None:
        request.exercise_id = args.exercise_id
    elif args.exercise:
        request.exercise_name = args.exercise
    if args.weight is not None:
        request.weight_kg = args.weight
    if args.reps is not None:
        request.reps = args.reps
    return request


def log_set(
    target: str,
    request: workout_pb2.LogSetRequest,
    username: str,
    password: str,
    deadline: float = 2.0,
    request_id: Optional[str] = None,
) -> tuple[Optional[workout_pb2.LogSetResponse], grpc.StatusCode, str, str, float]:
    """One LogSet call. Returns (response or None, code, message, request id, ms)."""
    credentials = base64.b64encode(f"{username}:{password}".encode()).decode()
    metadata = (
        ("authorization", f"Basic {credentials}"),
        ("x-request-id", request_id or uuid.uuid4().hex[:16]),
    )
    with grpc.insecure_channel(target) as channel:
        stub = workout_pb2_grpc.WorkoutServiceStub(channel)
        started = time.perf_counter()
        try:
            response, call = stub.LogSet.with_call(request, timeout=deadline, metadata=metadata)
            code, message, trailing = grpc.StatusCode.OK, "", call.trailing_metadata()
        except grpc.RpcError as error:
            response, code, message = None, error.code(), error.details() or ""
            trailing = error.trailing_metadata() or ()
        elapsed = (time.perf_counter() - started) * 1000
    returned = dict(trailing or ()).get("x-request-id", metadata[1][1])
    return response, code, message, returned, elapsed


def main() -> int:
    parser = argparse.ArgumentParser(description="Log one set over gRPC.")
    parser.add_argument("--target", default=os.getenv("GYM_GRPC_TARGET", "localhost:50051"))
    parser.add_argument("--username", default=os.getenv("GYM_USERNAME", "demo"))
    parser.add_argument("--password", default=os.getenv("GYM_PASSWORD", "demo-password"))
    parser.add_argument("--session-id", type=int, required=True)
    parser.add_argument("--exercise", help="exercise name, looked up or created")
    parser.add_argument("--exercise-id", type=int)
    parser.add_argument("--weight", type=float)
    parser.add_argument("--reps", type=int)
    parser.add_argument("--cheat-reps", type=int, default=0)
    parser.add_argument("--warmup", action="store_true")
    parser.add_argument("--key", help="Idempotency-Key; a new UUID if omitted")
    parser.add_argument("--deadline", type=float, default=2.0, help="seconds")
    parser.add_argument("--request-id")
    args = parser.parse_args()

    request = build_request(args)
    response, code, message, request_id, elapsed = log_set(
        args.target, request, args.username, args.password, args.deadline, args.request_id
    )
    print(f"status:     {code.name} ({elapsed:.0f} ms, deadline {args.deadline}s)")
    print(f"request id: {request_id}")
    if response is None:
        print(f"error:      {message}")
        return 1
    logged = response.set
    print(f"{'replayed' if response.replayed else 'written'}:    log_id {logged.log_id}, "
          f"{logged.exercise_name} set {logged.set_number}: {logged.display}")
    for record in response.records:
        print(f"record:     {record.label} {record.value:g}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
