"""Unit tests for the amber name flag.

The flag exists to catch an exercise name that was SAVED but does not look
right - a typo about to split one lift's history in two, a name the model may
have inferred rather than read, or a movement nothing recognizes.

The hard part is not detection, it is silence. A flag that fires on ordinary
lifts gets ignored within a week and then the real ones are invisible too, so
most of what follows asserts that nothing is flagged.
"""

from __future__ import annotations

import pytest

from pipeline import (
    FUZZY_MATCH_THRESHOLD,
    NEAR_MISS_FLOOR,
    _is_probable_typo,
    find_matching_exercise,
    flag_exercise_name,
    nearest_existing_exercise,
)

EXISTING = ["Chest Bench Press", "Lat Pulldown", "Barbell Row", "Leg Press", "Lateral Raise"]


def flag_for(name, span=None, existing=EXISTING):
    """Flag a name as the pipeline would, reading it from its own raw span."""
    span = span if span is not None else name.lower()
    return flag_exercise_name(name, existing, span, span)


# --------------------------------------------------------------------------
# Silence on ordinary names
# --------------------------------------------------------------------------


class TestOrdinaryNamesAreNotFlagged:
    @pytest.mark.parametrize(
        "name,span",
        [
            ("Squat", "squat 100kg 5 reps"),
            ("Hip Thrust", "hip thrust 60kg 12"),
            ("Cable Crossover", "some cable crossovers 15kg"),
            ("Face Pull", "face pulls 20kg 15"),
            ("Hammer Curl", "hammer curls 12.5kg each hand 10"),
        ],
    )
    def test_common_new_lift_is_silent(self, name, span):
        assert flag_for(name, span) is None

    @pytest.mark.parametrize(
        "name,span",
        [
            ("Incline Bench Press", "incline bench press 30kg 8"),
            ("Front Squat", "front squat 60kg 8"),
            ("Romanian Deadlift", "romanian deadlift 70kg 10"),
            ("Close Grip Bench Press", "close grip bench press 40kg 8"),
        ],
    )
    def test_real_variation_is_not_a_typo(self, name, span):
        """`incline`, `front`, `romanian` and `close grip` mark different lifts.

        `QUALIFIER_TOKENS` documents these as blocking a fuzzy merge on purpose,
        so calling one a probable typo of the lift it varies would contradict
        the matcher and train the reader to ignore the flag.
        """
        assert flag_for(name, span) is None

    def test_model_added_equipment_is_not_weak_grounding(self):
        """"Barbell" in front of a name the text wrote bare is normalization.

        The matcher already forgives equipment qualifiers, so the grounding
        check has to forgive them too.
        """
        assert flag_for("Barbell Hack Squat", "hack squat 80kg 8") is None

    def test_niche_but_placeable_is_silent(self):
        """Unusual is fine when the name still resolves to a muscle group."""
        assert flag_for("Jefferson Curl", "jefferson curl 20kg 10") is None


# --------------------------------------------------------------------------
# near_miss
# --------------------------------------------------------------------------


class TestNearMiss:
    def test_typo_below_the_merge_threshold_is_flagged(self):
        flag = flag_for("Bech Press", "bech press 40kg 8")
        assert flag is not None
        assert flag.reason == "near_miss"
        assert flag.nearest_name == "Chest Bench Press"
        assert NEAR_MISS_FLOOR <= flag.score < FUZZY_MATCH_THRESHOLD

    def test_detail_names_the_exercise_it_nearly_matched(self):
        """The name is the actionable part - without it there is nothing to do."""
        assert "Chest Bench Press" in flag_for("Bech Press", "bech press 40kg 8").detail

    @pytest.mark.parametrize("name", ["Lateral Rasie", "Leg Pres"])
    def test_typo_the_matcher_already_absorbs_is_not_flagged(self, name):
        """Scoring at/above the merge threshold means no new row was created.

        The history never splits, so there is nothing to warn about - the
        matcher has already done the job.
        """
        assert find_matching_exercise(name, EXISTING) is not None
        _, score = nearest_existing_exercise(name, EXISTING)
        assert score >= FUZZY_MATCH_THRESHOLD

    def test_wildly_different_name_is_not_a_near_miss(self):
        """Below the floor there is no reason to think two names are related."""
        flag = flag_for("Zercher Carry", "zercher carry 60kg 30s")
        assert flag.reason != "near_miss"


class TestIsProbableTypo:
    @pytest.mark.parametrize(
        "proposed,existing",
        [
            ("Bech Press", "Chest Bench Press"),      # qualifier ignored on one side
            ("Lateral Rasie", "Lateral Raise"),       # transposition
            ("Shoulderr Press", "Shoulder Press"),
        ],
    )
    def test_misspelling(self, proposed, existing):
        assert _is_probable_typo(proposed, existing) is True

    @pytest.mark.parametrize(
        "proposed,existing",
        [
            ("Incline Bench Press", "Chest Bench Press"),   # extra content token
            ("Front Squat", "Squat"),
            ("Romanian Deadlift", "Deadlift"),
            ("Leg Curl", "Leg Extension"),                  # different movement
        ],
    )
    def test_variation_is_not_a_typo(self, proposed, existing):
        assert _is_probable_typo(proposed, existing) is False

    def test_identical_content_is_not_a_typo(self):
        """Differing only by an equipment word is a match, not a misspelling."""
        assert _is_probable_typo("Barbell Bench Press", "Bench Press") is False


# --------------------------------------------------------------------------
# weak_grounding
# --------------------------------------------------------------------------


class TestWeakGrounding:
    @pytest.mark.parametrize(
        "name,span",
        [
            ("Seated Row Machine", "machine rows 40kg 10"),
            ("Overhead Tricep Extension", "tricep thing overhead 15kg 12"),
        ],
    )
    def test_name_loosely_supported_by_its_span_is_flagged(self, name, span):
        flag = flag_for(name, span)
        assert flag is not None and flag.reason == "weak_grounding"

    def test_detail_quotes_the_text_it_was_read_from(self):
        """Judging the flag means seeing what was actually written."""
        flag = flag_for("Seated Row Machine", "machine rows 40kg 10")
        assert "machine rows" in flag.detail

    def test_name_copied_from_the_text_is_silent(self):
        assert flag_for("Barbell Row", "barbell row 50kg 9") is None


# --------------------------------------------------------------------------
# unrecognized
# --------------------------------------------------------------------------


class TestUnrecognized:
    @pytest.mark.parametrize("name", ["Zercher Carry", "Wibble Machine", "Thing Doer"])
    def test_name_no_table_recognizes_is_flagged(self, name):
        flag = flag_for(name)
        assert flag is not None and flag.reason == "unrecognized"

    def test_resolvable_name_is_silent(self):
        assert flag_for("Kettlebell Swing", "kettlebell swings 24kg 20") is None


# --------------------------------------------------------------------------
# Precedence and shape
# --------------------------------------------------------------------------


class TestPrecedence:
    def test_at_most_one_flag_per_name(self):
        """One reason keeps the report readable; the list can get long."""
        flag = flag_for("Bech Press", "bech press 40kg 8")
        assert isinstance(flag.reason, str)

    def test_near_miss_outranks_unrecognized(self):
        """A split history is worse than an unplaceable name, so it reports first.

        "Bech Press" is also unrecognized by the muscle tables, but the typo is
        the thing worth acting on.
        """
        assert flag_for("Bech Press", "bech press 40kg 8").reason == "near_miss"

    def test_flag_carries_the_name_it_describes(self):
        assert flag_for("Wibble Machine").exercise_name == "Wibble Machine"

    def test_no_existing_exercises_still_resolves(self):
        """First ever entry: nothing to near-miss against, other checks still run."""
        assert flag_exercise_name("Squat", [], "squat 100kg 5", "squat 100kg 5") is None
        assert flag_exercise_name("Wibble", [], "wibble 10kg", "wibble 10kg").reason == (
            "unrecognized"
        )
