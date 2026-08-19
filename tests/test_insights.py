"""Unit tests for Stage A — the pure functions that produce every report number.

These are exactly the functions where a silent bug would emit a wrong training
recommendation without anyone noticing, so each rule branch is covered.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

import insights
from insights import (
    Recommendation,
    SetRecord,
    deload_load,
    detect_plateau,
    detect_program_stagnation,
    e1rm_series,
    epley_1rm,
    find_regularly_trained,
    next_load,
    rep_range_distribution,
    recommend_for_exercise,
    round_to_increment,
    total_volume,
    trailing_pain_streak,
    volume_by_muscle_group,
    warmup_load,
    week_start_for,
)

MONDAY = date(2026, 8, 3)  # a Monday


def make_session(
    day: date,
    weight: float,
    reps_per_set: list[int],
    *,
    exercise: str = "Bench Press",
    pain: bool = False,
    warmup_reps: int | None = None,
    muscle_group: str = "Chest",
) -> list[SetRecord]:
    records: list[SetRecord] = []
    if warmup_reps is not None:
        records.append(
            SetRecord(exercise, day, weight * 0.5, warmup_reps, is_warmup=True,
                      muscle_group=muscle_group)
        )
    for reps in reps_per_set:
        records.append(
            SetRecord(exercise, day, weight, reps, pain_flag=pain, muscle_group=muscle_group)
        )
    return records


# --------------------------------------------------------------------------
# Epley 1RM
# --------------------------------------------------------------------------


class TestEpley:
    def test_known_value(self):
        # 100kg x 10 -> 100 * (1 + 10/30) = 133.33
        assert epley_1rm(100, 10) == pytest.approx(133.333, abs=0.001)

    def test_single_rep_equals_weight(self):
        assert epley_1rm(80, 1) == pytest.approx(80 * (1 + 1 / 30))

    @pytest.mark.parametrize(
        "weight,reps",
        [(None, 10), (100, None), (None, None), (0, 10), (100, 0), (-5, 10), (100, -3)],
    )
    def test_missing_or_nonpositive_returns_none(self, weight, reps):
        assert epley_1rm(weight, reps) is None

    def test_more_reps_at_same_weight_scores_higher(self):
        assert epley_1rm(60, 12) > epley_1rm(60, 8)


# --------------------------------------------------------------------------
# Plateau detection
# --------------------------------------------------------------------------


class TestPlateauDetection:
    def test_flat_three_sessions_is_plateau(self):
        series = [(MONDAY, 100.0), (MONDAY + timedelta(3), 100.0), (MONDAY + timedelta(7), 100.0)]
        assert detect_plateau(series) is True

    def test_declining_is_plateau(self):
        series = [(MONDAY, 100.0), (MONDAY + timedelta(3), 98.0), (MONDAY + timedelta(7), 95.0)]
        assert detect_plateau(series) is True

    def test_clear_progress_is_not_plateau(self):
        series = [(MONDAY, 100.0), (MONDAY + timedelta(3), 105.0), (MONDAY + timedelta(7), 110.0)]
        assert detect_plateau(series) is False

    def test_progress_only_in_the_middle_is_not_plateau(self):
        # A real gain happened within the window, even though it regressed after.
        series = [(MONDAY, 100.0), (MONDAY + timedelta(3), 108.0), (MONDAY + timedelta(7), 101.0)]
        assert detect_plateau(series) is False

    def test_two_sessions_is_not_enough(self):
        assert detect_plateau([(MONDAY, 100.0), (MONDAY + timedelta(3), 100.0)]) is False

    def test_empty_series(self):
        assert detect_plateau([]) is False

    def test_sub_tolerance_drift_still_counts_as_flat(self):
        # +0.5% over the window is noise, not progress.
        series = [(MONDAY, 100.0), (MONDAY + timedelta(3), 100.2), (MONDAY + timedelta(7), 100.5)]
        assert detect_plateau(series, tolerance=0.01) is True

    def test_only_the_trailing_window_matters(self):
        # Old progress does not rescue three flat sessions at the end.
        series = [
            (MONDAY - timedelta(14), 80.0),
            (MONDAY - timedelta(7), 95.0),
            (MONDAY, 100.0),
            (MONDAY + timedelta(3), 100.0),
            (MONDAY + timedelta(7), 100.0),
        ]
        assert detect_plateau(series) is True

    def test_e1rm_series_skips_sessions_without_working_sets(self):
        sessions = {
            MONDAY: [SetRecord("Bench Press", MONDAY, 40, 10, is_warmup=True)],
            MONDAY + timedelta(3): [SetRecord("Bench Press", MONDAY + timedelta(3), 80, 8)],
        }
        series = e1rm_series(sessions)
        assert [d for d, _ in series] == [MONDAY + timedelta(3)]


# --------------------------------------------------------------------------
# Regularly-trained + program stagnation rollup
# --------------------------------------------------------------------------


class TestRegularlyTrained:
    def test_two_of_last_three_weeks_qualifies(self):
        records = [
            SetRecord("Bench Press", MONDAY, 60, 8),
            SetRecord("Bench Press", MONDAY - timedelta(weeks=1), 60, 8),
            SetRecord("Curl", MONDAY, 20, 10),  # one week only
        ]
        assert find_regularly_trained(records, MONDAY) == {"Bench Press"}

    def test_outside_the_lookback_window_does_not_count(self):
        records = [
            SetRecord("Squat", MONDAY, 100, 5),
            SetRecord("Squat", MONDAY - timedelta(weeks=5), 100, 5),
        ]
        assert find_regularly_trained(records, MONDAY) == set()

    def test_week_start_for_snaps_to_monday(self):
        assert week_start_for(date(2026, 8, 6)) == MONDAY  # Thursday -> Monday
        assert week_start_for(MONDAY) == MONDAY


class TestProgramStagnation:
    def test_not_checked_when_too_few_exercises(self):
        result = detect_program_stagnation(
            regularly_trained={"A", "B", "C"}, plateaued={"A", "B", "C"}, min_exercises=4
        )
        assert result["checked"] is False
        assert result["flagged"] is False
        assert result["fraction"] is None
        assert "at least 4" in result["reason"]

    def test_minority_plateaued_does_not_flag(self):
        result = detect_program_stagnation(
            regularly_trained={"A", "B", "C", "D", "E"},
            plateaued={"A", "B"},
            min_exercises=4,
            fraction_threshold=0.6,
        )
        assert result["checked"] is True
        assert result["flagged"] is False
        assert result["fraction"] == pytest.approx(0.4)

    def test_majority_plateaued_flags(self):
        result = detect_program_stagnation(
            regularly_trained={"A", "B", "C", "D", "E"},
            plateaued={"A", "B", "C", "D"},
            min_exercises=4,
            fraction_threshold=0.6,
        )
        assert result["flagged"] is True
        assert result["fraction"] == pytest.approx(0.8)

    def test_exactly_at_threshold_flags(self):
        result = detect_program_stagnation(
            regularly_trained={"A", "B", "C", "D", "E"},
            plateaued={"A", "B", "C"},
            min_exercises=4,
            fraction_threshold=0.6,
        )
        assert result["fraction"] == pytest.approx(0.6)
        assert result["flagged"] is True

    def test_plateaued_irregular_exercises_are_ignored(self):
        # "Z" is plateaued but not regularly trained, so it must not count.
        result = detect_program_stagnation(
            regularly_trained={"A", "B", "C", "D"},
            plateaued={"A", "Z"},
            min_exercises=4,
            fraction_threshold=0.6,
        )
        assert result["plateaued_count"] == 1
        assert result["flagged"] is False

    def test_no_plateaus_at_all(self):
        result = detect_program_stagnation({"A", "B", "C", "D"}, set(), min_exercises=4)
        assert result["checked"] is True
        assert result["flagged"] is False
        assert result["fraction"] == 0.0


# --------------------------------------------------------------------------
# Load arithmetic
# --------------------------------------------------------------------------


class TestLoadArithmetic:
    @pytest.mark.parametrize(
        "raw,expected", [(63.0, 62.5), (61.0, 60.0), (105.0, 105.0), (26.2, 25.0), (26.3, 27.5)]
    )
    def test_round_to_increment(self, raw, expected):
        assert round_to_increment(raw, 2.5) == expected

    def test_next_load_is_always_higher(self):
        for weight in (20, 24, 28, 60, 100, 142.5):
            assert next_load(weight, 2.5) > weight

    def test_next_load_lands_on_a_real_increment(self):
        assert next_load(100, 2.5) == 105.0
        assert next_load(60, 2.5) == 62.5

    def test_next_load_on_light_weight_uses_one_full_increment(self):
        # 5% of 20kg is 1kg, which no plate can express; one increment is correct.
        assert next_load(20, 2.5) == 22.5

    def test_deload_is_always_lower(self):
        for weight in (20, 60, 100):
            assert deload_load(weight, 2.5) < weight

    def test_deload_is_about_ten_percent(self):
        assert deload_load(100, 2.5) == 90.0

    def test_warmup_load_is_forty_to_fifty_percent(self):
        assert warmup_load(60, 2.5) == 27.5
        assert 0.4 <= warmup_load(60, 2.5) / 60 <= 0.5

    def test_warmup_load_never_zero(self):
        assert warmup_load(2.0, 2.5) > 0


# --------------------------------------------------------------------------
# Progressive-overload rule branches
# --------------------------------------------------------------------------


class TestRecommendationBranches:
    def test_increase_when_every_working_set_hits_top_of_range(self):
        sessions = {MONDAY: make_session(MONDAY, 60, [12, 12, 12], warmup_reps=10)}
        rec = recommend_for_exercise("Bench Press", sessions)
        assert rec.action == "increase"
        assert rec.current_weight_kg == 60
        assert rec.recommended_weight_kg == 62.5

    def test_warmup_sets_do_not_block_an_increase(self):
        # The warm-up is at 30kg x 10 — below the rep top — and must be ignored.
        sessions = {MONDAY: make_session(MONDAY, 60, [12, 12], warmup_reps=10)}
        assert recommend_for_exercise("Bench Press", sessions).action == "increase"

    def test_hold_when_one_working_set_is_short_of_the_top(self):
        sessions = {MONDAY: make_session(MONDAY, 60, [12, 12, 9])}
        rec = recommend_for_exercise("Bench Press", sessions)
        assert rec.action == "hold"
        assert rec.recommended_weight_kg == 60

    def test_hold_when_a_working_set_misses_the_bottom_of_the_range(self):
        sessions = {MONDAY: make_session(MONDAY, 60, [8, 6, 4])}
        rec = recommend_for_exercise("Bench Press", sessions)
        assert rec.action == "hold"
        assert "below 6 reps" in rec.reason

    def test_deload_when_plateaued_three_sessions(self):
        sessions = {}
        for offset in (0, 3, 7):
            day = MONDAY + timedelta(days=offset)
            sessions[day] = make_session(day, 60, [8, 8, 8])
        rec = recommend_for_exercise("Bench Press", sessions)
        assert rec.action == "deload_or_swap"
        assert rec.plateaued is True
        assert rec.recommended_weight_kg == 55.0

    def test_deload_wording_is_a_suggestion_not_an_instruction(self):
        sessions = {MONDAY + timedelta(d): make_session(MONDAY + timedelta(d), 60, [8, 8])
                    for d in (0, 3, 7)}
        rec = recommend_for_exercise("Bench Press", sessions)
        assert "Consider" in rec.reason

    def test_increase_outranks_plateau(self):
        # Top of the range at the same load for 3 sessions reads as a flat e1RM,
        # but the right answer is more weight, not a deload.
        sessions = {MONDAY + timedelta(d): make_session(MONDAY + timedelta(d), 60, [12, 12])
                    for d in (0, 3, 7)}
        rec = recommend_for_exercise("Bench Press", sessions)
        assert rec.plateaued is True
        assert rec.action == "increase"
        assert rec.recommended_weight_kg == 62.5

    def test_insufficient_data_when_no_sessions(self):
        assert recommend_for_exercise("Bench Press", {}).action == "insufficient_data"

    def test_insufficient_data_when_last_session_is_all_warmups(self):
        sessions = {MONDAY: [SetRecord("Bench Press", MONDAY, 40, 10, is_warmup=True)]}
        assert recommend_for_exercise("Bench Press", sessions).action == "insufficient_data"

    def test_current_weight_is_the_heaviest_working_set(self):
        records = [
            SetRecord("Bench Press", MONDAY, 50, 12),
            SetRecord("Bench Press", MONDAY, 60, 12),
        ]
        rec = recommend_for_exercise("Bench Press", {MONDAY: records})
        assert rec.current_weight_kg == 60


# --------------------------------------------------------------------------
# Pain safeguard
# --------------------------------------------------------------------------


class TestPainSafeguard:
    def _painful_sessions(self, pain_on_sessions: set[int], weight: float = 60):
        sessions = {}
        for index, offset in enumerate((0, 3, 7)):
            day = MONDAY + timedelta(days=offset)
            sessions[day] = make_session(
                day, weight, [12, 12, 12], pain=index in pain_on_sessions
            )
        return sessions

    def test_pain_blocks_an_otherwise_earned_increase(self):
        sessions = self._painful_sessions({2})
        rec = recommend_for_exercise("Bench Press", sessions, pain_safeguard_enabled=True)
        assert rec.action == "hold_pain"
        assert rec.pain_safeguard_applied is True
        assert rec.recommended_weight_kg == rec.current_weight_kg == 60

    def test_pain_safeguard_adds_one_light_warmup_set(self):
        rec = recommend_for_exercise(
            "Bench Press", self._painful_sessions({2}), pain_safeguard_enabled=True
        )
        assert rec.warmup_set_kg == 27.5
        assert 0.4 <= rec.warmup_set_kg / rec.current_weight_kg <= 0.5

    def test_disabling_the_safeguard_restores_the_normal_recommendation(self):
        sessions = self._painful_sessions({2})
        rec = recommend_for_exercise("Bench Press", sessions, pain_safeguard_enabled=False)
        assert rec.action == "increase"
        assert rec.recommended_weight_kg == 62.5
        assert rec.pain_safeguard_applied is False
        assert rec.warmup_set_kg is None

    def test_pain_in_the_session_before_last_still_counts(self):
        # "last 2 sessions", not "the last session".
        rec = recommend_for_exercise("Bench Press", self._painful_sessions({1}))
        assert rec.action == "hold_pain"

    def test_pain_three_sessions_ago_does_not_count(self):
        rec = recommend_for_exercise("Bench Press", self._painful_sessions({0}))
        assert rec.action == "increase"

    def test_no_escalation_at_two_consecutive_pain_sessions(self):
        rec = recommend_for_exercise("Bench Press", self._painful_sessions({1, 2}))
        assert rec.pain_safeguard_applied is True
        assert rec.escalate_pain_to_professional is False

    def test_escalates_at_three_consecutive_pain_sessions(self):
        rec = recommend_for_exercise("Bench Press", self._painful_sessions({0, 1, 2}))
        assert rec.escalate_pain_to_professional is True
        assert "professional" in rec.pain_note

    def test_pain_note_never_diagnoses_or_prescribes_rehab(self):
        rec = recommend_for_exercise("Bench Press", self._painful_sessions({0, 1, 2}))
        text = f"{rec.pain_note} {rec.reason}".lower()
        for forbidden in ("rotator cuff", "rehab", "stretch", "strain", "tendon",
                          "impingement", "mobility", "corrective"):
            assert forbidden not in text

    def test_pain_takes_precedence_over_plateau(self):
        sessions = {}
        for offset in (0, 3, 7):
            day = MONDAY + timedelta(days=offset)
            sessions[day] = make_session(day, 60, [8, 8, 8], pain=offset == 7)
        rec = recommend_for_exercise("Bench Press", sessions)
        assert rec.action == "hold_pain"
        assert rec.plateaued is True
        assert rec.recommended_weight_kg == 60  # not deloaded

    def test_trailing_pain_streak_counts_only_consecutive_recent_sessions(self):
        sessions = self._painful_sessions({0, 2})
        assert trailing_pain_streak(sessions) == 1

    def test_module_default_is_on(self):
        assert insights.PAIN_SAFEGUARD_ENABLED is True


# --------------------------------------------------------------------------
# Aggregates
# --------------------------------------------------------------------------


class TestAggregates:
    def test_rep_range_distribution_bands(self):
        records = [
            SetRecord("A", MONDAY, 100, 3),   # strength
            SetRecord("A", MONDAY, 100, 5),   # strength
            SetRecord("A", MONDAY, 60, 6),    # hypertrophy
            SetRecord("A", MONDAY, 60, 12),   # hypertrophy (boundary)
            SetRecord("A", MONDAY, 30, 15),   # endurance
        ]
        assert rep_range_distribution(records) == {
            "strength": 2, "hypertrophy": 2, "endurance": 1
        }

    def test_warmups_excluded_from_distribution_and_volume(self):
        records = [
            SetRecord("A", MONDAY, 20, 20, is_warmup=True),
            SetRecord("A", MONDAY, 60, 10),
        ]
        assert rep_range_distribution(records) == {"strength": 0, "hypertrophy": 1, "endurance": 0}
        assert total_volume(records) == 600.0

    def test_volume_by_muscle_group_rolls_up(self):
        records = [
            SetRecord("Bench", MONDAY, 60, 10, muscle_group="Chest"),
            SetRecord("Fly", MONDAY, 15, 12, muscle_group="Chest"),
            SetRecord("Row", MONDAY, 50, 10, muscle_group="Back"),
        ]
        assert volume_by_muscle_group(records) == {"Chest": 780.0, "Back": 500.0}

    def test_incomplete_sets_contribute_no_volume(self):
        records = [SetRecord("A", MONDAY, None, 10), SetRecord("A", MONDAY, 60, None)]
        assert total_volume(records) == 0.0


# --------------------------------------------------------------------------
# Stage A end-to-end shape (still no LLM, no database)
# --------------------------------------------------------------------------


class TestBuildStageA:
    def _program(self, plateaued: bool):
        """Five exercises, three sessions each — flat if plateaued, rising if not."""
        records: list[SetRecord] = []
        names = ["Bench Press", "Squat", "Row", "Overhead Press", "Deadlift"]
        for name in names:
            for index, offset in enumerate((-14, -7, 0)):
                day = MONDAY + timedelta(days=offset)
                weight = 60 if plateaued else 60 + index * 5
                records.extend(make_session(day, weight, [8, 8], exercise=name,
                                            muscle_group="Full Body"))
        return records

    def test_flags_program_stagnation_when_all_regulars_are_flat(self):
        stage_a = insights.build_stage_a(self._program(plateaued=True), MONDAY)
        assert stage_a["program_note"]["stagnation_flagged"] is True
        assert stage_a["program_note"]["suggestion"] is not None

    def test_does_not_flag_when_everyone_is_progressing(self):
        stage_a = insights.build_stage_a(self._program(plateaued=False), MONDAY)
        assert stage_a["program_note"]["stagnation_flagged"] is False
        assert stage_a["program_note"]["suggestion"] is None

    def test_does_not_flag_with_too_few_exercises(self):
        records = [r for r in self._program(plateaued=True)
                   if r.exercise_name in {"Bench Press", "Squat"}]
        stage_a = insights.build_stage_a(records, MONDAY)
        assert stage_a["program_note"]["detail"]["checked"] is False
        assert stage_a["program_note"]["stagnation_flagged"] is False

    def test_program_suggestion_names_no_specific_program(self):
        stage_a = insights.build_stage_a(self._program(plateaued=True), MONDAY)
        suggestion = stage_a["program_note"]["suggestion"].lower()
        for forbidden in ("push pull legs", "ppl", "5x5", "starting strength",
                          "upper lower", "bro split", "531"):
            assert forbidden not in suggestion

    def test_every_recommendation_carries_a_computed_weight(self):
        stage_a = insights.build_stage_a(self._program(plateaued=False), MONDAY)
        assert stage_a["recommendations"]
        for rec in stage_a["recommendations"].values():
            assert rec["recommended_weight_kg"] is not None
            assert isinstance(rec["recommended_weight_kg"], (int, float))

    def test_fallback_summary_renders_without_an_llm(self):
        stage_a = insights.build_stage_a(self._program(plateaued=True), MONDAY)
        summary = insights.fallback_summary(stage_a)
        assert "Week of 2026-08-03" in summary
        assert "Program note" in summary


# --------------------------------------------------------------------------
# Stage B contract: the LLM narrates, it never computes
# --------------------------------------------------------------------------


class FakeNarrationClient:
    def __init__(self, reply="## What improved\nSomething."):
        self.reply = reply
        self.calls: list[dict] = []
        outer = self

        class Completions:
            def create(self, **kwargs):
                outer.calls.append(kwargs)
                if isinstance(outer.reply, Exception):
                    raise outer.reply
                return type("R", (), {"choices": [
                    type("C", (), {"message": type("M", (), {"content": outer.reply})()})()
                ]})()

        self.chat = type("Chat", (), {"completions": Completions()})()


class TestStageBContract:
    def _stage_a(self):
        records = [
            SetRecord("Bench Press", MONDAY, 60, 12, muscle_group="Chest"),
            SetRecord("Bench Press", MONDAY, 60, 12, muscle_group="Chest"),
        ]
        return insights.build_stage_a(records, MONDAY)

    def test_stage_a_numbers_are_handed_to_the_model_as_input(self):
        client = FakeNarrationClient()
        insights.narrate(self._stage_a(), client=client)
        user_message = client.calls[0]["messages"][1]["content"]
        assert '"recommended_weight_kg": 62.5' in user_message

    def test_prompt_forbids_altering_numbers(self):
        client = FakeNarrationClient()
        insights.narrate(self._stage_a(), client=client)
        system_prompt = client.calls[0]["messages"][0]["content"].lower()
        assert "may not alter, invent, recompute" in system_prompt
        assert "may not create, change, or second-guess a recommendation" in system_prompt

    def test_prompt_forbids_diagnosing_pain_or_prescribing_rehab(self):
        client = FakeNarrationClient()
        insights.narrate(self._stage_a(), client=client)
        system_prompt = client.calls[0]["messages"][0]["content"].lower()
        assert "never suggest a cause for the pain" in system_prompt
        assert "rehab" in system_prompt

    def test_prompt_forbids_inventing_a_replacement_program(self):
        client = FakeNarrationClient()
        insights.narrate(self._stage_a(), client=client)
        system_prompt = client.calls[0]["messages"][0]["content"].lower()
        assert "never invent a specific" in system_prompt

    def test_narration_runs_at_zero_temperature(self):
        client = FakeNarrationClient()
        insights.narrate(self._stage_a(), client=client)
        assert client.calls[0]["temperature"] == 0

    def test_fallback_summary_carries_the_same_numbers_without_any_model(self):
        stage_a = self._stage_a()
        summary = insights.fallback_summary(stage_a)
        assert "62.5kg" in summary
        assert stage_a["recommendations"]["Bench Press"]["recommended_weight_kg"] == 62.5


# --------------------------------------------------------------------------
# Cheat reps: "10 reps 3 were cheat" is a count within a set, not a set flag
# --------------------------------------------------------------------------


class TestCheatReps:
    def test_clean_reps_subtracts_cheat_reps(self):
        assert insights.clean_reps(SetRecord("Lateral Raise", MONDAY, 7.5, 10, cheat_reps=3)) == 7

    def test_defaults_to_all_reps_clean(self):
        assert insights.clean_reps(SetRecord("Lateral Raise", MONDAY, 7.5, 10)) == 10

    def test_missing_reps_stays_none(self):
        assert insights.clean_reps(SetRecord("Lateral Raise", MONDAY, 7.5, None)) is None

    def test_never_goes_negative(self):
        assert insights.clean_reps(SetRecord("X", MONDAY, 10, 5, cheat_reps=9)) == 0

    def test_e1rm_uses_clean_reps_not_total(self):
        cheated = SetRecord("Lateral Raise", MONDAY, 7.5, 10, cheat_reps=3)
        strict = SetRecord("Lateral Raise", MONDAY, 7.5, 7)
        series_cheated = e1rm_series({MONDAY: [cheated]})
        series_strict = e1rm_series({MONDAY: [strict]})
        assert series_cheated == series_strict
        assert series_cheated[0][1] == pytest.approx(9.25)

    def test_counting_cheat_reps_as_clean_would_inflate_e1rm(self):
        inflated = epley_1rm(7.5, 10)
        honest = epley_1rm(7.5, 7)
        assert inflated > honest

    def test_a_fully_cheated_set_contributes_no_e1rm(self):
        sets = {MONDAY: [SetRecord("X", MONDAY, 40, 8, cheat_reps=8)]}
        assert e1rm_series(sets) == []

    def test_volume_still_counts_every_rep_performed(self):
        # The work happened, even if some reps were assisted.
        record = SetRecord("Lateral Raise", MONDAY, 7.5, 10, cheat_reps=3)
        assert total_volume([record]) == 75.0

    def test_cheat_reps_can_flip_increase_into_hold(self):
        strict = {MONDAY: [SetRecord("Lateral Raise", MONDAY, 7.5, 12)]}
        assert recommend_for_exercise("Lateral Raise", strict).action == "increase"

        cheated = {MONDAY: [SetRecord("Lateral Raise", MONDAY, 7.5, 12, cheat_reps=3)]}
        rec = recommend_for_exercise("Lateral Raise", cheated)
        assert rec.action == "hold"
        assert rec.recommended_weight_kg == 7.5

    def test_cheat_reps_can_trip_the_missed_bottom_rule(self):
        sets = {MONDAY: [SetRecord("X", MONDAY, 40, 8, cheat_reps=4)]}  # 4 clean, below 6
        rec = recommend_for_exercise("X", sets)
        assert rec.action == "hold"
        assert "below 6 reps" in rec.reason
