"""A stateful stand-in for Postgres, just big enough for sessions.log_set.

The recording fake in test_workout_app.py answers every statement the same way
whatever came before, which is right for checking that SQL is scoped to a user
and wrong for idempotency: a replay is only a replay if the second request can
see the row the first one wrote. This keeps the inserted sets and answers the
key lookup from them, so the real `sessions.log_set` runs unmodified against it.

Shared by test_api_v2.py and test_grpc_service.py - the point of both is that
the HTTP route and the gRPC handler reach the same rule.
"""

from __future__ import annotations

import contextlib
from datetime import datetime, timezone

from sqlalchemy.exc import IntegrityError

ACTIVE_SESSION = 10
FINISHED_SESSION = 11
EXERCISE_ID = 3
EXERCISE_NAME = "Bench Press (Barbell)"


class _Result:
    def __init__(self, rows=(), scalar=None):
        self._rows = list(rows)
        self._scalar = scalar

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def scalar(self):
        return self._scalar


class FakeLiveDb:
    """Two sessions (one live, one finished), one exercise, and the sets logged."""

    def __init__(self):
        self.sets: list[dict] = []
        self.inserts = 0

    # Engine surface -------------------------------------------------------

    def begin(self):
        return _Connection(self)

    connect = begin


class _Connection:
    def __init__(self, db: FakeLiveDb):
        self.db = db

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    @contextlib.contextmanager
    def begin_nested(self):
        yield self

    def execute(self, statement, params=None):
        sql = " ".join(str(statement).split())
        params = dict(params or {})

        if "FROM workout_sessions WHERE session_id" in sql:
            if params["session_id"] == ACTIVE_SESSION:
                return _Result([(None,)])
            if params["session_id"] == FINISHED_SESSION:
                return _Result([(datetime.now(timezone.utc),)])
            return _Result([])

        if sql.startswith("SELECT name FROM exercises"):
            found = params.get("id") == EXERCISE_ID
            return _Result([(EXERCISE_NAME,)] if found else [], EXERCISE_NAME if found else None)

        if "COALESCE(MAX(set_number), 0) + 1" in sql:
            same = [
                s for s in self.db.sets
                if s["session_id"] == params["session_id"]
                and s["exercise_id"] == params["exercise_id"]
            ]
            return _Result(scalar=len(same) + 1)

        if sql.startswith("INSERT INTO workout_logs"):
            key = params.get("client_set_id")
            if key is not None and any(
                s["user_id"] == params["user_id"] and s["client_set_id"] == key
                for s in self.db.sets
            ):
                raise IntegrityError(sql, params, Exception("duplicate client_set_id"))
            self.db.inserts += 1
            row = dict(params, log_id=100 + self.db.inserts, logged_at=datetime.now(timezone.utc))
            self.db.sets.append(row)
            return _Result([(row["log_id"], row["logged_at"])])

        if "wl.client_set_id = :key" in sql:
            for s in self.db.sets:
                if s["user_id"] == params["user_id"] and s["client_set_id"] == params["key"]:
                    return _Result([(
                        s["log_id"], s["exercise_id"], EXERCISE_NAME, s["set_number"],
                        s["weight_kg"], s["reps"], s["cheat_reps"], s["is_warmup"],
                        s["is_dropset"], s["rpe"], s["rir"], s["pain_flag"], s["notes"],
                        s["logged_at"],
                    )])
            return _Result([])

        # Record lookups and upserts: no records yet, nothing to return.
        if "RETURNING" in sql:
            return _Result([(1,)], scalar=1)
        return _Result([], scalar=0)
