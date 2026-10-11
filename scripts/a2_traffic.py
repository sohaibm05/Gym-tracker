"""Repeatable success and failure traffic for the Assignment 2 dashboard.

    python scripts/a2_traffic.py              5 rounds, then the rate-limit demo
    python scripts/a2_traffic.py --rounds 20

Each round makes the same four calls through BOTH transports, so the HTTP and
gRPC panels fill with the same mix and can be compared directly:

    a valid set                    HTTP 201        gRPC OK
    the same set, same key again   HTTP 200        gRPC OK (replayed)
    reps = 0                       HTTP 400        gRPC INVALID_ARGUMENT
    a workout that does not exist  HTTP 404        gRPC NOT_FOUND

Then POST /api/records/rebuild five times in a row: three are accepted and the
last two are refused with 429 and a Retry-After header.

Needs the stack running (python scripts/quickstart.py, or docker compose up)
and the demo account it creates.
"""

from __future__ import annotations

import argparse
import os
import sys
import uuid
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "clients"))
sys.path.insert(0, str(ROOT / "grpc_gen"))

import grpc_client  # noqa: E402
import workout_pb2  # noqa: E402

MISSING_SESSION = 2_000_000_000


def main() -> int:
    parser = argparse.ArgumentParser(description="Assignment 2 traffic generator.")
    parser.add_argument("--base-url", default=os.getenv("GYM_BASE_URL", "http://localhost:8000"))
    parser.add_argument("--grpc", default=os.getenv("GYM_GRPC_TARGET", "localhost:50051"))
    parser.add_argument("--username", default=os.getenv("GYM_USERNAME", "demo"))
    parser.add_argument("--password", default=os.getenv("GYM_PASSWORD", "demo-password"))
    parser.add_argument("--rounds", type=int, default=5)
    parser.add_argument("--exercise", default="Bench Press (Barbell)")
    args = parser.parse_args()

    http = httpx.Client(base_url=args.base_url, auth=(args.username, args.password), timeout=5)
    session = http.post("/api/sessions", json={})
    session.raise_for_status()
    session_id = session.json()["session"]["session_id"]
    print(f"workout {session_id} ({'started' if session.status_code == 201 else 'already live'})")

    def over_http(label: str, sid: int, reps: int, key: str) -> None:
        response = http.post(
            f"/api/sessions/{sid}/sets",
            json={"exercise_name": args.exercise, "weight_kg": 60, "reps": reps},
            headers={"Idempotency-Key": key},
        )
        detail = (response.json().get("error") or {}).get("message", "") if response.status_code >= 400 else (
            "replayed" if response.headers.get("Idempotent-Replayed") else "written")
        print(f"  HTTP {label:<16} {response.status_code}  {detail}")

    def over_grpc(label: str, sid: int, reps: int, key: str) -> None:
        request = workout_pb2.LogSetRequest(
            session_id=sid, exercise_name=args.exercise, weight_kg=60, reps=reps,
            idempotency_key=key,
        )
        response, code, message, _, _ = grpc_client.log_set(
            args.grpc, request, args.username, args.password)
        detail = message or ("replayed" if response and response.replayed else "written")
        print(f"  gRPC {label:<16} {code.name}  {detail}")

    for number in range(1, args.rounds + 1):
        print(f"round {number}")
        for send in (over_http, over_grpc):
            key = str(uuid.uuid4())
            send("valid", session_id, 8, key)
            send("repeat same key", session_id, 8, key)
            send("reps = 0", session_id, 0, str(uuid.uuid4()))
            send("no such workout", MISSING_SESSION, 8, str(uuid.uuid4()))

    print("rate limit: POST /api/records/rebuild x5")
    for attempt in range(1, 6):
        response = http.post("/api/records/rebuild")
        extra = (f"Retry-After: {response.headers['Retry-After']}s"
                 if response.status_code == 429 else response.text.strip())
        print(f"  call {attempt}: {response.status_code}  {extra}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
