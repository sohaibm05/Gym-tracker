"""Unit tests for pipeline.py's pure logic — confidence heuristic and fuzzy match.

Both are pure functions with no network or database, and both are places where a
silent bug corrupts the dataset quietly: a bad confidence score inserts junk, a
bad fuzzy match fragments an exercise's progress history across several rows.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import date, datetime, timedelta, timezone

import pytest

import pipeline
from pipeline import (
    BodyweightEntry,
    ExtractionError,
    WorkoutSet,
    compute_confidence,
    extract_entities,
    find_matching_exercise,
    name_match_score,
    normalize_name,
    resolve_logged_at,
    validate_extraction,
)

RAW_ENTRY = (
    "Chest bench press 20kg 12 reps warm up. Chest bench press 24kg 12 reps. "
    "6:20ish Dumbbell bench press 12.5kg each hand 11 reps. Shoulder pain noticed."
)


# --------------------------------------------------------------------------
# Name normalization
# --------------------------------------------------------------------------


class TestNormalizeName:
    def test_lowercases_and_collapses_whitespace(self):
        assert normalize_name("  Chest   Bench  Press ") == "chest bench press"

    def test_strips_punctuation(self):
        assert normalize_name("Bench Press (Chest)") == "bench press chest"

    def test_handles_empty(self):
        assert normalize_name("") == ""


# --------------------------------------------------------------------------
# Fuzzy exercise matching
# --------------------------------------------------------------------------


class TestFuzzyMatch:
    """The rule: token_sort_ratio, plus a token_set bonus only when the extra
    tokens are known equipment/muscle qualifiers. Unknown extra words fail
    closed into a separate exercise row."""

    @pytest.mark.parametrize(
        "proposed,existing",
        [
            ("ches press", "Chest Press"),              # acceptance criterion
            ("shoulder pres", "Shoulder Press"),
            ("Lateral Raise", "Lateral Raises"),
            ("Dumbell Bench Press", "Dumbbell Bench Press"),
            ("Chest Bench Press", "Bench Press (Chest)"),
            ("Barbell Chest Bench Press", "Chest Bench Press"),
        ],
    )
    def test_same_exercise_matches(self, proposed, existing):
        assert find_matching_exercise(proposed, [existing]) == existing

    @pytest.mark.parametrize(
        "proposed,existing",
        [
            ("Bench Press", "Incline Bench Press"),     # variation, not a qualifier
            ("Squat", "Front Squat"),
            ("Bench Press", "Close Grip Bench Press"),
            ("Deadlift", "Romanian Deadlift"),
            ("Bench Press", "Leg Press"),
            ("Barbell Row", "Barbell Curl"),
            ("Overhead Press", "Bench Press"),
            ("Lat Pulldown", "Leg Press"),
            ("Leg Curl", "Curl"),                       # single-token names never absorb
        ],
    )
    def test_different_exercises_do_not_match(self, proposed, existing):
        assert find_matching_exercise(proposed, [existing]) is None

    def test_picks_the_best_of_several_candidates(self):
        existing = ["Leg Press", "Chest Press", "Overhead Press"]
        assert find_matching_exercise("ches press", existing) == "Chest Press"

    def test_returns_none_against_an_empty_table(self):
        assert find_matching_exercise("Bench Press", []) is None

    def test_identical_name_scores_full(self):
        assert name_match_score("Bench Press", "bench press") == 100.0

    def test_threshold_is_respected(self):
        # Same pair, above and below the cut.
        assert find_matching_exercise("Bench Press", ["Incline Bench Press"], threshold=70) is not None
        assert find_matching_exercise("Bench Press", ["Incline Bench Press"], threshold=85) is None

    def test_blank_name_scores_zero(self):
        assert name_match_score("", "Bench Press") == 0.0


# --------------------------------------------------------------------------
# Confidence heuristic
# --------------------------------------------------------------------------


class TestComputeConfidence:
    def test_fully_grounded_set_scores_full(self):
        score = compute_confidence(
            "Chest Bench Press", 24.0, 12, "Chest bench press 24kg 12 reps", RAW_ENTRY
        )
        assert score == 1.0
        assert score >= pipeline.CONFIDENCE_THRESHOLD

    def test_failed_validation_scores_zero(self):
        assert compute_confidence("Bench Press", 60, 10, "Bench Press", RAW_ENTRY,
                                  validation_ok=False) == 0.0

    @pytest.mark.parametrize("name", [None, "", "   "])
    def test_missing_exercise_name_scores_zero(self, name):
        assert compute_confidence(name, 60, 10, "whatever", RAW_ENTRY) == 0.0

    def test_missing_weight_drops_below_threshold(self):
        score = compute_confidence(
            "Chest Bench Press", None, 12, "Chest bench press 12 reps", RAW_ENTRY
        )
        assert score < pipeline.CONFIDENCE_THRESHOLD

    def test_missing_reps_drops_below_threshold(self):
        score = compute_confidence(
            "Chest Bench Press", 24.0, None, "Chest bench press 24kg", RAW_ENTRY
        )
        assert score < pipeline.CONFIDENCE_THRESHOLD

    def test_missing_both_scores_lower_than_missing_one(self):
        one = compute_confidence("Chest Bench Press", 24.0, None, "Chest bench press", RAW_ENTRY)
        both = compute_confidence("Chest Bench Press", None, None, "Chest bench press", RAW_ENTRY)
        assert both < one

    def test_ungrounded_name_is_penalized(self):
        """A name that does not appear in the span it supposedly came from is the
        signal that the model invented it."""
        grounded = compute_confidence(
            "Chest Bench Press", 24.0, 12, "Chest bench press 24kg 12 reps", RAW_ENTRY
        )
        invented = compute_confidence(
            "Leg Press Machine", 24.0, 12, "Chest bench press 24kg 12 reps", RAW_ENTRY
        )
        assert invented < grounded
        assert invented < pipeline.CONFIDENCE_THRESHOLD

    def test_missing_span_is_penalized_but_still_checked_against_full_text(self):
        with_span = compute_confidence(
            "Chest Bench Press", 24.0, 12, "Chest bench press 24kg 12 reps", RAW_ENTRY
        )
        without_span = compute_confidence("Chest Bench Press", 24.0, 12, None, RAW_ENTRY)
        assert without_span < with_span
        assert without_span == pytest.approx(0.8)

    def test_typo_in_span_still_grounds_the_name(self):
        # The LLM's job is to fix "ches bent prees"; that must not look invented.
        score = compute_confidence(
            "Chest Bench Press", 24.0, 12, "ches bent prees 24kg 12 reps", RAW_ENTRY
        )
        assert score >= pipeline.CONFIDENCE_THRESHOLD

    def test_score_is_always_in_range(self):
        worst = compute_confidence("X", None, None, None, "")
        assert 0.0 <= worst <= 1.0

    def test_confidence_is_never_read_from_the_model(self):
        """A self-reported confidence field must not influence the score."""
        payload = {
            "sets": [
                {
                    "exercise_name": "Chest Bench Press",
                    "weight_kg": None,
                    "reps": None,
                    "raw_span": "Chest bench press",
                    "confidence": 0.99,  # model-supplied; must be ignored
                }
            ],
            "bodyweight": None,
        }
        scored_sets, _, _ = validate_extraction(payload, RAW_ENTRY)
        assert len(scored_sets) == 1
        assert scored_sets[0][1] < pipeline.CONFIDENCE_THRESHOLD


class TestBodyweightConfidence:
    def test_number_present_in_span_scores_full(self):
        entry = BodyweightEntry(weight_kg=82.4, raw_span="was 82.4kg this morning")
        assert pipeline.compute_bodyweight_confidence(entry, RAW_ENTRY) == 1.0

    def test_number_absent_from_text_is_penalized_below_threshold(self):
        entry = BodyweightEntry(weight_kg=75.0, raw_span="was 82.4kg this morning")
        score = pipeline.compute_bodyweight_confidence(entry, RAW_ENTRY)
        assert score < pipeline.CONFIDENCE_THRESHOLD

    def test_integer_weight_matches_integer_text(self):
        entry = BodyweightEntry(weight_kg=82.0, raw_span="weighed 82 kg today")
        assert pipeline.compute_bodyweight_confidence(entry, "weighed 82 kg today") == 1.0


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------


class TestValidateExtraction:
    def test_valid_payload_produces_scored_models(self):
        payload = {
            "sets": [
                {
                    "exercise_name": "Chest Bench Press",
                    "weight_kg": 24.0,
                    "reps": 12,
                    "set_number": 2,
                    "is_warmup": False,
                    "pain_flag": False,
                    "raw_span": "Chest bench press 24kg 12 reps",
                }
            ],
            "bodyweight": {"weight_kg": 82.4, "raw_span": "82.4kg this morning"},
        }
        scored_sets, bodyweight, review = validate_extraction(payload, RAW_ENTRY)
        assert len(scored_sets) == 1
        assert isinstance(scored_sets[0][0], WorkoutSet)
        assert bodyweight is not None and bodyweight[0].weight_kg == 82.4
        assert review == []

    def test_invalid_set_goes_to_review_not_into_the_data(self):
        payload = {"sets": [{"exercise_name": "", "reps": -4}], "bodyweight": None}
        scored_sets, _, review = validate_extraction(payload, RAW_ENTRY)
        assert scored_sets == []
        assert len(review) == 1
        assert review[0].kind == "workout_set"

    def test_one_bad_set_does_not_discard_the_good_ones(self):
        payload = {
            "sets": [
                {"exercise_name": "Bench Press", "weight_kg": 60, "reps": 10,
                 "raw_span": "Bench press 60kg 10 reps"},
                {"exercise_name": "", "reps": 0},
            ],
            "bodyweight": None,
        }
        scored_sets, _, review = validate_extraction(payload, RAW_ENTRY)
        assert len(scored_sets) == 1
        assert len(review) == 1

    def test_missing_keys_are_tolerated(self):
        scored_sets, bodyweight, review = validate_extraction({}, RAW_ENTRY)
        assert scored_sets == [] and bodyweight is None and review == []

    def test_non_list_sets_goes_to_review(self):
        _, _, review = validate_extraction({"sets": "nope"}, RAW_ENTRY)
        assert review and review[0].kind == "extraction"

    def test_bodyweight_of_wrong_type_goes_to_review(self):
        _, bodyweight, review = validate_extraction({"bodyweight": "82kg"}, RAW_ENTRY)
        assert bodyweight is None
        assert review and review[0].kind == "bodyweight"


# --------------------------------------------------------------------------
# Timestamp resolution
# --------------------------------------------------------------------------


class TestResolveLoggedAt:
    SESSION = date(2026, 8, 14)

    def test_uses_the_extracted_time_when_it_is_on_the_session_date(self):
        result = resolve_logged_at("2026-08-14T18:20:00", self.SESSION, "UTC")
        assert result == datetime(2026, 8, 14, 18, 20, tzinfo=timezone.utc)

    def test_converts_local_wall_clock_to_utc(self):
        result = resolve_logged_at("2026-08-14T18:20:00", self.SESSION, "Asia/Karachi")
        assert result == datetime(2026, 8, 14, 13, 20, tzinfo=timezone.utc)

    def test_falls_back_to_the_default_hour_when_no_time_marker(self):
        result = resolve_logged_at(None, self.SESSION, "UTC", default_hour=18)
        assert result == datetime(2026, 8, 14, 18, 0, tzinfo=timezone.utc)

    def test_a_timestamp_on_another_day_is_discarded(self):
        """The model does not get to move a workout to a different date."""
        result = resolve_logged_at("2026-08-01T09:00:00", self.SESSION, "UTC", default_hour=18)
        assert result == datetime(2026, 8, 14, 18, 0, tzinfo=timezone.utc)

    def test_unparseable_timestamp_falls_back(self):
        result = resolve_logged_at("6:20ish", self.SESSION, "UTC", default_hour=18)
        assert result == datetime(2026, 8, 14, 18, 0, tzinfo=timezone.utc)

    def test_time_only_value_is_combined_with_the_session_date(self):
        result = resolve_logged_at("18:20", self.SESSION, "UTC")
        assert result == datetime(2026, 8, 14, 18, 20, tzinfo=timezone.utc)

    def test_result_is_always_timezone_aware_utc(self):
        assert resolve_logged_at(None, self.SESSION, "UTC").tzinfo == timezone.utc


# --------------------------------------------------------------------------
# Extraction transport: JSON mode + exactly one retry
# --------------------------------------------------------------------------


class FakeCompletions:
    def __init__(self, behaviours):
        self.behaviours = list(behaviours)
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        behaviour = self.behaviours.pop(0)
        if isinstance(behaviour, Exception):
            raise behaviour
        return type(
            "Response",
            (),
            {"choices": [type("Choice", (), {"message": type("Msg", (), {"content": behaviour})()})()]},
        )()


class FakeClient:
    def __init__(self, behaviours):
        self.completions = FakeCompletions(behaviours)
        self.chat = type("Chat", (), {"completions": self.completions})()


VALID_JSON = json.dumps({"sets": [], "bodyweight": None})


class TestExtractEntities:
    def test_uses_json_mode_and_zero_temperature(self):
        client = FakeClient([VALID_JSON])
        extract_entities(RAW_ENTRY, date(2026, 8, 14), client=client)
        kwargs = client.completions.calls[0]
        assert kwargs["response_format"] == {"type": "json_object"}
        assert kwargs["temperature"] == 0

    def test_session_date_is_passed_to_the_model(self):
        client = FakeClient([VALID_JSON])
        extract_entities(RAW_ENTRY, date(2026, 8, 14), client=client)
        assert "2026-08-14" in client.completions.calls[0]["messages"][1]["content"]

    def test_retries_once_on_an_api_error_then_succeeds(self):
        client = FakeClient([RuntimeError("503 upstream"), VALID_JSON])
        assert extract_entities(RAW_ENTRY, date(2026, 8, 14), client=client) == {
            "sets": [], "bodyweight": None
        }
        assert len(client.completions.calls) == 2

    def test_retries_once_on_unparseable_json_then_succeeds(self):
        client = FakeClient(["not json at all", VALID_JSON])
        extract_entities(RAW_ENTRY, date(2026, 8, 14), client=client)
        assert len(client.completions.calls) == 2

    def test_gives_up_after_exactly_one_retry(self):
        client = FakeClient([RuntimeError("boom"), RuntimeError("boom again")])
        with pytest.raises(ExtractionError):
            extract_entities(RAW_ENTRY, date(2026, 8, 14), client=client)
        assert len(client.completions.calls) == 2

    def test_a_json_array_is_rejected_as_not_an_object(self):
        client = FakeClient(["[1, 2, 3]", "[4, 5]"])
        with pytest.raises(ExtractionError):
            extract_entities(RAW_ENTRY, date(2026, 8, 14), client=client)


# --------------------------------------------------------------------------
# Model constraints
# --------------------------------------------------------------------------


class TestModels:
    def test_exercise_name_is_whitespace_normalized(self):
        assert WorkoutSet(exercise_name="  Chest   Bench Press ").exercise_name == "Chest Bench Press"

    def test_blank_exercise_name_is_rejected(self):
        with pytest.raises(Exception):
            WorkoutSet(exercise_name="   ")

    @pytest.mark.parametrize("field,value", [("reps", 0), ("reps", -1), ("weight_kg", -5)])
    def test_impossible_values_are_rejected(self, field, value):
        with pytest.raises(Exception):
            WorkoutSet(exercise_name="Bench Press", **{field: value})

    def test_bodyweight_requires_a_weight(self):
        with pytest.raises(Exception):
            BodyweightEntry()

    def test_body_fat_pct_must_be_a_percentage(self):
        with pytest.raises(Exception):
            BodyweightEntry(weight_kg=82.4, body_fat_pct=150)


# --------------------------------------------------------------------------
# Numeric grounding (normalize_name strips decimal points, so numbers use their
# own normalizer — this is the bug that made a correct "82.4kg" look invented)
# --------------------------------------------------------------------------


class TestNumericGrounding:
    def test_decimal_weight_is_found_in_the_text(self):
        assert pipeline._number_appears_in(82.4, "was 82.4kg this morning")

    def test_integer_weight_is_found(self):
        assert pipeline._number_appears_in(82.0, "weighed 82 kg today")

    def test_does_not_match_inside_a_longer_number(self):
        # "82" must not be satisfied by the "82" inside "182.5".
        assert not pipeline._number_appears_in(82.0, "the bar was 182.5 total")

    def test_absent_number_is_not_found(self):
        assert not pipeline._number_appears_in(75.0, "was 82.4kg this morning")

    def test_normalizer_preserves_decimals(self):
        assert "82.4" in pipeline._normalize_numeric_text("Was  82.4KG this morning")


# --------------------------------------------------------------------------
# cheat_reps on the extraction model
# --------------------------------------------------------------------------


class TestCheatRepsModel:
    def test_defaults_to_zero(self):
        assert WorkoutSet(exercise_name="Lateral Raise", reps=10).cheat_reps == 0

    def test_accepts_a_count_within_the_set(self):
        s = WorkoutSet(exercise_name="Lateral Raise", weight_kg=7.5, reps=10, cheat_reps=3)
        assert s.cheat_reps == 3

    def test_rejects_more_cheat_reps_than_reps(self):
        with pytest.raises(Exception, match="cheat_reps"):
            WorkoutSet(exercise_name="Lateral Raise", reps=8, cheat_reps=9)

    def test_allows_a_fully_cheated_set(self):
        assert WorkoutSet(exercise_name="X", reps=5, cheat_reps=5).cheat_reps == 5

    def test_rejects_negative(self):
        with pytest.raises(Exception):
            WorkoutSet(exercise_name="X", reps=5, cheat_reps=-1)


# --------------------------------------------------------------------------
# Matcher gaps found on real journal entries
# --------------------------------------------------------------------------


class TestRealEntryNameMatching:
    @pytest.mark.parametrize(
        "proposed,existing",
        [
            ("Skull Crushers", "Skullcrushers"),      # word boundary only
            ("Cable Hammer Curls", "Hammer Curl"),    # qualifier + plural at once
            ("Dumbbell Press", "Dumbbell Shoulder Press"),
            ("Lateral Raises", "Lateral Raise"),
            ("Face Pull", "Face Pulls"),
        ],
    )
    def test_same_exercise_still_merges(self, proposed, existing):
        assert find_matching_exercise(proposed, [existing]) == existing

    @pytest.mark.parametrize(
        "proposed,existing",
        [
            ("Dumbbell Press", "Dumbbell Bench Press"),   # shoulder vs chest
            ("Lateral Raises", "Front Raises"),
            ("Bench Press", "Incline Bench Press"),
            ("Squat", "Front Squat"),
        ],
    )
    def test_different_exercises_still_separate(self, proposed, existing):
        assert find_matching_exercise(proposed, [existing]) is None

    def test_singularizer_leaves_double_s_words_alone(self):
        assert pipeline._singularize("press") == "press"
        assert pipeline._singularize("curls") == "curl"
        assert pipeline._singularize("raises") == "raise"
        assert pipeline._singularize("abs") == "abs"  # too short to strip


# --------------------------------------------------------------------------
# Connection pooling: serverless invocations must not retain a pool
# --------------------------------------------------------------------------


class TestEnginePooling:
    URL = "postgresql+psycopg2://u:p@localhost:55432/db"

    def test_long_running_process_uses_a_real_pool(self):
        from sqlalchemy.pool import NullPool

        engine = pipeline.get_engine(self.URL, serverless=False)
        assert not isinstance(engine.pool, NullPool)

    def test_serverless_uses_nullpool(self):
        from sqlalchemy.pool import NullPool

        engine = pipeline.get_engine(self.URL, serverless=True)
        assert isinstance(engine.pool, NullPool)

    def test_vercel_env_var_switches_it_on(self, monkeypatch):
        monkeypatch.setenv("VERCEL", "1")
        import importlib

        reloaded = importlib.reload(pipeline)
        try:
            assert reloaded.SERVERLESS is True
        finally:
            monkeypatch.delenv("VERCEL", raising=False)
            importlib.reload(pipeline)

    def test_off_by_default(self):
        assert pipeline.SERVERLESS is False

    def test_missing_url_still_raises(self, monkeypatch):
        monkeypatch.delenv("DATABASE_URL", raising=False)
        with pytest.raises(RuntimeError, match="DATABASE_URL"):
            pipeline.get_engine()


# --------------------------------------------------------------------------
# Empty environment variables must not crash the app at import
# --------------------------------------------------------------------------


class TestEnvHelper:
    """os.getenv returns "" for a variable that exists but is blank, so the
    default never applies. On Vercel that made float("") raise at import, which
    the platform reported only as an opaque function crash."""

    def test_unset_falls_back_to_the_default(self, monkeypatch):
        monkeypatch.delenv("SOME_SETTING", raising=False)
        assert pipeline.env("SOME_SETTING", "0.7") == "0.7"

    def test_empty_string_falls_back_to_the_default(self, monkeypatch):
        monkeypatch.setenv("SOME_SETTING", "")
        assert pipeline.env("SOME_SETTING", "0.7") == "0.7"

    def test_whitespace_only_falls_back_to_the_default(self, monkeypatch):
        monkeypatch.setenv("SOME_SETTING", "   ")
        assert pipeline.env("SOME_SETTING", "0.7") == "0.7"

    def test_a_real_value_wins(self, monkeypatch):
        monkeypatch.setenv("SOME_SETTING", "0.9")
        assert pipeline.env("SOME_SETTING", "0.7") == "0.9"

    def test_surrounding_whitespace_is_stripped(self, monkeypatch):
        """Dashboard fields pick up stray spaces on paste."""
        monkeypatch.setenv("SOME_SETTING", "  0.9  ")
        assert pipeline.env("SOME_SETTING", "0.7") == "0.9"

    @pytest.mark.parametrize(
        "name,default",
        [
            ("CONFIDENCE_THRESHOLD", "0.7"),
            ("FUZZY_MATCH_THRESHOLD", "85"),
            ("DEFAULT_SESSION_HOUR", "18"),
            ("DUPLICATE_WINDOW_MINUTES", "5"),
            ("PLATE_INCREMENT_KG", "2.5"),
            ("PLATEAU_TOLERANCE", "0.01"),
            ("GROQ_TPM_LIMIT", "8000"),
            ("GROQ_MAX_COMPLETION_TOKENS", "8000"),
            ("GROQ_MIN_COMPLETION_TOKENS", "1200"),
            ("GROQ_MAX_RETRY_WAIT_SECONDS", "20"),
        ],
    )
    def test_every_numeric_setting_survives_being_blank(self, name, default, monkeypatch):
        monkeypatch.setenv(name, "")
        assert float(pipeline.env(name, default)) == float(default)

    def test_modules_import_with_every_setting_blank(self, monkeypatch):
        """The regression: a blank value anywhere must not stop the app booting."""
        import importlib

        for name in (
            "CONFIDENCE_THRESHOLD", "FUZZY_MATCH_THRESHOLD", "GROQ_MODEL",
            "GROQ_TPM_LIMIT", "GROQ_MAX_COMPLETION_TOKENS",
            "GROQ_MIN_COMPLETION_TOKENS", "GROQ_MAX_RETRY_WAIT_SECONDS",
            "LOCAL_TIMEZONE", "DEFAULT_SESSION_HOUR", "DUPLICATE_WINDOW_MINUTES",
            "PLATE_INCREMENT_KG", "ANALYSIS_WEEKS", "PLATEAU_MIN_SESSIONS",
            "PLATEAU_TOLERANCE", "WORKING_REP_RANGE_LOW", "WORKING_REP_RANGE_HIGH",
            "PROGRAM_STAGNATION_MIN_EXERCISES", "PROGRAM_STAGNATION_FRACTION",
            "PAIN_SAFEGUARD_ENABLED", "LOG_LEVEL",
        ):
            monkeypatch.setenv(name, "")

        import insights

        reloaded_pipeline = importlib.reload(pipeline)
        reloaded_insights = importlib.reload(insights)
        try:
            assert reloaded_pipeline.CONFIDENCE_THRESHOLD == 0.7
            assert reloaded_pipeline.LOCAL_TIMEZONE == "UTC"
            assert reloaded_insights.PLATE_INCREMENT_KG == 2.5
            # Blank must not silently disable the safeguard.
            assert reloaded_insights.PAIN_SAFEGUARD_ENABLED is True
        finally:
            for name in list(os.environ):
                if name.startswith(("CONFIDENCE", "FUZZY", "GROQ", "LOCAL", "DEFAULT",
                                    "DUPLICATE", "PLATE", "ANALYSIS", "PLATEAU",
                                    "WORKING", "PROGRAM", "PAIN", "LOG_LEVEL")):
                    monkeypatch.delenv(name, raising=False)
            importlib.reload(pipeline)
            importlib.reload(insights)


# --------------------------------------------------------------------------
# Same-day handling: add vs replace
# --------------------------------------------------------------------------


class TestLocalDayBounds:
    """Replace deletes by a UTC window covering one LOCAL day. Getting the
    window wrong would delete a neighbouring day's rows."""

    def test_utc_day_is_midnight_to_midnight(self, monkeypatch):
        monkeypatch.setattr(pipeline, "LOCAL_TIMEZONE", "UTC")
        start, end = pipeline._local_day_bounds(date(2026, 8, 20))
        assert start == datetime(2026, 8, 20, tzinfo=timezone.utc)
        assert end == datetime(2026, 8, 21, tzinfo=timezone.utc)

    def test_offset_zone_shifts_the_window(self, monkeypatch):
        """Karachi is UTC+5, so its day runs 19:00 the previous day to 19:00."""
        monkeypatch.setattr(pipeline, "LOCAL_TIMEZONE", "Asia/Karachi")
        start, end = pipeline._local_day_bounds(date(2026, 8, 20))
        assert start == datetime(2026, 8, 19, 19, 0, tzinfo=timezone.utc)
        assert end == datetime(2026, 8, 20, 19, 0, tzinfo=timezone.utc)

    def test_window_is_exactly_one_day(self, monkeypatch):
        monkeypatch.setattr(pipeline, "LOCAL_TIMEZONE", "Asia/Karachi")
        start, end = pipeline._local_day_bounds(date(2026, 8, 20))
        assert end - start == timedelta(days=1)

    def test_consecutive_days_abut_without_overlapping(self, monkeypatch):
        """An overlap would let replace delete part of the neighbouring day."""
        monkeypatch.setattr(pipeline, "LOCAL_TIMEZONE", "Asia/Karachi")
        _, first_end = pipeline._local_day_bounds(date(2026, 8, 20))
        second_start, _ = pipeline._local_day_bounds(date(2026, 8, 21))
        assert first_end == second_start


class TestLocalToday:
    """The day boundary must come from LOCAL_TIMEZONE, not the server clock.

    Every deploy target runs its containers on UTC, so `date.today()` there is
    the UTC date - a day early for part of every local day east of Greenwich.
    """

    @staticmethod
    def _frozen(moment_utc: datetime):
        class _Frozen(datetime):
            @classmethod
            def now(cls, tz=None):
                return moment_utc.astimezone(tz) if tz else moment_utc

        return _Frozen

    def test_utc_zone_matches_the_utc_date(self, monkeypatch):
        monkeypatch.setattr(pipeline, "LOCAL_TIMEZONE", "UTC")
        monkeypatch.setattr(
            pipeline, "datetime", self._frozen(datetime(2026, 8, 21, 20, 30, tzinfo=timezone.utc))
        )
        assert pipeline.local_today() == date(2026, 8, 21)

    def test_late_evening_utc_is_already_tomorrow_in_karachi(self, monkeypatch):
        """20:30 UTC is 01:30 the next day in Karachi - the local date wins."""
        monkeypatch.setattr(pipeline, "LOCAL_TIMEZONE", "Asia/Karachi")
        monkeypatch.setattr(
            pipeline, "datetime", self._frozen(datetime(2026, 8, 21, 20, 30, tzinfo=timezone.utc))
        )
        assert pipeline.local_today() == date(2026, 8, 22)

    def test_a_sunday_night_session_is_not_filed_under_the_previous_week(self, monkeypatch):
        """The failure this exists to prevent: a whole week's misplacement.

        2026-08-23 is a Sunday. At 20:00 UTC it is already Monday in Karachi,
        so the session belongs to the week starting 2026-08-24, not 2026-08-17.
        """
        import insights

        monkeypatch.setattr(pipeline, "LOCAL_TIMEZONE", "Asia/Karachi")
        monkeypatch.setattr(
            pipeline, "datetime", self._frozen(datetime(2026, 8, 23, 20, 0, tzinfo=timezone.utc))
        )
        assert insights.week_start_for(pipeline.local_today()) == date(2026, 8, 24)

    def test_zone_is_read_at_call_time_not_bound_at_import(self, monkeypatch):
        monkeypatch.setattr(
            pipeline, "datetime", self._frozen(datetime(2026, 8, 21, 20, 30, tzinfo=timezone.utc))
        )
        monkeypatch.setattr(pipeline, "LOCAL_TIMEZONE", "UTC")
        assert pipeline.local_today() == date(2026, 8, 21)
        monkeypatch.setattr(pipeline, "LOCAL_TIMEZONE", "Asia/Karachi")
        assert pipeline.local_today() == date(2026, 8, 22)

    def test_explicit_zone_argument_overrides_the_setting(self, monkeypatch):
        monkeypatch.setattr(pipeline, "LOCAL_TIMEZONE", "UTC")
        monkeypatch.setattr(
            pipeline, "datetime", self._frozen(datetime(2026, 8, 21, 20, 30, tzinfo=timezone.utc))
        )
        assert pipeline.local_today("Asia/Karachi") == date(2026, 8, 22)


class TestReplaceIsOptIn:
    def test_process_entry_defaults_to_adding(self):
        import inspect

        signature = inspect.signature(pipeline.process_entry)
        assert signature.parameters["replace_existing"].default is False

    def test_result_reports_nothing_replaced_by_default(self):
        assert pipeline.PipelineResult().replaced is None

    def test_empty_entry_never_reports_a_replace(self):
        """Nothing to insert means nothing should have been deleted."""
        result = pipeline.process_entry("   ", date(2026, 8, 20))
        assert result.replaced is None and result.error


# --------------------------------------------------------------------------
# Extraction transport, after json_validate_failed on a reasoning model
# --------------------------------------------------------------------------


class TestJsonObjectExtraction:
    """The fallback attempt runs without JSON mode, so the reply can carry
    prose or a fenced block around the object."""

    def test_plain_object(self):
        assert pipeline.extract_json_object('{"sets": []}') == {"sets": []}

    def test_fenced_block(self):
        raw = '```json\n{"sets": [1]}\n```'
        assert pipeline.extract_json_object(raw) == {"sets": [1]}

    def test_prose_either_side(self):
        raw = 'Here is the data:\n{"sets": [2]}\nHope that helps.'
        assert pipeline.extract_json_object(raw) == {"sets": [2]}

    def test_a_brace_inside_a_string_does_not_end_the_object(self):
        raw = '{"sets": [], "note": "a } brace"}'
        assert pipeline.extract_json_object(raw)["note"] == "a } brace"

    def test_nested_objects(self):
        assert pipeline.extract_json_object('{"a": {"b": {"c": 1}}}') == {"a": {"b": {"c": 1}}}

    @pytest.mark.parametrize("value", ["", "   ", None])
    def test_empty_response_is_rejected(self, value):
        """The exact failure seen in production: the model emitted nothing."""
        with pytest.raises(ValueError, match="empty"):
            pipeline.extract_json_object(value)

    def test_no_object_is_rejected(self):
        with pytest.raises(ValueError, match="no JSON object"):
            pipeline.extract_json_object("I could not parse that")

    def test_array_is_rejected(self):
        with pytest.raises(ValueError, match="expected a JSON object"):
            pipeline.extract_json_object("[1, 2, 3]")

    def test_unterminated_object_is_rejected(self):
        with pytest.raises(ValueError, match="unterminated"):
            pipeline.extract_json_object('{"sets": [1, 2')


class TestExtractionAttemptsDiffer:
    """At temperature 0 an identical retry reproduces an identical failure, so
    the second attempt has to change something to be worth making."""

    def test_first_attempt_uses_json_mode_and_the_second_does_not(self):
        first, second = pipeline._extraction_attempts("openai/gpt-oss-120b")
        assert first["response_format"] == {"type": "json_object"}
        assert "response_format" not in second

    def test_reasoning_effort_is_sent_for_gpt_oss(self):
        """Reasoning shares the completion budget with the answer; at the
        default effort it can consume all of it and emit nothing."""
        for options in pipeline._extraction_attempts("openai/gpt-oss-120b"):
            assert options["extra_body"]["reasoning_effort"] == "low"

    def test_reasoning_effort_is_omitted_for_other_models(self):
        for options in pipeline._extraction_attempts("llama-3.3-70b-versatile"):
            assert "reasoning_effort" not in options["extra_body"]

    def test_a_completion_budget_is_always_set(self):
        for options in pipeline._extraction_attempts("openai/gpt-oss-120b"):
            assert options["extra_body"]["max_completion_tokens"] > 0

    def test_attempts_do_not_share_a_mutable_body(self):
        first, second = pipeline._extraction_attempts("openai/gpt-oss-120b")
        first["extra_body"]["max_completion_tokens"] = 1
        assert second["extra_body"]["max_completion_tokens"] != 1


class TestExtractionFallback:
    def test_falls_back_to_text_parsing_when_json_mode_fails(self):
        """json_validate_failed on the first call must not doom the entry."""
        client = FakeClient([
            RuntimeError("400 json_validate_failed ... 'failed_generation': ''"),
            'Sure:\n```json\n{"sets": [], "bodyweight": null}\n```',
        ])
        result = extract_entities(RAW_ENTRY, date(2026, 8, 14), client=client)
        assert result == {"sets": [], "bodyweight": None}
        assert len(client.completions.calls) == 2

    def test_the_second_call_really_drops_json_mode(self):
        client = FakeClient([RuntimeError("json_validate_failed"), '{"sets": []}'])
        extract_entities(RAW_ENTRY, date(2026, 8, 14), client=client)
        assert "response_format" in client.completions.calls[0]
        assert "response_format" not in client.completions.calls[1]

    # These go through the module rather than the names imported at the top of
    # this file: another test reloads pipeline, which rebinds its classes, so a
    # name captured at import time would no longer be the class actually raised.
    def test_an_empty_generation_on_both_attempts_goes_to_review(self):
        client = FakeClient(["", ""])
        with pytest.raises(pipeline.ExtractionError):
            pipeline.extract_entities(RAW_ENTRY, date(2026, 8, 14), client=client)
        assert len(client.completions.calls) == 2

    def test_still_only_two_calls_in_total(self):
        client = FakeClient([RuntimeError("a"), RuntimeError("b")])
        with pytest.raises(pipeline.ExtractionError):
            pipeline.extract_entities(RAW_ENTRY, date(2026, 8, 14), client=client)
        assert len(client.completions.calls) == 2


# --------------------------------------------------------------------------
# Token budgeting
# --------------------------------------------------------------------------


class RateLimited(Exception):
    """Stand-in for the Groq SDK's rate-limit exception.

    The real one carries the status on the exception and the delay in a header;
    older SDK versions surface neither, so both paths are exercised.
    """

    def __init__(self, message, status_code=None, headers=None):
        super().__init__(message)
        self.status_code = status_code
        if headers is not None:
            self.response = type("Response", (), {"headers": headers, "status_code": status_code})()


TPM_413 = (
    "Error code: 413 - {'error': {'message': 'Request too large for model "
    "`openai/gpt-oss-120b` in organization org_x service tier `on_demand` on tokens "
    "per minute (TPM): Limit 8000, Requested 9337, please reduce your message size "
    "and try again.', 'type': 'tokens', 'code': 'rate_limit_exceeded'}}"
)


class TestCompletionBudget:
    """The 413 regression: Groq counts the reserved completion budget toward the
    per-minute limit, so a flat 8000-token reservation on a 1300-token prompt is
    a 9337-token request against an 8000 ceiling - rejected before generation."""

    def _requested(self, text):
        session_date = date(2026, 8, 14)
        prompt = pipeline.estimate_prompt_tokens(text, session_date)
        return prompt + pipeline.completion_budget(text, prompt)

    @pytest.mark.parametrize("chars", [0, 50, 200, 400, 1000, 2000, 4000, 8000, 15000])
    def test_a_request_never_exceeds_the_minute_limit(self, chars):
        """The whole point. Any entry that can be extracted at all has to fit."""
        assert pipeline.entry_fits_in_window(
            pipeline.estimate_prompt_tokens("x" * chars, date(2026, 8, 14))
        )
        assert self._requested("x" * chars) <= pipeline.GROQ_TPM_LIMIT

    def test_an_entry_too_long_for_the_window_is_refused_before_the_call(self):
        """No reservation size rescues a prompt that fills the whole allowance,
        so say so rather than spending two calls being told."""
        enormous = "x" * 30_000
        client = FakeClient([VALID_JSON])
        with pytest.raises(pipeline.ExtractionError, match="too long"):
            pipeline.extract_entities(enormous, date(2026, 8, 14), client=client)
        assert client.completions.calls == []

    def test_the_failing_entry_now_fits(self):
        entry = (
            "Chest bench press 20kg 12 reps warm up. Chest bench press 24kg 12 reps. "
            "Chest bench press 28kg 11 reps almost died. 6:20ish Dumbbell bench press "
            "12.5kg each hand 11 reps warm up ish. Dumbell bench press 15kg each hand "
            "10 reps. Dumbbell bench press 15kg each hand 9 reps. Shoulder pain noticed "
            "toward the end. Was 82.4kg this morning btw. Finished with some cable "
            "flys, forgot the weight."
        )
        assert self._requested(entry) <= pipeline.GROQ_TPM_LIMIT

    def test_a_short_entry_reserves_far_less_than_the_flat_maximum(self):
        prompt = pipeline.estimate_prompt_tokens(RAW_ENTRY, date(2026, 8, 14))
        budget = pipeline.completion_budget(RAW_ENTRY, prompt)
        assert budget < pipeline.GROQ_MAX_COMPLETION_TOKENS / 2

    def test_a_longer_entry_reserves_more(self):
        short = pipeline.completion_budget("x" * 200, 1200)
        longer = pipeline.completion_budget("x" * 1500, 1200)
        assert longer > short

    def test_the_reservation_never_drops_below_the_floor(self):
        """Too small a budget truncates the answer, which reads downstream as a
        parse failure and hides the real cause."""
        assert pipeline.completion_budget("", 999_999) == pipeline.GROQ_MIN_COMPLETION_TOKENS

    def test_the_flat_maximum_is_still_an_upper_bound(self, monkeypatch):
        monkeypatch.setattr(pipeline, "GROQ_TPM_LIMIT", 10_000_000)
        assert pipeline.completion_budget("x" * 500_000, 100) == pipeline.GROQ_MAX_COMPLETION_TOKENS

    def test_an_ordinary_entry_does_not_warn(self, caplog):
        """The floor warning means "the limit is squeezing the answer". An entry
        that simply needs less than the floor is not that."""
        with caplog.at_level(logging.WARNING, logger="pipeline"):
            pipeline.completion_budget(RAW_ENTRY, 1200)
        assert caplog.records == []

    def test_a_squeezed_window_does_warn(self, caplog):
        with caplog.at_level(logging.WARNING, logger="pipeline"):
            pipeline.completion_budget("x" * 100, pipeline.GROQ_TPM_LIMIT - 10)
        assert any("floor" in record.getMessage() for record in caplog.records)

    def test_the_estimate_tracks_the_real_prompt_size(self):
        """Estimated ~1337 tokens is what the failing request actually reported;
        being a little over is safe, being under is what causes a 413."""
        estimated = pipeline.estimate_prompt_tokens("x" * 387, date(2026, 8, 22))
        assert 1000 <= estimated <= 1600


class TestRateLimitDetection:
    def test_a_413_is_a_rate_limit(self):
        assert pipeline.is_rate_limit_error(RateLimited(TPM_413, status_code=413))

    def test_a_429_is_a_rate_limit(self):
        assert pipeline.is_rate_limit_error(RateLimited("slow down", status_code=429))

    def test_the_message_alone_is_enough(self):
        """An older SDK surfaces the failure as a bare error with no status."""
        assert pipeline.is_rate_limit_error(RuntimeError(TPM_413))

    def test_an_unrelated_failure_is_not_a_rate_limit(self):
        assert not pipeline.is_rate_limit_error(RuntimeError("json_validate_failed"))

    def test_a_400_is_not_a_rate_limit(self):
        assert not pipeline.is_rate_limit_error(RateLimited("bad request", status_code=400))


class TestParseDuration:
    @pytest.mark.parametrize("value,expected", [
        ("7.66s", 7.66),
        ("2m59.56s", 179.56),
        ("1500ms", 1.5),
        ("30", 30.0),
        ("1h", 3600.0),
    ])
    def test_groq_duration_formats(self, value, expected):
        assert pipeline.parse_duration(value) == pytest.approx(expected)

    @pytest.mark.parametrize("value", ["", "   ", "soon"])
    def test_unparseable_values_give_none(self, value):
        assert pipeline.parse_duration(value) is None


class TestRetryAfter:
    def test_reads_the_retry_after_header(self):
        exc = RateLimited("slow down", status_code=429, headers={"retry-after": "12"})
        assert pipeline.retry_after_seconds(exc) == 12.0

    def test_falls_back_to_the_token_reset_header(self):
        exc = RateLimited("slow down", status_code=429,
                          headers={"x-ratelimit-reset-tokens": "2m59.56s"})
        assert pipeline.retry_after_seconds(exc) == pytest.approx(179.56)

    def test_reads_the_delay_out_of_the_message(self):
        exc = RuntimeError("Rate limit reached. Please try again in 7.66s.")
        assert pipeline.retry_after_seconds(exc) == pytest.approx(7.66)

    def test_a_413_carries_no_delay(self):
        """"Reduce your message size" names no time, so the caller picks one."""
        assert pipeline.retry_after_seconds(RuntimeError(TPM_413)) is None


class TestExtractionWaitsOutRateLimits:
    """A token ceiling is a condition of the clock. Retrying instantly - which
    is what the original code did - reproduces it exactly."""

    @pytest.fixture
    def slept(self, monkeypatch):
        recorded = []
        monkeypatch.setattr(pipeline, "_sleep", recorded.append)
        return recorded

    def test_it_waits_before_retrying_a_rate_limited_call(self, slept):
        client = FakeClient([RateLimited(TPM_413, status_code=413), VALID_JSON])
        pipeline.extract_entities(RAW_ENTRY, date(2026, 8, 14), client=client)
        assert len(slept) == 1 and slept[0] > 0

    def test_it_waits_as_long_as_the_server_asked(self, slept):
        client = FakeClient([
            RateLimited("try again in 7.66s", status_code=429), VALID_JSON,
        ])
        pipeline.extract_entities(RAW_ENTRY, date(2026, 8, 14), client=client)
        assert slept == [pytest.approx(7.66)]

    def test_the_wait_is_capped(self, slept):
        """A web request cannot sit blocked for three minutes."""
        client = FakeClient([
            RateLimited("try again in 2m59.56s", status_code=429), VALID_JSON,
        ])
        pipeline.extract_entities(RAW_ENTRY, date(2026, 8, 14), client=client)
        assert slept == [pipeline.GROQ_MAX_RETRY_WAIT_SECONDS]

    def test_the_retry_asks_for_a_smaller_reservation(self, slept):
        """Waiting alone cannot fix a 413: that one request was itself over the
        limit, so the retry has to ask for less."""
        entry = RAW_ENTRY * 12  # long enough to reserve well above the floor
        client = FakeClient([RateLimited(TPM_413, status_code=413), VALID_JSON])
        pipeline.extract_entities(entry, date(2026, 8, 14), client=client)
        first, second = client.completions.calls
        assert (second["extra_body"]["max_completion_tokens"]
                < first["extra_body"]["max_completion_tokens"])

    def test_the_retry_never_shrinks_below_the_floor(self, slept):
        """A short entry already sits on the floor; halving it would only
        guarantee a truncated answer on top of the rate limit."""
        client = FakeClient([RateLimited(TPM_413, status_code=413), VALID_JSON])
        pipeline.extract_entities(RAW_ENTRY, date(2026, 8, 14), client=client)
        _, second = client.completions.calls
        assert (second["extra_body"]["max_completion_tokens"]
                >= pipeline.GROQ_MIN_COMPLETION_TOKENS)

    def test_an_ordinary_failure_does_not_wait(self, slept):
        client = FakeClient([RuntimeError("json_validate_failed"), VALID_JSON])
        pipeline.extract_entities(RAW_ENTRY, date(2026, 8, 14), client=client)
        assert slept == []

    def test_an_ordinary_retry_keeps_the_full_reservation(self, slept):
        """Without JSON mode the model may wrap the object in prose, so the
        second attempt needs no less room than the first."""
        client = FakeClient([RuntimeError("json_validate_failed"), VALID_JSON])
        pipeline.extract_entities(RAW_ENTRY, date(2026, 8, 14), client=client)
        first, second = client.completions.calls
        assert (second["extra_body"]["max_completion_tokens"]
                == first["extra_body"]["max_completion_tokens"])

    def test_two_rate_limits_give_a_readable_error(self, slept):
        """The raw Groq JSON blob ended up in the review panel verbatim."""
        client = FakeClient([
            RateLimited(TPM_413, status_code=413), RateLimited(TPM_413, status_code=413),
        ])
        with pytest.raises(pipeline.ExtractionError) as caught:
            pipeline.extract_entities(RAW_ENTRY, date(2026, 8, 14), client=client)
        message = str(caught.value)
        assert "per-minute token limit" in message
        assert "Wait a minute and resubmit" in message
        assert "'error':" not in message

    def test_the_underlying_error_is_still_chained(self, slept):
        client = FakeClient([
            RateLimited(TPM_413, status_code=413), RateLimited(TPM_413, status_code=413),
        ])
        with pytest.raises(pipeline.ExtractionError) as caught:
            pipeline.extract_entities(RAW_ENTRY, date(2026, 8, 14), client=client)
        assert isinstance(caught.value.__cause__, RateLimited)

    def test_it_reports_the_limit_the_server_actually_enforced(self, slept):
        """GROQ_TPM_LIMIT is what we believe the tier allows; when the server
        names a different one, repeating our number hides the cause."""
        wrong_limit = (
            "Error code: 413 - on tokens per minute (TPM): Limit 6000, "
            "Requested 9337, please reduce your message size and try again."
        )
        client = FakeClient([
            RateLimited(wrong_limit, status_code=413),
            RateLimited(wrong_limit, status_code=413),
        ])
        with pytest.raises(pipeline.ExtractionError) as caught:
            pipeline.extract_entities(RAW_ENTRY, date(2026, 8, 14), client=client)
        assert "GROQ_TPM_LIMIT to 6000" in str(caught.value)

    def test_it_keeps_the_generic_advice_when_the_limits_agree(self, slept):
        client = FakeClient([
            RateLimited(TPM_413, status_code=413), RateLimited(TPM_413, status_code=413),
        ])
        with pytest.raises(pipeline.ExtractionError) as caught:
            pipeline.extract_entities(RAW_ENTRY, date(2026, 8, 14), client=client)
        assert "Wait a minute and resubmit" in str(caught.value)
