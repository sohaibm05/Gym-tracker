"""Assignment 2, Part A: the reworked JSON API.

Runs the real routes and the real `sessions.log_set` against the stateful fake
in live_db_fake.py, so no Postgres is needed. Covers what the brief asks to be
shown - a valid request, invalid input, and a repeated request - plus the error
format, the status codes, the rate limit and the OpenAPI document.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import api  # noqa: E402
import app as app_module  # noqa: E402
import auth  # noqa: E402
import records  # noqa: E402
import sessions as sessions_module  # noqa: E402
from live_db_fake import (  # noqa: E402
    ACTIVE_SESSION,
    EXERCISE_ID,
    FINISHED_SESSION,
    FakeLiveDb,
)

USER = SimpleNamespace(user_id=7, username="alice")
SET = {"exercise_id": EXERCISE_ID, "weight_kg": 80, "reps": 8}


@pytest.fixture
def db():
    return FakeLiveDb()


@pytest.fixture
def client(db, monkeypatch):
    """The real app, with the API wired to the fake database and a fixed user."""
    signed_in = {"user": USER}

    def require_user(_request):
        if signed_in["user"] is None:
            raise app_module.LoginRequired("/api")
        return signed_in["user"]

    previous = (api._get_engine, api._require_user, api._LOGIN_REQUIRED)
    api.configure(lambda: db, require_user, app_module.LoginRequired)
    # The refreshed session in the response needs a dozen more queries the fake
    # does not model; it is not what these tests are about.
    monkeypatch.setattr(sessions_module, "load_session", lambda *a, **k: None)
    monkeypatch.setattr(api, "REBUILD_LIMITER", auth.RateLimiter(3, 60))

    test_client = TestClient(app_module.app, raise_server_exceptions=False)
    test_client.signed_in = signed_in
    yield test_client
    api.configure(*previous)


def _live_sets(transport, outcome):
    return api.METRICS.live_sets_total.labels(transport, outcome)._value.get()


# ==========================================================================
# The user task: log a set
# ==========================================================================


class TestLogSet:
    def test_a_valid_set_is_written(self, client, db):
        before = _live_sets("http", "written")
        response = client.post(f"/api/sessions/{ACTIVE_SESSION}/sets", json=SET)

        assert response.status_code == 201
        body = response.json()
        assert body["set"]["reps"] == 8 and body["set"]["weight_kg"] == 80.0
        assert body["set"]["set_number"] == 1
        assert db.inserts == 1
        assert _live_sets("http", "written") == before + 1

    def test_invalid_input_is_400_in_the_shared_error_shape(self, client, db):
        response = client.post(
            f"/api/sessions/{ACTIVE_SESSION}/sets", json={**SET, "reps": "eight"}
        )

        assert response.status_code == 400
        error = response.json()["error"]
        assert error["code"] == "invalid_input"
        assert error["message"].startswith("reps:")
        # The id in the body is the id in the header and in every log line.
        assert error["request_id"] == response.headers["x-request-id"]
        assert db.inserts == 0

    def test_a_business_rule_violation_is_also_400(self, client, db):
        """Reps 0 is well-typed and wrong. The shared rule says so, not FastAPI."""
        response = client.post(f"/api/sessions/{ACTIVE_SESSION}/sets", json={**SET, "reps": 0})
        assert response.status_code == 400
        assert "Reps must be between" in response.json()["error"]["message"]
        assert db.inserts == 0

    def test_a_repeated_request_writes_the_set_once(self, client, db):
        headers = {"Idempotency-Key": "4b0d7c2e-5e1f-4a8e-9d55-0c3f2b9a1e77"}
        replays_before = _live_sets("http", "replayed")

        first = client.post(f"/api/sessions/{ACTIVE_SESSION}/sets", json=SET, headers=headers)
        second = client.post(f"/api/sessions/{ACTIVE_SESSION}/sets", json=SET, headers=headers)

        assert first.status_code == 201
        assert "idempotent-replayed" not in first.headers
        assert second.status_code == 200
        assert second.headers["idempotent-replayed"] == "true"
        assert second.json()["set"]["log_id"] == first.json()["set"]["log_id"]
        # A replay announces nothing: the PR was already shown the first time.
        assert second.json()["records"] == []
        assert db.inserts == 1
        assert _live_sets("http", "replayed") == replays_before + 1

    def test_two_copies_racing_past_the_lookup_still_write_once(self, db, monkeypatch):
        """The original (timed out on the client, still running) and its retry.

        Both checked for the key before either inserted, so both reach the
        INSERT. The unique index refuses the second, and it returns the first.
        """
        with db.begin() as conn:
            sessions_module.log_set(conn, USER.user_id, ACTIVE_SESSION, exercise_id=EXERCISE_ID,
                                    reps=8, weight_kg=80, idempotency_key="k1")
        real_lookup = sessions_module.find_set_by_key
        lookups = iter([None])  # the loser's pre-insert lookup saw nothing yet
        monkeypatch.setattr(sessions_module, "find_set_by_key",
                            lambda *a: next(lookups, None) or real_lookup(*a))

        with db.begin() as conn:
            written, breaks = sessions_module.log_set(
                conn, USER.user_id, ACTIVE_SESSION, exercise_id=EXERCISE_ID,
                reps=8, weight_kg=80, idempotency_key="k1")

        assert written.replayed and breaks == []
        assert db.inserts == 1

    def test_without_a_key_a_repeat_is_a_second_set(self, client, db):
        """The documented behaviour for a client that sends no key."""
        client.post(f"/api/sessions/{ACTIVE_SESSION}/sets", json=SET)
        again = client.post(f"/api/sessions/{ACTIVE_SESSION}/sets", json=SET)
        assert again.json()["set"]["set_number"] == 2
        assert db.inserts == 2

    def test_keys_are_per_account(self, client, db):
        headers = {"Idempotency-Key": "same-key"}
        client.post(f"/api/sessions/{ACTIVE_SESSION}/sets", json=SET, headers=headers)
        client.signed_in["user"] = SimpleNamespace(user_id=8, username="bob")
        other = client.post(f"/api/sessions/{ACTIVE_SESSION}/sets", json=SET, headers=headers)
        assert other.status_code == 201
        assert db.inserts == 2

    @pytest.mark.parametrize("key", ["has space", "a" * 65, "semi;colon"])
    def test_a_malformed_key_is_refused(self, client, db, key):
        response = client.post(
            f"/api/sessions/{ACTIVE_SESSION}/sets", json=SET, headers={"Idempotency-Key": key}
        )
        assert response.status_code == 400
        assert db.inserts == 0

    def test_an_unknown_workout_is_404(self, client):
        response = client.post("/api/sessions/999/sets", json=SET)
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "not_found"

    def test_a_finished_workout_is_409(self, client):
        response = client.post(f"/api/sessions/{FINISHED_SESSION}/sets", json=SET)
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "conflict"

    def test_the_old_path_still_works(self, client, db):
        """A phone on last week's app.js must still drain its offline queue."""
        response = client.post(f"/api/session/{ACTIVE_SESSION}/sets", json=SET)
        assert response.status_code == 201
        assert db.inserts == 1

    def test_signed_out_is_401_not_a_redirect(self, client):
        client.signed_in["user"] = None
        response = client.post(f"/api/sessions/{ACTIVE_SESSION}/sets", json=SET)
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "unauthenticated"


class TestErrorFormat:
    def test_an_unknown_api_route_answers_json(self, client):
        response = client.get("/api/no-such-thing")
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "not_found"

    def test_pages_outside_the_api_are_untouched(self, client):
        response = client.get("/no-such-page")
        assert response.status_code == 404
        assert "error" not in response.json()

    def test_a_non_integer_id_in_a_list_is_400_not_500(self, client):
        response = client.post("/api/routines/reorder", json={"routine_ids": ["x"]})
        assert response.status_code == 400

    def test_an_empty_patch_is_refused(self, client):
        response = client.patch("/api/sets/5", json={})
        assert response.status_code == 400


@pytest.mark.skipif(
    not __import__("os").getenv("TEST_DATABASE_URL"),
    reason="set TEST_DATABASE_URL to run the tests that need Postgres",
)
class TestIdempotencyAgainstPostgres:
    """The unique index and the SAVEPOINT are Postgres behaviour; prove them there."""

    @pytest.fixture
    def engine(self):
        import os

        import sqlalchemy as sa

        eng = sa.create_engine(os.environ["TEST_DATABASE_URL"], future=True)
        with eng.begin() as conn:
            conn.execute(sa.text(
                "TRUNCATE personal_records, measurements, workout_logs, workout_sessions, "
                "routine_exercises, routines, exercises, users RESTART IDENTITY CASCADE"))
            conn.execute(sa.text(
                "INSERT INTO users (username, display_name, password_hash) VALUES ('a','A','x')"))
        return eng

    def _start(self, engine):
        with engine.begin() as conn:
            return sessions_module.start_session(conn, 1).session_id

    def _count(self, engine):
        import sqlalchemy as sa

        with engine.begin() as conn:
            return conn.execute(sa.text("SELECT COUNT(*) FROM workout_logs")).scalar()

    def test_a_retry_with_the_same_key_writes_one_row(self, engine):
        session_id = self._start(engine)
        results = []
        for _ in range(2):
            with engine.begin() as conn:
                results.append(sessions_module.log_set(
                    conn, 1, session_id, exercise_name="Bench Press", weight_kg=80, reps=8,
                    idempotency_key="pg-key-1"))
        (first, _), (second, breaks) = results
        assert not first.replayed and second.replayed and breaks == []
        assert second.log_id == first.log_id
        assert self._count(engine) == 1

    def test_the_index_settles_a_race_and_the_transaction_survives(self, engine, monkeypatch):
        """Both copies past the lookup: the INSERT conflicts inside a SAVEPOINT,
        the loser reads the winner's row, and its transaction is still usable."""
        session_id = self._start(engine)
        with engine.begin() as conn:
            sessions_module.log_set(conn, 1, session_id, exercise_name="Bench Press",
                                    weight_kg=80, reps=8, idempotency_key="pg-key-2")
        real_lookup = sessions_module.find_set_by_key
        lookups = iter([None])
        monkeypatch.setattr(sessions_module, "find_set_by_key",
                            lambda *a: next(lookups, None) or real_lookup(*a))
        with engine.begin() as conn:
            written, _ = sessions_module.log_set(
                conn, 1, session_id, exercise_name="Bench Press", weight_kg=80, reps=8,
                idempotency_key="pg-key-2")
            # A statement after the conflict: refused if the savepoint were missing.
            assert sessions_module.load_session(conn, 1, session_id) is not None
        assert written.replayed
        assert self._count(engine) == 1


# ==========================================================================
# Rate limiting
# ==========================================================================


class TestRebuildRateLimit:
    def test_three_per_minute_then_429_with_retry_after(self, client, monkeypatch):
        monkeypatch.setattr(records, "rebuild", lambda conn, user_id: 4)

        accepted = [client.post("/api/records/rebuild") for _ in range(3)]
        refused = client.post("/api/records/rebuild")

        assert [r.status_code for r in accepted] == [200, 200, 200]
        assert refused.status_code == 429
        assert refused.json()["error"]["code"] == "rate_limited"
        assert 1 <= int(refused.headers["retry-after"]) <= 60

    def test_the_limit_is_per_account(self, client, monkeypatch):
        monkeypatch.setattr(records, "rebuild", lambda conn, user_id: 4)
        for _ in range(4):
            client.post("/api/records/rebuild")
        client.signed_in["user"] = SimpleNamespace(user_id=8, username="bob")
        assert client.post("/api/records/rebuild").status_code == 200


class TestRetryAfter:
    def test_zero_while_under_the_limit(self):
        limiter = auth.RateLimiter(3, 60)
        limiter.check("k", now=0)
        assert limiter.retry_after("k", now=1) == 0

    def test_waits_for_the_oldest_counted_call_to_expire(self):
        limiter = auth.RateLimiter(3, 60)
        for stamp in (0, 10, 20):
            limiter.check("k", now=stamp)
        # Three in the window: the next is allowed once the one at t=0 expires.
        assert limiter.retry_after("k", now=30) == 30
        assert limiter.check("k", now=61) is True


# ==========================================================================
# OpenAPI
# ==========================================================================


class TestOpenApi:
    def test_the_committed_document_matches_the_code(self):
        committed = json.loads((ROOT / "docs" / "openapi.json").read_text(encoding="utf-8"))
        assert committed == json.loads(json.dumps(api.openapi_document())), (
            "docs/openapi.json is stale: run python scripts/export_openapi.py"
        )

    def test_every_route_is_documented(self):
        documented = api.openapi_document()["paths"]
        for route in api.router.routes:
            for method in route.methods:
                assert method.lower() in documented[route.path], (method, route.path)

    def test_the_old_paths_are_marked_deprecated(self):
        documented = api.openapi_document()["paths"]
        assert documented["/api/session/{session_id}/sets"]["post"]["deprecated"] is True
        assert "deprecated" not in documented["/api/sessions/{session_id}/sets"]["post"]

    def test_the_idempotency_header_and_replay_are_described(self):
        operation = api.openapi_document()["paths"]["/api/sessions/{session_id}/sets"]["post"]
        assert any(p["name"] == "idempotency-key" for p in operation["parameters"])
        assert {"200", "201", "400", "404", "409"} <= set(operation["responses"])

    def test_the_rate_limit_is_described(self):
        operation = api.openapi_document()["paths"]["/api/records/rebuild"]["post"]
        assert "Retry-After" in operation["responses"]["429"]["headers"]
