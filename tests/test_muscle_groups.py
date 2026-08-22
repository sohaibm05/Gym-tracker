"""Unit tests for the static muscle-group lookup.

The table is the sole source of `exercises.muscle_group`, and that column is
written once per exercise and then never revisited, so a wrong entry is a wrong
label on every future chart and on the volume figures fed to the weekly report.
Two kinds of test here: mechanical guards that the tables are well-formed, and
resolution cases - especially the names whose muscle word contradicts the muscle
actually trained.
"""

from __future__ import annotations

import pytest

import muscle_groups
from muscle_groups import (
    CANONICAL_GROUPS,
    EXERCISE_MUSCLE_GROUPS,
    MOVEMENT_TOKENS,
    MUSCLE_TOKENS,
)
from pipeline import normalize_tokens, resolve_muscle_group


# --------------------------------------------------------------------------
# Table well-formedness
# --------------------------------------------------------------------------


class TestTablesAreWellFormed:
    """Guards that catch a bad entry at commit time rather than in the data."""

    @pytest.mark.parametrize("key", sorted(EXERCISE_MUSCLE_GROUPS))
    def test_key_is_in_normalized_singular_form(self, key):
        """A key the normalizer can never produce would silently never match.

        Lookup happens on `normalize_tokens` output, so every key must already
        be what that function emits - lowercase, punctuation-free, and
        singularized by the crude rule that turns "raises" into "raise" but
        leaves "press" alone.
        """
        assert " ".join(normalize_tokens(key)) == key

    @pytest.mark.parametrize("table_name", ["EXERCISE_MUSCLE_GROUPS", "MUSCLE_TOKENS",
                                            "MOVEMENT_TOKENS"])
    def test_values_are_canonical_groups(self, table_name):
        table = getattr(muscle_groups, table_name)
        unknown = {value for value in table.values() if value not in CANONICAL_GROUPS}
        assert unknown == set()

    def test_token_tables_are_single_words(self):
        """Both token tables are matched one token at a time."""
        for table in (MUSCLE_TOKENS, MOVEMENT_TOKENS, muscle_groups.EQUIPMENT_TOKENS):
            assert all(" " not in token for token in table)

    def test_ambiguous_verbs_are_not_movement_tokens(self):
        """"press" and "raise" span chest, shoulders and legs.

        Either one as a fallback would mislabel roughly a third of the lifts it
        caught, so they are left out and their names carried by the table.
        """
        assert "press" not in MOVEMENT_TOKENS
        assert "raise" not in MOVEMENT_TOKENS


# --------------------------------------------------------------------------
# Resolution
# --------------------------------------------------------------------------


class TestResolvesKnownExercises:
    @pytest.mark.parametrize(
        "name,expected",
        [
            ("Bench Press", "Chest"),
            ("Chest Bench Press", "Chest"),
            ("Lat Pulldown", "Back"),
            ("Barbell Row", "Back"),
            ("Shoulder Press", "Shoulders"),
            ("Lateral Raise", "Shoulders"),
            ("Preacher Curl", "Biceps"),
            ("Skullcrusher", "Triceps"),
            ("Leg Press", "Legs"),
            ("Hip Thrust", "Glutes"),
            ("Plank", "Core"),
            ("Wrist Curl", "Forearms"),
            ("Power Clean", "Full Body"),
        ],
    )
    def test_table_hit(self, name, expected):
        assert resolve_muscle_group(name) == expected

    @pytest.mark.parametrize(
        "name,expected",
        [
            # Short plurals survive `_singularize` intact - it only strips a
            # trailing "s" past three characters, so "ups" never becomes "up".
            # These regressed once and are the reason both spellings are listed.
            ("Push Ups", "Chest"),
            ("Pull Ups", "Back"),
            ("Chin Ups", "Back"),
            ("Sit Ups", "Core"),
            ("Step Ups", "Legs"),
            ("Abs Crunch", "Core"),
            ("Lateral Raises", "Shoulders"),
            ("Barbell Rows", "Back"),
            ("Bicep Curls", "Biceps"),
            ("Crunches", "Core"),
            ("Chest Flies", "Chest"),
            ("Cable Flys", "Chest"),
        ],
    )
    def test_plural_forms_resolve(self, name, expected):
        """The singularizer is crude, so plurals are covered by real cases."""
        assert resolve_muscle_group(name) == expected


class TestEquipmentIsStripped:
    """Equipment prefixes must not require their own table entry."""

    @pytest.mark.parametrize(
        "name,expected",
        [
            ("Dumbbell Bench Press", "Chest"),
            ("Dumbell Bench Press", "Chest"),        # the common typo
            ("Machine Chest Press", "Chest"),
            ("Seated Cable Row", "Back"),
            ("EZ Bar Preacher Curl", "Biceps"),
            ("Lying Leg Curl", "Legs"),
            ("Standing Calf Raise", "Legs"),
            ("Seated Calf Raise", "Legs"),
            ("Single Arm Dumbbell Row", "Back"),
        ],
    )
    def test_strips_and_resolves(self, name, expected):
        assert resolve_muscle_group(name) == expected

    def test_variations_are_not_stripped(self):
        """A variation changes which muscle leads, so it must survive.

        `EQUIPMENT_TOKENS` is looser than `pipeline.QUALIFIER_TOKENS`, and this
        is the line it must not cross: "romanian" and "close grip" move the lift
        to a different primary mover.
        """
        assert resolve_muscle_group("Deadlift") == "Back"
        assert resolve_muscle_group("Romanian Deadlift") == "Legs"
        assert resolve_muscle_group("Bench Press") == "Chest"
        assert resolve_muscle_group("Close Grip Bench Press") == "Triceps"


class TestMisleadingMuscleWords:
    """The reason the explicit table is consulted before the token heuristics.

    Each of these names contains a muscle word that is not the muscle trained.
    A pure token heuristic gets every one of them wrong.
    """

    @pytest.mark.parametrize(
        "name,expected,wrong",
        [
            ("Chest Supported Row", "Back", "Chest"),
            ("Leg Raise", "Core", "Legs"),
            ("Hanging Leg Raise", "Core", "Legs"),
            ("Leg Curl", "Legs", "Biceps"),      # "curl" would say Biceps
            ("Rear Delt Row", "Shoulders", "Back"),
            ("Glute Kickback", "Glutes", "Triceps"),
        ],
    )
    def test_table_wins_over_tokens(self, name, expected, wrong):
        resolved = resolve_muscle_group(name)
        assert resolved == expected
        assert resolved != wrong


class TestTokenFallbacks:
    @pytest.mark.parametrize(
        "name,expected",
        [
            ("Chest Cable Crossover", "Chest"),   # named muscle, novel movement
            ("Tricep Rope Pushdown", "Triceps"),
            ("Lat Prayer", "Back"),
            ("Oblique Twist", "Core"),
        ],
    )
    def test_muscle_token_fallback(self, name, expected):
        assert resolve_muscle_group(name) == expected

    @pytest.mark.parametrize(
        "name,expected",
        [
            ("Meadows Row", "Back"),              # no muscle word, known verb
            ("Jefferson Curl", "Biceps"),
            ("Zercher Squat", "Legs"),
        ],
    )
    def test_movement_token_fallback(self, name, expected):
        assert resolve_muscle_group(name) == expected


class TestFailsClosed:
    """Unrecognized names stay NULL rather than being guessed at.

    `insights.volume_by_muscle_group` renders None as "Unassigned", so an
    unknown movement is visibly unlabelled instead of quietly miscounted
    against a muscle it never trained.
    """

    @pytest.mark.parametrize("name", ["Zercher Carry", "Some Machine Thing", "Wibble"])
    def test_unknown_name_is_none(self, name):
        assert resolve_muscle_group(name) is None

    @pytest.mark.parametrize("name", ["", "   ", "!!!"])
    def test_empty_name_is_none(self, name):
        assert resolve_muscle_group(name) is None

    def test_equipment_only_name_is_none(self):
        """Nothing is left after stripping, and "dumbbell" names no muscle."""
        assert resolve_muscle_group("Dumbbell") is None
