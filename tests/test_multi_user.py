"""Isolation: one account's training must never reach another's screen.

Two halves.

`TestQueriesAreScoped` drives the database functions against a recording
connection and asserts that every statement they emit names `user_id`, in the
SQL and in the bound parameters. An unscoped SELECT is the failure that shows
somebody else's lifts on your progress page; an unscoped DELETE is the one that
wipes them.

`TestRoutes` drives the app itself and asserts the id it hands those functions
always comes from the session cookie - never from the form that was posted, and
never from a query parameter.
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import auth  # noqa: E402
import insights  # noqa: E402
import pipeline  # noqa: E402

USER_ID = 7
OTHER_USER_ID = 8

# The tables a person's training lives in. A statement touching one of these
# without naming user_id is the bug this whole module exists to catch.
OWNED_TABLES = ("workout_logs", "bodyweight_logs", "exercises", "weekly_reports")


# --------------------------------------------------------------------------
# Recording fakes
# --------------------------------------------------------------------------


class _Result:
    def __init__(self, rows=()):
        self._rows = list(rows)

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def scalar_one(self):
        return 0

    def scalar(self):
        return None

    @property
    def rowcount(self):
        return 0


class RecordingConnection:
    """Records every statement and its parameters, returns nothing useful."""

    def __init__(self, rows=(), calls=None):
        self.rows = list(rows)
        self.calls = calls if calls is not None else []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, statement, params=None):
        sql = " ".join(str(statement).split())
        self.calls.append((sql, dict(params or {})))
        # The only rows any caller here actually reads back are the exercise id
        # from an upsert's RETURNING clause; everything else just needs to be
        # empty and not raise.
        if "RETURNING" in sql:
            return _Result([(1,)])
        return _Result(self.rows)


class RecordingEngine:
    def __init__(self, rows=()):
        self.rows = list(rows)
        self.calls: list = []

    def begin(self):
        return RecordingConnection(self.rows, self.calls)

    def connect(self):
        return RecordingConnection(self.rows, self.calls)


def assert_scoped(calls, user_id=USER_ID):
    """Every statement touching an owned table must name user_id and bind it."""
    assert calls, "expected at least one statement"
    for sql, params in calls:
        if not any(table in sql for table in OWNED_TABLES):
            continue
        assert "user_id" in sql, f"statement is not scoped to a user: {sql}"
        assert params.get("user_id") == user_id, f"wrong or missing user_id: {sql}"


# --------------------------------------------------------------------------
# The database layer
# --------------------------------------------------------------------------


class TestQueriesAreScoped:
    def test_counting_a_days_entries(self):
        engine = RecordingEngine()
        pipeline.count_entries_for_date(engine, date(2026, 8, 22), USER_ID)
        assert_scoped(engine.calls)

    def test_deleting_a_day(self):
        """The one destructive statement in the codebase. Unscoped, a single
        'Replace' would clear that date for every account on the deployment."""
        conn = RecordingConnection()
        pipeline.delete_entries_for_date(conn, date(2026, 8, 22), USER_ID)
        assert_scoped(conn.calls)
        assert len([sql for sql, _ in conn.calls if sql.startswith("DELETE")]) == 2

    def test_the_duplicate_guard(self):
        """Scoped so two people pasting the same template entry on the same
        evening each get theirs saved, rather than the second being swallowed."""
        engine = RecordingEngine()
        pipeline.find_recent_submission(engine, "bench 60x8", USER_ID)
        assert_scoped(engine.calls)

    def test_loading_exercise_names(self):
        conn = RecordingConnection()
        pipeline.load_exercise_names(conn, USER_ID)
        assert_scoped(conn.calls)

    def test_loading_exercise_groups(self):
        conn = RecordingConnection()
        pipeline.load_exercise_groups(conn, USER_ID)
        assert_scoped(conn.calls)

    def test_creating_an_exercise(self):
        conn = RecordingConnection()
        pipeline.get_or_create_exercise(conn, USER_ID, "Bench Press", "Chest", {})
        assert_scoped(conn.calls)

    def test_revising_the_group_of_an_existing_exercise(self):
        conn = RecordingConnection()
        pipeline.get_or_create_exercise(
            conn, USER_ID, "Bench Press", "Chest", {"Bench Press": 3}, chosen_group=True
        )
        assert_scoped(conn.calls)

    def test_two_users_can_each_own_the_same_exercise_name(self):
        """Uniqueness moved from (name) to (user_id, name), so the upsert has to
        conflict on both - otherwise the second person to log a Bench Press
        silently adopts the first person's row, muscle group and all."""
        conn = RecordingConnection()
        pipeline.get_or_create_exercise(conn, USER_ID, "Bench Press", "Chest", {})
        insert = next(sql for sql, _ in conn.calls if "INSERT INTO exercises" in sql)
        assert "ON CONFLICT (user_id, name)" in insert

    def test_loading_sets_for_the_report(self):
        engine = RecordingEngine()
        insights.load_set_records(engine, date(2026, 7, 1), date(2026, 8, 1), USER_ID)
        assert_scoped(engine.calls)

    def test_the_join_to_exercises_is_scoped_on_both_sides(self):
        """Belt and braces: the logs and the names are both per-user, so a
        mistake in one table's filter cannot leak a name from another account."""
        engine = RecordingEngine()
        insights.load_set_records(engine, date(2026, 7, 1), date(2026, 8, 1), USER_ID)
        sql = engine.calls[0][0]
        assert "e.user_id = w.user_id" in sql and "w.user_id = :user_id" in sql

    def test_loading_bodyweight(self):
        engine = RecordingEngine()
        insights.load_bodyweight(engine, date(2026, 7, 1), date(2026, 8, 1), USER_ID)
        assert_scoped(engine.calls)

    def test_saving_a_weekly_report(self):
        engine = RecordingEngine()
        insights.save_report(engine, USER_ID, date(2026, 8, 17), "summary", {})
        assert_scoped(engine.calls)

    def test_regenerating_a_week_overwrites_only_that_users_report(self):
        engine = RecordingEngine()
        insights.save_report(engine, USER_ID, date(2026, 8, 17), "summary", {})
        sql = engine.calls[0][0]
        assert "ON CONFLICT (user_id, week_start_date)" in sql

    def test_a_whole_entry_is_written_against_one_user(self):
        """The end-to-end write path: every insert it emits carries the owner."""
        draft = pipeline.draft_from_payload(
            {"sets": [{"exercise_name": "Bench Press", "weight_kg": 60.0, "reps": 8,
                       "raw_span": "bench 60x8"}],
             "bodyweight": {"weight_kg": 82.0, "raw_span": "bw 82"}},
            "bench 60x8, bw 82",
            date(2026, 8, 22),
        )
        for row in draft.rows:
            row.include = True

        engine = RecordingEngine()
        result = pipeline.commit_draft(draft, USER_ID, engine=engine)
        assert result.inserted_sets == 1 and result.inserted_bodyweight == 1
        assert_scoped(engine.calls)

    def test_replacing_a_day_deletes_only_that_users_rows(self):
        draft = pipeline.draft_from_payload(
            {"sets": [{"exercise_name": "Bench Press", "weight_kg": 60.0, "reps": 8,
                       "raw_span": "bench 60x8"}]},
            "bench 60x8",
            date(2026, 8, 22),
        )
        draft.sets[0].include = True
        draft.replace_existing = True

        engine = RecordingEngine()
        pipeline.commit_draft(draft, USER_ID, engine=engine)

        deletes = [sql for sql, _ in engine.calls if sql.startswith("DELETE")]
        assert deletes, "replace should have deleted the day"
        assert_scoped(engine.calls)

    def test_the_same_draft_writes_to_whichever_user_is_named(self):
        """`user_id` is an argument, not a field on the draft. The draft comes
        back from a hidden-field form; ownership must not."""
        def fresh_draft():
            draft = pipeline.draft_from_payload(
                {"sets": [{"exercise_name": "Bench Press", "weight_kg": 60.0,
                           "reps": 8, "raw_span": "bench 60x8"}]},
                "bench 60x8",
                date(2026, 8, 22),
            )
            draft.sets[0].include = True
            return draft

        mine = RecordingEngine()
        theirs = RecordingEngine()
        pipeline.commit_draft(fresh_draft(), USER_ID, engine=mine)
        pipeline.commit_draft(fresh_draft(), OTHER_USER_ID, engine=theirs)

        assert_scoped(mine.calls, USER_ID)
        assert_scoped(theirs.calls, OTHER_USER_ID)


class TestOwnershipIsNotOptional:
    """A write with no owner has to be impossible to express, not merely
    discouraged: a default would pick somebody, and it would pick wrong."""

    def test_commit_draft_demands_a_user(self):
        draft = pipeline.EntryDraft("bench 60x8", date(2026, 8, 22))
        with pytest.raises(TypeError):
            pipeline.commit_draft(draft, engine=RecordingEngine())

    def test_process_entry_demands_a_user(self):
        with pytest.raises(TypeError):
            pipeline.process_entry("bench 60x8", date(2026, 8, 22))

    def test_deleting_a_day_demands_a_user(self):
        with pytest.raises(TypeError):
            pipeline.delete_entries_for_date(RecordingConnection(), date(2026, 8, 22))

    def test_the_dashboard_demands_a_user(self):
        with pytest.raises(TypeError):
            insights.build_dashboard(RecordingEngine())


# --------------------------------------------------------------------------
# The web layer
# --------------------------------------------------------------------------


@pytest.fixture
def client(monkeypatch):
    """A TestClient over an app whose database and account store are fakes.

    The auth functions are replaced with an in-memory store so these tests can
    run without Postgres. What is under test is the app's wiring - which user id
    reaches the data layer - not the SQL, which the half above covers.
    """
    from fastapi.testclient import TestClient

    import app as app_module

    state = {
        "users": {
            "alice": auth.User(USER_ID, "alice", "alice", "Asia/Karachi"),
            "bob": auth.User(OTHER_USER_ID, "bob", "bob", None),
        },
        "passwords": {"alice": "alicepassword", "bob": "bobpassword"},
        "sessions": {},          # token -> username
        "calls": [],             # (what, user_id) seen by the data layer
    }

    def fake_authenticate(conn, username, password):
        name = (username or "").strip().lower()
        if state["passwords"].get(name) != password:
            return None
        return state["users"][name]

    def fake_create_session(conn, user_id, ttl_days=30):
        token = f"token-for-{user_id}"
        name = next(u.username for u in state["users"].values() if u.user_id == user_id)
        state["sessions"][token] = name
        return token

    def fake_resolve_session(conn, token):
        name = state["sessions"].get(token)
        return state["users"][name] if name else None

    def fake_delete_session(conn, token):
        state["sessions"].pop(token, None)

    def fake_create_user(conn, username, password, timezone_name=None):
        name = auth.normalize_username(username)
        if name in state["users"]:
            raise auth.AuthError("That username is already taken.")
        auth.validate_password(password)
        user = auth.User(100 + len(state["users"]), name, username.strip(),
                         auth.validate_timezone(timezone_name))
        state["users"][name] = user
        state["passwords"][name] = password
        return user

    def fake_set_timezone(conn, user_id, timezone_name):
        return auth.validate_timezone(timezone_name)

    def fake_set_password(conn, user_id, password):
        auth.validate_password(password)
        state["sessions"].clear()

    monkeypatch.setattr(auth, "authenticate", fake_authenticate)
    monkeypatch.setattr(auth, "create_session", fake_create_session)
    monkeypatch.setattr(auth, "resolve_session", fake_resolve_session)
    monkeypatch.setattr(auth, "delete_session", fake_delete_session)
    monkeypatch.setattr(auth, "create_user", fake_create_user)
    monkeypatch.setattr(auth, "set_timezone", fake_set_timezone)
    monkeypatch.setattr(auth, "set_password", fake_set_password)

    monkeypatch.setattr(app_module, "get_engine", lambda: RecordingEngine())

    # Data-layer stubs that record whose id they were handed.
    def record(what):
        def recorder(*args, **kwargs):
            state["calls"].append((what, args, kwargs))
            return _stub_return(what)
        return recorder

    def _stub_return(what):
        if what == "build_dashboard":
            return {"has_data": False, "week_start": "2026-08-17", "weeks": 12,
                    "timezone": "UTC", "kpis": {}, "weekly_volume": [],
                    "exercise_e1rm": [], "bodyweight": [], "muscle_volume": [],
                    "pain": []}
        if what == "generate_weekly_report":
            return {"week_start_date": date(2026, 8, 17), "summary_text": "ok",
                    "recommendations": {"program_note": {"stagnation_flagged": False,
                                                         "detail": {"reason": ""}}},
                    "stage_a": {}, "narration_error": None, "record_count": 0}
        if what == "commit_draft":
            return pipeline.PipelineResult(inserted_sets=1)
        if what == "count_entries_for_date":
            return {"sets": 0, "bodyweight": 0}
        return {}

    monkeypatch.setattr(insights, "build_dashboard", record("build_dashboard"))
    monkeypatch.setattr(insights, "generate_weekly_report", record("generate_weekly_report"))
    monkeypatch.setattr(pipeline, "commit_draft", record("commit_draft"))
    monkeypatch.setattr(pipeline, "count_entries_for_date", record("count_entries_for_date"))
    monkeypatch.setattr(pipeline, "load_exercise_groups", lambda conn, user_id: {})

    for limiter in (app_module._LOGIN_LIMITER, app_module._SIGNUP_LIMITER):
        limiter._hits.clear()

    test_client = TestClient(app_module.app, follow_redirects=False)
    test_client.state = state
    return test_client


def login(client, username="alice", password=None):
    password = password or client.state["passwords"][username]
    response = client.post("/login", data={"username": username, "password": password})
    assert response.status_code == 303, response.text
    return response


class TestSignedOut:
    @pytest.mark.parametrize("path", ["/", "/progress", "/weekly-report", "/account"])
    def test_every_data_page_sends_you_to_the_login_form(self, client, path):
        response = client.get(path)
        assert response.status_code == 303
        assert response.headers["location"].startswith("/login")

    def test_the_login_form_remembers_where_you_were_going(self, client):
        response = client.get("/progress")
        assert response.headers["location"] == "/login?next=/progress"

    def test_posting_an_entry_signed_out_writes_nothing(self, client):
        response = client.post(
            "/log", data={"session_date": "2026-08-22", "raw_text": "bench 60x8"}
        )
        assert response.status_code == 303
        assert client.state["calls"] == []

    def test_the_api_schema_is_not_published(self, client):
        """docs_url and redoc_url are off; without openapi_url off too, the
        schema behind them still serves every route and form field to anyone."""
        assert client.get("/openapi.json").status_code == 404

    def test_the_health_probe_stays_open(self, client):
        assert client.get("/healthz").status_code == 200

    def test_the_login_page_renders(self, client):
        body = client.get("/login").text
        assert "Log in" in body and 'name="password"' in body

    def test_the_signup_page_renders(self, client):
        body = client.get("/signup").text
        assert "Create account" in body


class TestLoggingIn:
    def test_correct_credentials_issue_a_session_cookie(self, client):
        response = login(client)
        assert auth.SESSION_COOKIE in response.cookies
        assert response.headers["location"] == "/"

    def test_the_cookie_is_not_readable_by_scripts(self, client):
        header = login(client).headers["set-cookie"]
        assert "HttpOnly" in header
        # Lax is what stands in for a CSRF token: the browser withholds this
        # cookie on a POST from another origin.
        assert "SameSite=lax" in header.lower().replace("samesite=lax", "SameSite=lax")

    def test_a_wrong_password_does_not(self, client):
        response = client.post(
            "/login", data={"username": "alice", "password": "not the password"}
        )
        assert response.status_code == 200
        assert auth.SESSION_COOKIE not in response.cookies

    def test_the_error_does_not_say_which_half_was_wrong(self, client):
        """Otherwise the form doubles as a test for which usernames exist."""
        missing = client.post(
            "/login", data={"username": "nobody", "password": "whatever12"}
        ).text
        wrong = client.post(
            "/login", data={"username": "alice", "password": "whatever12"}
        ).text
        assert "do not match an account" in missing
        assert "do not match an account" in wrong

    def test_you_land_back_where_you_were_going(self, client):
        response = client.post(
            "/login",
            data={"username": "alice", "password": "alicepassword", "next": "/progress"},
        )
        assert response.headers["location"] == "/progress"

    @pytest.mark.parametrize(
        "hostile",
        [
            "https://evil.example/phish",
            "//evil.example",
            "http://evil.example",
            # Browsers normalise "\\" to "/" while parsing a URL, so these reach
            # evil.example just as "//evil.example" does. Rejecting only a
            # leading "//" leaves the redirect wide open.
            "/\\evil.example",
            "/\\/evil.example",
            "\\\\evil.example",
            # A raw CR or LF in a Location header is a response-splitting
            # primitive; a path has no use for control characters at all.
            "/progress\r\nSet-Cookie: gt_session=stolen",
        ],
    )
    def test_you_cannot_be_bounced_off_the_site(self, client, hostile):
        """An open redirect on the login page is worth more to an attacker than
        no login page: it lands you on their copy of it, from a real link."""
        response = client.post(
            "/login",
            data={"username": "alice", "password": "alicepassword", "next": hostile},
        )
        assert response.headers["location"] == "/"

    def test_repeated_failures_are_slowed_down(self, client):
        for _ in range(10):
            client.post("/login", data={"username": "alice", "password": "wrong-one"})
        blocked = client.post("/login", data={"username": "alice", "password": "wrong-one"})
        assert "Too many attempts" in blocked.text

    def test_the_limit_does_not_stop_a_different_caller(self, client):
        for _ in range(12):
            client.post(
                "/login",
                data={"username": "alice", "password": "wrong-one"},
                headers={"x-forwarded-for": "10.0.0.1"},
            )
        response = client.post(
            "/login",
            data={"username": "alice", "password": "alicepassword"},
            headers={"x-forwarded-for": "10.0.0.2"},
        )
        assert response.status_code == 303


class TestSigningUp:
    def test_a_new_account_is_created_and_signed_in(self, client):
        response = client.post(
            "/signup", data={"username": "carol", "password": "carolpassword"}
        )
        assert response.status_code == 303
        assert response.headers["location"] == "/"
        assert "carol" in client.state["users"]

    def test_a_taken_username_is_refused(self, client):
        response = client.post(
            "/signup", data={"username": "alice", "password": "somepassword"}
        )
        assert response.status_code == 200
        assert "already taken" in response.text

    def test_a_short_password_is_refused(self, client):
        response = client.post("/signup", data={"username": "carol", "password": "short"})
        assert "at least" in response.text
        assert "carol" not in client.state["users"]

    def test_a_bad_timezone_is_refused(self, client):
        response = client.post(
            "/signup",
            data={"username": "carol", "password": "carolpassword",
                  "timezone": "Mars/Olympus"},
        )
        assert "not an IANA timezone" in response.text
        assert "carol" not in client.state["users"]

    def test_a_lookalike_username_cannot_be_registered(self, client):
        response = client.post(
            "/signup", data={"username": "ALICE", "password": "somepassword"}
        )
        assert "already taken" in response.text


class TestSignedIn:
    def test_the_entry_form_names_who_is_signed_in(self, client):
        login(client)
        body = client.get("/").text
        assert "alice" in body and "Log out" in body

    def test_the_dashboard_is_built_for_the_signed_in_user(self, client):
        login(client)
        client.get("/progress")
        what, args, kwargs = client.state["calls"][-1]
        assert what == "build_dashboard"
        assert args[1] == USER_ID

    def test_the_weekly_report_is_generated_for_the_signed_in_user(self, client):
        login(client)
        client.post("/weekly-report", data={"week_start": "2026-08-17"})
        what, args, kwargs = client.state["calls"][-1]
        assert what == "generate_weekly_report"
        assert args[1] == USER_ID

    def test_a_different_user_gets_their_own_data(self, client):
        login(client, "bob")
        client.get("/progress")
        _, args, _ = client.state["calls"][-1]
        assert args[1] == OTHER_USER_ID

    def test_the_users_own_timezone_is_used(self, client):
        """alice trains in Asia/Karachi; her week must not be cut on the
        deployment's UTC midnight."""
        login(client)
        client.get("/progress")
        _, _, kwargs = client.state["calls"][-1]
        assert kwargs["timezone_name"] == "Asia/Karachi"

    def test_logging_out_drops_the_session(self, client):
        login(client)
        client.post("/logout")
        assert client.get("/progress").status_code == 303

    def test_a_stale_cookie_is_not_honoured(self, client):
        client.cookies.set(auth.SESSION_COOKIE, "token-for-999")
        assert client.get("/").status_code == 303


class TestSavingBelongsToTheSession:
    """The review form round-trips through the browser, so nothing it carries
    can decide who the rows belong to."""

    def _form(self, **extra):
        form = {
            "session_date": "2026-08-22",
            "raw_text": "bench 60x8",
            "action": "save",
            "set_count": "1",
            "set_0_include": "on",
            "set_0_exercise_name": "Bench Press",
            "set_0_weight_kg": "60",
            "set_0_reps": "8",
            "bodyweight_present": "0",
        }
        form.update(extra)
        return form

    def test_the_owner_comes_from_the_cookie(self, client):
        login(client)
        client.post("/save", data=self._form())
        saved = [call for call in client.state["calls"] if call[0] == "commit_draft"]
        assert saved, "nothing was saved"
        assert saved[-1][1][1] == USER_ID

    def test_a_user_id_in_the_form_is_ignored(self, client):
        """The attack this closes: hand-edit the posted form to write into
        somebody else's account."""
        login(client)
        client.post("/save", data=self._form(user_id=str(OTHER_USER_ID)))
        saved = [call for call in client.state["calls"] if call[0] == "commit_draft"]
        assert saved[-1][1][1] == USER_ID

    def test_saving_signed_out_writes_nothing(self, client):
        response = client.post("/save", data=self._form())
        assert response.status_code == 303
        assert [c for c in client.state["calls"] if c[0] == "commit_draft"] == []


class TestAccountPage:
    def test_the_timezone_can_be_changed(self, client):
        login(client)
        response = client.post(
            "/account", data={"action": "timezone", "timezone": "Europe/London"}
        )
        assert "Timezone set to Europe/London" in response.text

    def test_a_bad_timezone_is_refused(self, client):
        login(client)
        response = client.post(
            "/account", data={"action": "timezone", "timezone": "Mars/Olympus"}
        )
        assert "not an IANA timezone" in response.text

    def test_changing_the_password_needs_the_current_one(self, client):
        login(client)
        response = client.post(
            "/account",
            data={"action": "password", "current_password": "wrong",
                  "new_password": "a-new-password"},
        )
        assert "Current password is not correct" in response.text

    def test_changing_the_password_signs_you_out(self, client):
        login(client)
        response = client.post(
            "/account",
            data={"action": "password", "current_password": "alicepassword",
                  "new_password": "a-new-password"},
        )
        assert response.status_code == 303
        assert response.headers["location"] == "/login"
        assert client.get("/progress").status_code == 303
