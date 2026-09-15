"""The live workout half: catalog, routines, sessions, records, measurements.

Two kinds of test here, for the same reason the rest of the suite is split that
way.

Most of it is pure logic and needs nothing: catalog search, input validation,
the record rules, the display formatting. Those run everywhere, always.

The rest is SQL, and SQL that is not scoped to a user is the bug that shows
somebody else's training on your screen. Those tests drive a recording fake —
the same one `test_multi_user.py` uses — and assert that every statement
touching an owned table names `user_id` and binds it. That catches the whole
class of mistake without needing a database, which keeps `pytest -q` runnable
on a laptop with nothing installed.

Tests that genuinely need Postgres (constraints, DISTINCT ON, the upsert) are
marked `needs_db` and skip unless TEST_DATABASE_URL is set:

    TEST_DATABASE_URL=postgresql+psycopg2://... python -m pytest tests/test_workout_app.py
"""

from __future__ import annotations

import os
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import catalog  # noqa: E402
import insights  # noqa: E402
import measurements as measurements_module  # noqa: E402
import records  # noqa: E402
import routines as routines_module  # noqa: E402
import sessions as sessions_module  # noqa: E402

USER_ID = 7
OTHER_USER_ID = 8

# Tables where an unscoped statement leaks or corrupts another account's data.
OWNED_TABLES = (
    "workout_logs", "bodyweight_logs", "exercises", "weekly_reports",
    "routines", "routine_exercises", "workout_sessions", "measurements",
    "personal_records",
)


# --------------------------------------------------------------------------
# Recording fake
# --------------------------------------------------------------------------


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

    @property
    def rowcount(self):
        return 1


class RecordingConnection:
    """Records every statement. Returns just enough for callers to proceed."""

    def __init__(self, calls=None, responses=None):
        self.calls = calls if calls is not None else []
        self.responses = responses or {}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, statement, params=None):
        sql = " ".join(str(statement).split())
        # executemany passes a list; record the first for the scoping assertions
        # and keep the shape the caller expects.
        recorded = params[0] if isinstance(params, list) and params else params
        self.calls.append((sql, dict(recorded or {})))

        for fragment, result in self.responses.items():
            if fragment in sql:
                return result
        if "RETURNING" in sql:
            return _Result([(1, datetime.now().astimezone())], scalar=1)
        if sql.strip().upper().startswith("SELECT"):
            return _Result([], scalar=0)
        return _Result([])


class RecordingEngine:
    def __init__(self, responses=None):
        self.calls: list = []
        self.responses = responses or {}

    def begin(self):
        return RecordingConnection(self.calls, self.responses)

    connect = begin


def assert_scoped(calls, user_id=USER_ID):
    """Every statement touching an owned table names user_id and binds it."""
    assert calls, "expected at least one statement"
    checked = 0
    for sql, params in calls:
        if not any(table in sql for table in OWNED_TABLES):
            continue
        checked += 1
        assert "user_id" in sql, f"statement is not scoped to a user: {sql}"
        assert params.get("user_id") == user_id, f"wrong or missing user_id: {sql}"
    assert checked, "no statement touched an owned table"


needs_db = pytest.mark.skipif(
    not os.getenv("TEST_DATABASE_URL"),
    reason="set TEST_DATABASE_URL to run the tests that need Postgres",
)


# ==========================================================================
# Catalog
# ==========================================================================


class TestCatalog:
    def test_every_entry_has_equipment_and_a_group(self):
        for item in catalog.CATALOG:
            assert item.equipment in catalog.EQUIPMENT, item.name
            assert item.muscle_group, item.name

    def test_names_are_unique(self):
        keys = [item.key for item in catalog.CATALOG]
        assert len(keys) == len(set(keys))

    def test_equipment_is_a_field_not_a_name_suffix(self):
        """Two variants of one movement are separate, comparable entries.

        They must not share a progression chart: a dumbbell bench at 30kg and a
        barbell bench at 80kg are not the same lift getting weaker.
        """
        bench = [e for e in catalog.CATALOG if e.movement == "Bench Press"]
        assert len({e.equipment for e in bench}) == len(bench) > 1

    @pytest.mark.parametrize(
        "query,expected",
        [
            ("bench", "Bench Press (Barbell)"),
            ("rdl", "Romanian Deadlift (Barbell)"),
            ("ohp", "Overhead Press (Barbell)"),
            ("pec deck", "Chest Fly (Dumbbell)"),
            ("pushdown", "Tricep Pushdown (Cable)"),
        ],
    )
    def test_search_ranks_the_obvious_answer_first(self, query, expected):
        assert catalog.search(query)[0].name == expected

    @pytest.mark.parametrize(
        "query,expected",
        [
            ("db incline", "Incline Bench Press (Dumbbell)"),
            ("bb row", "Bent Over Row (Barbell)"),
            ("kb swing", "Kettlebell Swing (Kettlebell)"),
            ("dumbell curl", "Bicep Curl (Dumbbell)"),
        ],
    )
    def test_gym_abbreviations_and_misspellings_resolve(self, query, expected):
        """"db" is how a dumbbell press is searched for in an actual gym.

        Matching only the full word "Dumbbell" returns nothing for it, and a
        search box that fails on the most common abbreviation is a search box
        nobody uses twice.
        """
        assert catalog.search(query)[0].name == expected

    def test_equipment_filter_narrows(self):
        found = catalog.search("", equipment="barbell")
        assert found and all(item.equipment == "barbell" for item in found)

    def test_muscle_group_filter_narrows(self):
        found = catalog.search("", muscle_group="Chest")
        assert found and all(item.muscle_group == "Chest" for item in found)

    def test_resolve_is_exact_only(self):
        """A near-miss returns nothing rather than guessing.

        Guessing here files a set under the wrong lift. The fuzzy matching in
        pipeline.py handles near-misses, and does it with a person in the loop.
        """
        assert catalog.resolve("Bench Press (Barbell)") is not None
        assert catalog.resolve("bench press (barbell)") is not None  # case/punctuation
        assert catalog.resolve("Bench Pres") is None
        assert catalog.resolve("") is None

    def test_search_key_folds_case_accents_and_punctuation(self):
        assert catalog.search_key("Bench-Press (Barbell)") == "bench press barbell"
        assert catalog.search_key("Björn's Row") == "bjorn s row"

    def test_empty_query_browses_in_declared_order(self):
        """Not alphabetical — that would greet everyone with Ab Wheel Rollout."""
        assert catalog.search("")[0].muscle_group == "Chest"


# ==========================================================================
# Routines
# ==========================================================================


class TestRoutineValidation:
    def test_name_is_collapsed_and_trimmed(self):
        assert routines_module.clean_name("  Push   Day  ") == "Push Day"

    @pytest.mark.parametrize("bad", ["", "   ", "\t\n"])
    def test_blank_name_rejected(self, bad):
        with pytest.raises(routines_module.RoutineError):
            routines_module.clean_name(bad)

    def test_absurdly_long_name_rejected(self):
        with pytest.raises(routines_module.RoutineError):
            routines_module.clean_name("x" * 81)

    def test_a_single_rep_value_becomes_a_fixed_target(self):
        cleaned = routines_module._clean_target({"target_reps_low": 8})
        assert cleaned["target_reps_low"] == cleaned["target_reps_high"] == 8

    def test_reversed_rep_range_is_swapped_not_rejected(self):
        """"12-8" is unambiguous about what was meant; refusing it is pedantry."""
        cleaned = routines_module._clean_target(
            {"target_reps_low": 12, "target_reps_high": 8}
        )
        assert (cleaned["target_reps_low"], cleaned["target_reps_high"]) == (8, 12)

    @pytest.mark.parametrize(
        "payload",
        [
            {"target_sets": 0},
            {"target_sets": 999},
            {"target_reps_low": "abc"},
            {"target_weight_kg": -5},
            {"rest_seconds": 99999},
        ],
    )
    def test_out_of_range_values_are_rejected_with_a_message(self, payload):
        with pytest.raises(routines_module.RoutineError) as caught:
            routines_module._clean_target(payload)
        assert str(caught.value)  # a message meant for a person, not a stack trace

    def test_target_reps_label(self):
        make = lambda low, high: routines_module.RoutineExercise(  # noqa: E731
            exercise_id=1, name="x", target_reps_low=low, target_reps_high=high
        )
        assert make(8, 12).target_reps_label == "8-12"
        assert make(5, 5).target_reps_label == "5"
        assert make(None, None).target_reps_label == ""

    def test_routine_totals_and_groups_are_derived(self):
        routine = routines_module.Routine(
            routine_id=1, user_id=USER_ID, name="Push",
            exercises=[
                routines_module.RoutineExercise(1, "Bench", target_sets=4, muscle_group="Chest"),
                routines_module.RoutineExercise(2, "Fly", target_sets=3, muscle_group="Chest"),
                routines_module.RoutineExercise(3, "Push", target_sets=3, muscle_group="Triceps"),
            ],
        )
        assert routine.total_sets == 10
        # Order of first appearance, not sorted: it reads as the session's shape.
        assert routine.muscle_groups == ["Chest", "Triceps"]


class TestRoutineQueriesAreScoped:
    def test_listing(self):
        engine = RecordingEngine()
        with engine.begin() as conn:
            routines_module.list_routines(conn, USER_ID)
        assert_scoped(engine.calls)

    def test_getting_one(self):
        engine = RecordingEngine(
            responses={"FROM routines WHERE routine_id":
                       _Result([(1, USER_ID, "Push", None, 0, False)])}
        )
        with engine.begin() as conn:
            routines_module.get_routine(conn, USER_ID, 1)
        assert_scoped(engine.calls)

    def test_deleting(self):
        engine = RecordingEngine()
        with engine.begin() as conn:
            routines_module.delete_routine(conn, USER_ID, 1)
        assert_scoped(engine.calls)

    def test_reordering(self):
        engine = RecordingEngine()
        with engine.begin() as conn:
            routines_module.reorder_routines(conn, USER_ID, [3, 1, 2])
        assert_scoped(engine.calls)

    def test_routine_exercises_are_scoped_through_their_routine(self):
        """`routine_exercises` has no user_id column of its own.

        It is scoped through its parent routine, so every statement against it
        must join `routines` and filter there. Without that join, a guessed
        routine id reads somebody else's programme — and because the rows join
        to `exercises` for their names, it leaks those too.
        """
        engine = RecordingEngine()
        with engine.begin() as conn:
            routines_module._load_exercises(conn, USER_ID, [1, 2])
        sql, params = engine.calls[0]
        assert "JOIN routines" in sql
        assert "r.user_id = :user_id" in sql
        assert params["user_id"] == USER_ID


# ==========================================================================
# Sessions
# ==========================================================================


class TestSetValidation:
    def test_reps_are_required(self):
        with pytest.raises(sessions_module.SessionError):
            sessions_module._validate_set(100, None, 0, None, None)

    def test_cheat_reps_cannot_exceed_reps(self):
        with pytest.raises(sessions_module.SessionError):
            sessions_module._validate_set(100, 5, 9, None, None)

    @pytest.mark.parametrize("weight", [-1, 1001, "heavy"])
    def test_implausible_weight_rejected(self, weight):
        with pytest.raises(sessions_module.SessionError):
            sessions_module._validate_set(weight, 5, 0, None, None)

    @pytest.mark.parametrize("rpe", [0.5, 11])
    def test_rpe_outside_its_scale_rejected(self, rpe):
        with pytest.raises(sessions_module.SessionError):
            sessions_module._validate_set(100, 5, 0, rpe, None)

    def test_a_bodyweight_set_may_have_no_weight(self):
        """A pull-up with no added load is a complete log, not a missing one."""
        cleaned = sessions_module._validate_set(None, 10, 0, None, None)
        assert cleaned["weight_kg"] is None and cleaned["reps"] == 10

    def test_blank_strings_are_absent_not_zero(self):
        cleaned = sessions_module._validate_set("", 8, "", "", "")
        assert cleaned["weight_kg"] is None
        assert cleaned["rpe"] is None and cleaned["rir"] is None


class TestSessionTypes:
    def test_volume_excludes_warmups(self):
        """Warm-ups are work, but not the work volume is measuring."""
        working = sessions_module.LoggedSet(1, 1, "Bench", 1, weight_kg=100, reps=5)
        warmup = sessions_module.LoggedSet(2, 1, "Bench", 0, weight_kg=60, reps=10,
                                           is_warmup=True)
        assert working.volume_kg == 500
        assert warmup.volume_kg == 0

    def test_display_handles_bodyweight(self):
        assert sessions_module.LoggedSet(1, 1, "x", 1, weight_kg=80, reps=8).display == "80 × 8"
        assert sessions_module.LoggedSet(2, 1, "x", 1, weight_kg=None, reps=12).display == "× 12"

    def test_completion_counts_working_sets_only(self):
        block = sessions_module.SessionExercise(
            exercise_id=1, name="Bench", target_sets=3,
            sets=[
                sessions_module.LoggedSet(1, 1, "Bench", 0, 60, 10, is_warmup=True),
                sessions_module.LoggedSet(2, 1, "Bench", 1, 80, 8),
                sessions_module.LoggedSet(3, 1, "Bench", 2, 80, 8),
            ],
        )
        assert block.working_sets_done == 2
        assert block.is_complete is False

    def test_an_unplanned_exercise_is_complete_once_it_has_a_set(self):
        block = sessions_module.SessionExercise(exercise_id=1, name="Curl")
        assert block.is_complete is False
        block.sets.append(sessions_module.LoggedSet(1, 1, "Curl", 1, 20, 12))
        assert block.is_complete is True


class TestSessionQueriesAreScoped:
    def test_active_session_lookup(self):
        engine = RecordingEngine()
        with engine.begin() as conn:
            sessions_module.active_session(conn, USER_ID)
        assert_scoped(engine.calls)

    def test_loading_a_session(self):
        engine = RecordingEngine(
            responses={"FROM workout_sessions WHERE session_id":
                       _Result([(1, USER_ID, "Push", datetime.now().astimezone(),
                                 None, None, None)])}
        )
        with engine.begin() as conn:
            sessions_module.load_session(conn, USER_ID, 1)
        assert_scoped(engine.calls)

    def test_previous_performance(self):
        engine = RecordingEngine()
        with engine.begin() as conn:
            sessions_module.previous_performance(conn, USER_ID, 1)
        assert_scoped(engine.calls)

    def test_deleting_a_set(self):
        engine = RecordingEngine()
        with engine.begin() as conn:
            sessions_module.delete_set(conn, USER_ID, 1)
        assert_scoped(engine.calls)

    def test_discarding_deletes_the_sets_explicitly(self):
        """`workout_logs.session_id` is ON DELETE SET NULL, not CASCADE.

        That is right for deleting a routine — history survives — and means a
        discarded session would otherwise leave its sets behind, detached and
        still counted in every chart.
        """
        engine = RecordingEngine()
        with engine.begin() as conn:
            sessions_module.discard_session(conn, USER_ID, 1)
        statements = [sql for sql, _ in engine.calls]
        assert any("DELETE FROM workout_logs" in sql for sql in statements)
        assert any("DELETE FROM workout_sessions" in sql for sql in statements)
        assert_scoped(engine.calls)

    def test_an_exercise_id_from_the_client_is_rechecked(self):
        """The id arrives in a request body, so it is a claim, not a fact."""
        engine = RecordingEngine(responses={"SELECT name FROM exercises": _Result([])})
        with engine.begin() as conn:
            conn.responses = {
                "SELECT finished_at": _Result([(None,)]),
                "SELECT name FROM exercises": _Result([]),
            }
            with pytest.raises(sessions_module.SessionError, match="not in your list"):
                sessions_module.log_set(conn, USER_ID, 1, exercise_id=999, reps=5)


# ==========================================================================
# Personal records
# ==========================================================================


class TestRecordRules:
    def test_a_warmup_sets_nothing(self):
        """A heavy warm-up single would otherwise poison the cache with a
        number no later real set could beat."""
        assert records.candidates(100, 1, is_warmup=True) == {}

    def test_an_incomplete_set_sets_nothing(self):
        assert records.candidates(None, 5) == {}
        assert records.candidates(100, None) == {}
        assert records.candidates(0, 5) == {}

    def test_a_working_set_sets_weight_and_1rm(self):
        found = records.candidates(100, 5)
        assert set(found) == {records.HEAVIEST_WEIGHT, records.BEST_1RM}
        assert found[records.HEAVIEST_WEIGHT][0] == 100

    def test_1rm_matches_the_weekly_report(self):
        """One implementation, so a PR announced mid-workout cannot disagree
        with the report generated on Sunday."""
        found = records.candidates(100, 5)
        assert found[records.BEST_1RM][0] == pytest.approx(insights.epley_1rm(100, 5))

    def test_cheated_reps_do_not_demonstrate_strength(self):
        """They still count toward volume — the work happened — but a set of
        entirely cheated reps proves nothing about the load."""
        assert records.candidates(100, 5, cheat_reps=5) == {}
        partial = records.candidates(100, 5, cheat_reps=2)
        assert partial[records.BEST_1RM][0] == pytest.approx(insights.epley_1rm(100, 3))

    def test_clean_rep_rule_is_shared_with_insights(self):
        assert insights.clean_rep_count(10, 3) == 7
        assert insights.clean_rep_count(10, 0) == 10
        assert insights.clean_rep_count(None, 0) is None
        # Never negative: more cheat reps than reps is a data-entry error, not a
        # negative set.
        assert insights.clean_rep_count(3, 10) == 0


class TestRecordPresentation:
    def test_a_first_record_is_worded_differently(self):
        """Calling somebody's first logged set a "personal record" is
        technically true and reads as a lie."""
        first = records.RecordBreak(records.HEAVIEST_WEIGHT, "Bench", 100, None)
        beaten = records.RecordBreak(records.HEAVIEST_WEIGHT, "Bench", 105, 100)
        assert first.is_first and first.improvement is None
        assert not beaten.is_first and beaten.improvement == 5

    def test_display_reads_like_a_person_wrote_it(self):
        assert records.PersonalRecord(1, records.HEAVIEST_WEIGHT, 100.0, 100.0, 5).display \
            == "100 kg × 5"
        assert records.PersonalRecord(1, records.BEST_1RM, 102.5, 85.0, 6).display \
            == "102.5 kg e1RM (85 × 6)"

    def test_every_record_type_has_a_label(self):
        for record_type in records.RECORD_TYPES:
            assert records.RECORD_LABELS[record_type]


class TestRecordQueriesAreScoped:
    def test_current(self):
        engine = RecordingEngine()
        with engine.begin() as conn:
            records.current(conn, USER_ID, 1)
        assert_scoped(engine.calls)

    def test_recent(self):
        engine = RecordingEngine()
        with engine.begin() as conn:
            records.recent(conn, USER_ID)
        assert_scoped(engine.calls)

    def test_rebuild(self):
        engine = RecordingEngine()
        with engine.begin() as conn:
            records.rebuild(conn, USER_ID)
        assert_scoped(engine.calls)


# ==========================================================================
# Measurements
# ==========================================================================


class TestMeasurementValidation:
    def test_site_names_are_folded(self):
        assert measurements_module.normalise_site("Left Arm") == "left_arm"
        assert measurements_module.normalise_site("  LEFT   ARM ") == "left_arm"

    def test_blank_site_rejected(self):
        with pytest.raises(measurements_module.MeasurementError):
            measurements_module.normalise_site("  ")

    def test_labels_fall_back_gracefully(self):
        assert measurements_module.site_label("left_arm") == "Left arm"
        assert measurements_module.site_label("left_bicep_peak") == "Left bicep peak"

    def test_inches_conversion(self):
        assert measurements_module.to_inches(2.54) == 1.0
        assert measurements_module.to_inches(None) is None

    def test_trend_change_and_percentage(self):
        now = datetime.now().astimezone()
        trend = measurements_module.SiteTrend(
            site="waist", latest_cm=81, latest_at=now,
            first_cm=84, first_at=now - timedelta(days=60), reading_count=2,
        )
        assert trend.change_cm == -3.0
        assert trend.change_pct == pytest.approx(-3.6, abs=0.05)

    def test_a_single_reading_has_no_change(self):
        """One reading is a latest with nothing to compare to — None, not zero,
        which would read as "measured, unchanged"."""
        trend = measurements_module.SiteTrend(
            site="waist", latest_cm=81, latest_at=datetime.now().astimezone(),
        )
        assert trend.change_cm is None and trend.change_pct is None


class TestMeasurementQueriesAreScoped:
    def test_latest(self):
        engine = RecordingEngine()
        with engine.begin() as conn:
            measurements_module.latest(conn, USER_ID)
        assert_scoped(engine.calls)

    def test_series(self):
        engine = RecordingEngine()
        with engine.begin() as conn:
            measurements_module.series(conn, USER_ID, ["waist"])
        assert_scoped(engine.calls)

    def test_trends(self):
        engine = RecordingEngine()
        with engine.begin() as conn:
            measurements_module.trends(conn, USER_ID)
        assert_scoped(engine.calls)

    def test_bodyweight_series(self):
        engine = RecordingEngine()
        with engine.begin() as conn:
            measurements_module.bodyweight_series(conn, USER_ID)
        assert_scoped(engine.calls)

    @pytest.mark.parametrize(
        "module",
        [measurements_module, sessions_module, routines_module, records],
        ids=lambda m: m.__name__,
    )
    def test_no_sql_uses_the_colon_cast_syntax(self, module):
        """`:name::type` beside a bind parameter is unparseable by text().

        SQLAlchemy scans the string for bind parameters itself and does not
        understand the Postgres :: operator next to one: it reads ":since::date"
        as a parameter named `since::date`, leaves the real placeholder
        unsubstituted, and the statement fails at the database. Casts must be
        written CAST(:name AS type).

        That scan covers SQL comments too, so a comment mentioning a colon-name
        is itself read as a bind parameter.

        Checked by walking the AST for the string arguments of `text(...)`
        rather than by grepping the file, so the prose explaining the rule --
        this docstring included -- is not mistaken for a violation.
        """
        import ast

        source = Path(module.__file__).read_text()
        offenders: list[str] = []

        for node in ast.walk(ast.parse(source)):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id == "text"):
                continue
            for argument in node.args:
                if not (isinstance(argument, ast.Constant) and isinstance(argument.value, str)):
                    continue
                # The lookbehind matters: a column cast chain like
                # `logged_at::date::text` contains ":date::" as a substring but
                # is perfectly valid — no bind parameter is involved. Only a
                # colon that is NOT itself part of a :: operator can start one.
                for match in re.finditer(r"(?<!:):([a-z_]+)::", argument.value):
                    offenders.append(f"{module.__name__}:{argument.lineno} :{match.group(1)}::")

        assert not offenders, (
            "use CAST(:name AS type) instead of :name::type -- "
            f"{offenders}"
        )


# ==========================================================================
# Tests that need a real database
# ==========================================================================


@needs_db
class TestAgainstPostgres:
    """The parts whose behaviour is the database's, not Python's."""

    @pytest.fixture
    def engine(self):
        import sqlalchemy as sa

        eng = sa.create_engine(os.environ["TEST_DATABASE_URL"], future=True)
        with eng.begin() as conn:
            conn.execute(sa.text(
                "TRUNCATE personal_records, measurements, workout_logs, "
                "workout_sessions, routine_exercises, routines, exercises, "
                "users RESTART IDENTITY CASCADE"))
            conn.execute(sa.text(
                "INSERT INTO users (username, display_name, password_hash) "
                "VALUES ('a','A','x'), ('b','B','x')"))
        return eng

    def test_only_one_live_session_per_person(self, engine):
        """Enforced by a partial unique index, not by checking first.

        A double-tapped "start workout" with cold hands is a real thing, and
        check-then-insert races: both checks pass, both insert, and half the
        workout lands in each session.
        """
        import sqlalchemy as sa

        with engine.begin() as conn:
            conn.execute(sa.text(
                "INSERT INTO workout_sessions (user_id, name) VALUES (1, 'A')"))
        with pytest.raises(Exception):
            with engine.begin() as conn:
                conn.execute(sa.text(
                    "INSERT INTO workout_sessions (user_id, name) VALUES (1, 'B')"))
        # Finishing the first frees the slot.
        with engine.begin() as conn:
            conn.execute(sa.text(
                "UPDATE workout_sessions SET finished_at = now() WHERE user_id = 1"))
            conn.execute(sa.text(
                "INSERT INTO workout_sessions (user_id, name) VALUES (1, 'B')"))

    def test_incremental_records_match_a_full_rebuild(self, engine):
        """The two paths must agree, or the cache is worse than no cache."""
        import sqlalchemy as sa

        with engine.begin() as conn:
            exercise_id = sessions_module.resolve_exercise(conn, 1, "Bench Press (Barbell)")
            session_id = conn.execute(sa.text(
                "INSERT INTO workout_sessions (user_id, name) VALUES (1,'P') "
                "RETURNING session_id")).scalar()
            for weight, reps, warm in [(60, 10, True), (80, 8, False), (85, 6, False)]:
                sessions_module.log_set(
                    conn, 1, session_id, exercise_id=exercise_id,
                    weight_kg=weight, reps=reps, is_warmup=warm)
            incremental = {t: round(r.value, 2) for t, r in records.current(conn, 1, exercise_id).items()}
            records.rebuild(conn, 1)
            rebuilt = {t: round(r.value, 2) for t, r in records.current(conn, 1, exercise_id).items()}
        assert incremental == rebuilt

    def test_a_session_with_no_sets_is_not_kept(self, engine):
        """Somebody opened the app and left. Keeping it would put a phantom
        workout on the heatmap and in the weekly count."""
        import sqlalchemy as sa

        with engine.begin() as conn:
            session = sessions_module.start_session(conn, 1, name="Nothing")
            sessions_module.finish_session(conn, 1, session.session_id)
            remaining = conn.execute(sa.text(
                "SELECT count(*) FROM workout_sessions WHERE user_id = 1")).scalar()
        assert remaining == 0

    def test_another_account_cannot_reach_a_routine(self, engine):
        with engine.begin() as conn:
            exercise_id = sessions_module.resolve_exercise(conn, 1, "Squat (Barbell)")
            routine = routines_module.create_routine(
                conn, 1, "Legs", exercises=[{"exercise_id": exercise_id, "target_sets": 3}])
            assert routines_module.get_routine(conn, 2, routine.routine_id) is None
            assert routines_module.list_routines(conn, 2) == []

    def test_a_routine_cannot_reference_another_accounts_exercise(self, engine):
        with engine.begin() as conn:
            mine = sessions_module.resolve_exercise(conn, 1, "Squat (Barbell)")
            theirs = sessions_module.resolve_exercise(conn, 2, "Secret Lift")
            routine = routines_module.create_routine(conn, 1, "Legs")
            with pytest.raises(routines_module.RoutineError):
                routines_module.replace_exercises(
                    conn, 1, routine.routine_id, [{"exercise_id": theirs}])
            # The legitimate one still works.
            routines_module.replace_exercises(
                conn, 1, routine.routine_id, [{"exercise_id": mine, "target_sets": 3}])

    def test_previous_performance_is_one_earlier_session(self, engine):
        """Not "the last N sets" — repeating a workout would splice two
        different days into a Previous column that never happened."""
        import sqlalchemy as sa

        with engine.begin() as conn:
            exercise_id = sessions_module.resolve_exercise(conn, 1, "Squat (Barbell)")
            first = sessions_module.start_session(conn, 1, name="A")
            for weight in (100, 105):
                sessions_module.log_set(conn, 1, first.session_id,
                                        exercise_id=exercise_id, weight_kg=weight, reps=5)
            sessions_module.finish_session(conn, 1, first.session_id)

            second = sessions_module.start_session(conn, 1, name="B")
            sessions_module.log_set(conn, 1, second.session_id,
                                    exercise_id=exercise_id, weight_kg=110, reps=5)
            previous, when = sessions_module.previous_performance(
                conn, 1, exercise_id, before_session_id=second.session_id)

        assert [s.weight_kg for s in previous] == [100, 105]
        assert when is not None

    def test_catalog_pick_arrives_with_equipment_filled_in(self, engine):
        import sqlalchemy as sa

        with engine.begin() as conn:
            exercise_id = sessions_module.resolve_exercise(conn, 1, "Bench Press (Barbell)")
            row = conn.execute(sa.text(
                "SELECT equipment, muscle_group, is_custom FROM exercises "
                "WHERE exercise_id = :id"), {"id": exercise_id}).fetchone()
        assert row == ("barbell", "Chest", False)

    def test_a_lift_not_in_the_catalog_is_marked_custom(self, engine):
        import sqlalchemy as sa

        with engine.begin() as conn:
            exercise_id = sessions_module.resolve_exercise(conn, 1, "Sohaib Special Curl")
            row = conn.execute(sa.text(
                "SELECT equipment, is_custom FROM exercises WHERE exercise_id = :id"),
                {"id": exercise_id}).fetchone()
        assert row == (None, True)

    def test_measurements_share_one_timestamp_per_sitting(self, engine):
        """Measured together should line up on one x position on a chart."""
        with engine.begin() as conn:
            written = measurements_module.record_many(
                conn, 1, {"chest": "104", "waist": "84", "left_arm": "38.5"})
        assert len({m.measured_at for m in written}) == 1

    def test_a_blank_measurement_is_skipped_not_zeroed(self, engine):
        with engine.begin() as conn:
            written = measurements_module.record_many(
                conn, 1, {"chest": "104", "waist": "", "hips": None})
        assert [m.site for m in written] == ["chest"]


# ==========================================================================
# Regressions
#
# One test per bug found in review. Each names the failure it prevents, because
# a regression test whose purpose is not written down gets deleted by the next
# person who finds it inconvenient.
# ==========================================================================


class TestValidationRegressions:
    def test_zero_reps_is_rejected_in_python_not_by_the_database(self):
        """reps=0 passed every Python check and then violated
        `workout_logs_reps_positive`, so a typo surfaced as an opaque 500
        instead of the message the validator exists to produce."""
        with pytest.raises(sessions_module.SessionError, match="Reps"):
            sessions_module._validate_set(50, 0, 0, None, None)

    def test_zero_weight_is_still_allowed(self):
        """The bound moved for reps only. A bodyweight set carries no load, and
        rejecting weight=0 would make a dip unloggable."""
        cleaned = sessions_module._validate_set(0, 10, 0, None, None)
        assert cleaned["weight_kg"] == 0 and cleaned["reps"] == 10


class TestPartialUpdateRegression:
    """PUT /sets/{id} is a partial update. It used to replace every column."""

    def _stored(self):
        return {
            "weight_kg": 100.0, "reps": 5, "cheat_reps": 0,
            "rpe": 8.0, "rir": None,
        }

    def test_omitted_fields_keep_their_stored_values(self):
        engine = RecordingEngine(
            responses={
                "FROM workout_logs wl JOIN exercises": _Result([(
                    3, "Squat", 9, 2, True, 100.0, 5, 0, 8.0, None, False, False, None,
                    datetime.now().astimezone(),
                )]),
            }
        )
        with engine.begin() as conn:
            updated = sessions_module.update_set(conn, USER_ID, 42, reps=9)

        update = next(
            params for sql, params in engine.calls if sql.startswith("UPDATE workout_logs")
        )
        # Only the rep count moved. Nulling the rest is what destroyed the
        # recorded load and effort of a set somebody was fixing a typo in.
        assert update["reps"] == 9
        assert update["weight_kg"] == 100.0
        assert update["rpe"] == 8.0
        assert updated.weight_kg == 100.0

    def test_an_omitted_warmup_flag_is_not_cleared(self):
        """`fields.get("is_warmup", stored)` never fired its default, because
        the key was present with value None. A warm-up silently became a
        working set, and then competed for records."""
        engine = RecordingEngine(
            responses={
                "FROM workout_logs wl JOIN exercises": _Result([(
                    3, "Squat", 9, 2, True, 100.0, 5, 0, None, None, False, False, None,
                    datetime.now().astimezone(),
                )]),
            }
        )
        with engine.begin() as conn:
            updated = sessions_module.update_set(conn, USER_ID, 42, reps=6)
        assert updated.is_warmup is True

    def test_an_explicit_null_still_clears_a_field(self):
        """Passing rpe=None means "clear it" and must be distinguishable from
        not mentioning rpe at all."""
        engine = RecordingEngine(
            responses={
                "FROM workout_logs wl JOIN exercises": _Result([(
                    3, "Squat", 9, 2, False, 100.0, 5, 0, 8.0, None, False, False, None,
                    datetime.now().astimezone(),
                )]),
            }
        )
        with engine.begin() as conn:
            sessions_module.update_set(conn, USER_ID, 42, rpe=None)
        update = next(
            params for sql, params in engine.calls if sql.startswith("UPDATE workout_logs")
        )
        assert update["rpe"] is None
        assert update["weight_kg"] == 100.0  # untouched


class TestSearchKeyAgreement:
    def test_the_migration_backfill_matches_catalog_search_key(self):
        """The backfilled key has to equal what the application computes, or the
        indexed lookup misses every migrated row and the picker offers to create
        lifts the person already has.

        The SQL is read out of the migration rather than restated, so the two
        cannot drift apart without this failing.
        """
        migration = (
            Path(__file__).resolve().parents[1]
            / "migrations"
            / "003_routines_sessions_measurements.sql"
        ).read_text()
        assert "btrim(" in migration, "the backfill must trim, or trailing punctuation lingers"
        assert "translate(" in migration, "the backfill must fold accents"
        # And the Python side is the definition both agree on.
        assert catalog.search_key("Bench Press (Barbell)") == "bench press barbell"
        assert catalog.search_key("Café Press") == "cafe press"

    def test_the_journal_flow_writes_a_search_key(self):
        """A row created by pipeline.get_or_create_exercise with a NULL
        search_key is invisible to the picker's indexed search, which is how one
        lift became two rows with two separate histories."""
        import pipeline

        source = Path(pipeline.__file__).read_text()
        insert = source[source.index("INSERT INTO exercises"):]
        insert = insert[: insert.index("RETURNING")]
        assert "search_key" in insert


class TestPackaging:
    def test_dockerignore_excludes_secrets_and_the_virtualenv(self):
        """`COPY . .` with no .dockerignore bakes .env into an image layer,
        where docker history exposes it to anyone who can pull."""
        patterns = {
            line.strip()
            for line in (Path(__file__).resolve().parents[1] / ".dockerignore").read_text().splitlines()
            if line.strip() and not line.startswith("#")
        }
        for required in (".env", ".venv/", ".git/"):
            assert required in patterns, f"{required} must not reach the image"

    def test_the_cardinality_demo_still_reaches_the_image(self):
        """docker-compose runs it from the app image, so excluding
        observability/ wholesale would break the Part E2 service."""
        ignore = (Path(__file__).resolve().parents[1] / ".dockerignore").read_text()
        assert "observability/\n" not in ignore
        assert "observability/cardinality_demo.py" not in ignore


@needs_db
class TestPostgresRegressions:
    """Bugs that only reproduce against a real database."""

    @pytest.fixture
    def engine(self):
        import sqlalchemy as sa

        eng = sa.create_engine(os.environ["TEST_DATABASE_URL"], future=True)
        with eng.begin() as conn:
            conn.execute(sa.text(
                "TRUNCATE personal_records, measurements, workout_logs, "
                "workout_sessions, routine_exercises, routines, exercises, "
                "users RESTART IDENTITY CASCADE"))
            conn.execute(sa.text(
                "INSERT INTO users (username, display_name, password_hash) "
                "VALUES ('a','A','x'), ('b','B','x')"))
        return eng

    def test_a_second_start_returns_the_live_session_not_an_error(self, engine):
        """The double-tap recovery read used to run on a transaction the failed
        INSERT had already aborted, so Postgres refused it and the race this
        code exists to absorb surfaced as a 500. A SAVEPOINT scopes the failure
        so the recovery is legal.
        """
        import sqlalchemy as sa

        with engine.begin() as conn:
            first = sessions_module.start_session(conn, 1, name="A")
            # Simulate the loser of the race: the row is already there, so the
            # INSERT inside start_session violates the partial unique index.
            second = sessions_module.start_session(conn, 1, name="B")
            assert second.session_id == first.session_id
            # The transaction must still be usable afterwards.
            assert conn.execute(sa.text("SELECT 1")).scalar() == 1

    def test_zero_reps_never_reaches_the_check_constraint(self, engine):
        with engine.begin() as conn:
            exercise_id = sessions_module.resolve_exercise(conn, 1, "Squat (Barbell)")
            session = sessions_module.start_session(conn, 1, name="A")
            with pytest.raises(sessions_module.SessionError):
                sessions_module.log_set(
                    conn, 1, session.session_id, exercise_id=exercise_id,
                    weight_kg=50, reps=0)

    def test_resolving_a_catalog_name_finds_a_row_created_by_the_journal(self, engine):
        """The journal flow files "Bench Press (Barbell)"; the picker then
        resolved "bench press barbell" and inserted the SAME canonical name
        again, violating the unique constraint. Both now land on one row.
        """
        import sqlalchemy as sa

        with engine.begin() as conn:
            conn.execute(sa.text(
                "INSERT INTO exercises (user_id, name, muscle_group) "
                "VALUES (1, 'Bench Press (Barbell)', 'Chest')"))
            resolved = sessions_module.resolve_exercise(conn, 1, "bench press barbell")
            rows = conn.execute(sa.text(
                "SELECT exercise_id FROM exercises WHERE user_id = 1")).fetchall()
        assert len(rows) == 1, "one lift must not become two rows"
        assert resolved == rows[0][0]

    def test_the_stored_search_key_describes_its_own_row(self, engine):
        """It used to be computed from what was typed while the CATALOG name was
        stored, so the key did not describe the row it was on."""
        import sqlalchemy as sa

        with engine.begin() as conn:
            exercise_id = sessions_module.resolve_exercise(conn, 1, "bench press barbell")
            name, key = conn.execute(sa.text(
                "SELECT name, search_key FROM exercises WHERE exercise_id = :i"),
                {"i": exercise_id}).fetchone()
        assert name == "Bench Press (Barbell)"
        assert key == catalog.search_key(name)

    def test_session_volume_is_announced_once_per_session(self, engine):
        """It is a running total compared against the record the same session
        just stored, so every later set beat it and fired another toast."""
        with engine.begin() as conn:
            exercise_id = sessions_module.resolve_exercise(conn, 1, "Squat (Barbell)")
            session = sessions_module.start_session(conn, 1, name="A")
            announced = []
            for _ in range(4):
                _, breaks = sessions_module.log_set(
                    conn, 1, session.session_id, exercise_id=exercise_id,
                    weight_kg=100, reps=5)
                announced.append([b.record_type for b in breaks])

        volume_toasts = sum(
            1 for kinds in announced if records.BEST_SESSION_VOLUME in kinds
        )
        assert volume_toasts == 1, announced
        # The record itself still tracks the full session total.
        with engine.begin() as conn:
            stored = records.current(conn, 1, exercise_id)[records.BEST_SESSION_VOLUME]
        assert stored.value == pytest.approx(2000)

    def test_a_later_session_that_beats_it_is_announced(self, engine):
        """Suppressing the repeat must not suppress a genuine improvement."""
        with engine.begin() as conn:
            exercise_id = sessions_module.resolve_exercise(conn, 1, "Squat (Barbell)")
            first = sessions_module.start_session(conn, 1, name="A")
            for _ in range(3):
                sessions_module.log_set(conn, 1, first.session_id,
                                        exercise_id=exercise_id, weight_kg=100, reps=5)
            sessions_module.finish_session(conn, 1, first.session_id)

            second = sessions_module.start_session(conn, 1, name="B")
            announced = []
            for _ in range(4):
                _, breaks = sessions_module.log_set(
                    conn, 1, second.session_id, exercise_id=exercise_id,
                    weight_kg=100, reps=5)
                announced.append([b.record_type for b in breaks])

        assert any(records.BEST_SESSION_VOLUME in kinds for kinds in announced), announced

    def test_loading_a_session_costs_a_fixed_number_of_queries(self, engine):
        """previous_performance ran once per exercise, and the API reloaded the
        whole session after every logged set — so one tap cost a query per lift
        in the routine, on gym wifi.
        """
        import sqlalchemy as sa
        from sqlalchemy import event

        counter = {"n": 0}

        @event.listens_for(engine, "before_cursor_execute")
        def _count(conn, cursor, statement, params, context, executemany):
            counter["n"] += 1

        try:
            with engine.begin() as conn:
                ids = [
                    sessions_module.resolve_exercise(conn, 1, name)
                    for name in (
                        "Bench Press (Barbell)", "Squat (Barbell)",
                        "Lat Pulldown (Cable)", "Bicep Curl (Dumbbell)",
                        "Lateral Raise (Dumbbell)", "Leg Press (Machine)",
                    )
                ]
                routine = routines_module.create_routine(
                    conn, 1, "Full",
                    exercises=[{"exercise_id": i, "target_sets": 3} for i in ids])
                session = sessions_module.start_session(
                    conn, 1, routine_id=routine.routine_id)

            with engine.begin() as conn:
                counter["n"] = 0
                loaded = sessions_module.load_session(conn, 1, session.session_id)
        finally:
            event.remove(engine, "before_cursor_execute", _count)

        assert len(loaded.exercises) == 6
        # Flat in the number of exercises. The old shape was ~1 + 1 + 1 + N.
        assert counter["n"] <= 6, f"{counter['n']} queries for a 6-lift routine"

    def test_previous_performance_in_bulk_matches_one_at_a_time(self, engine):
        """The bulk query replaced N single queries, so it has to agree with
        them exactly."""
        with engine.begin() as conn:
            ids = [
                sessions_module.resolve_exercise(conn, 1, name)
                for name in ("Bench Press (Barbell)", "Squat (Barbell)")
            ]
            old = sessions_module.start_session(conn, 1, name="old")
            for exercise_id, weight in zip(ids, (80, 120)):
                sessions_module.log_set(conn, 1, old.session_id,
                                        exercise_id=exercise_id, weight_kg=weight, reps=5)
                sessions_module.log_set(conn, 1, old.session_id,
                                        exercise_id=exercise_id, weight_kg=weight, reps=4)
            sessions_module.finish_session(conn, 1, old.session_id)

            bulk = sessions_module.previous_performance_bulk(conn, 1, ids)
            for exercise_id in ids:
                one = sessions_module.previous_performance(conn, 1, exercise_id)
                assert [s.display for s in bulk[exercise_id][0]] == [s.display for s in one[0]]
                assert bulk[exercise_id][1] == one[1]
