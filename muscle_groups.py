"""Static exercise-name -> primary muscle group resolution.

Why static rather than asking the LLM or the user:
  * `exercises.muscle_group` is a property of the exercise, not of a log entry.
    `pipeline.get_or_create_exercise` writes it only on INSERT, so the value is
    decided once per distinct movement and then never revisited - a few dozen
    decisions over the life of the tool, not one per set.
  * The answer is a lookup, not a judgement. A table is free, instant,
    deterministic and unit-testable, which the same table asked of a model at
    `reasoning_effort=low` is not.

This module is a leaf: it deliberately imports nothing from `pipeline`, so the
name-normalization helpers stay single-sourced there and there is no import
cycle. Callers hand it already-normalized tokens; `pipeline.resolve_muscle_group`
is the public entry point that does the normalizing.

PRIMARY MOVER ONLY. `exercises.muscle_group` is a single TEXT column and the
volume chart sums over it, so an exercise is attributed to exactly one group.
A bench press is chest, triceps and front delts; recording all three would make
"volume by muscle group" stop summing to actual total volume. Secondary movers
are out of scope until the column becomes an array.

Unknown names resolve to None, which is stored as NULL and reported as
"Unassigned" by `insights.volume_by_muscle_group`. The tables are an allowlist:
they fail closed rather than guessing, matching how `pipeline.QUALIFIER_TOKENS`
treats unrecognized words.
"""

from __future__ import annotations

from typing import Optional, Sequence

# Every value the resolver may return. `insights` renders None as "Unassigned";
# it is not a group and so is not listed here.
CANONICAL_GROUPS = (
    "Chest",
    "Back",
    "Shoulders",
    "Biceps",
    "Triceps",
    "Legs",
    "Glutes",
    "Core",
    "Forearms",
    "Full Body",
)

# Tokens dropped before the second lookup pass, so "Dumbbell Bench Press" and
# "Bench Press" reach the same table entry without the table enumerating every
# equipment permutation.
#
# This is a LOOSER list than `pipeline.QUALIFIER_TOKENS`, and deliberately so.
# That set governs exercise IDENTITY, where "seated" vs "standing" must stay two
# separate rows with separate progress histories. Muscle group is a coarser
# question: seated and standing calf raises are different exercises that train
# the same muscle. Stripping a token here therefore never merges two exercises,
# it only lets them share one lookup key.
#
# Genuine movement variations stay OUT of this set. "incline", "decline",
# "front", "close grip" and "romanian" change which muscle leads the lift, so
# stripping them would produce a wrong group, not a coarser one.
EQUIPMENT_TOKENS = frozenset(
    {
        "barbell", "dumbbell", "dumbell", "db", "bb", "machine", "cable",
        "smith", "ez", "bar", "weighted", "bodyweight", "banded", "band",
        "seated", "standing", "lying", "single", "one", "alternating", "alt",
    }
)

# Explicit movement -> primary mover. Keys are normalized and singularized the
# way `pipeline.normalize_tokens` produces them: lowercase, punctuation gone,
# trailing plural "s" dropped unless the word ends in "ss" ("press" survives,
# "raises" becomes "raise", "biceps" becomes "bicep"). A test asserts every key
# is already in that form, so a hand-written plural cannot silently never match.
#
# That rule only fires on words longer than three characters, so short plurals
# come through intact - "ups" and "abs" are never reduced to "up" and "ab".
# Both spellings are therefore listed wherever a name ends in one.
#
# Entries whose name contains a MISLEADING muscle word are the important ones
# here - "chest supported row" is a back exercise and "leg raise" is a core
# exercise. The table is consulted before the token heuristics below precisely
# so these resolve correctly.
EXERCISE_MUSCLE_GROUPS: dict[str, str] = {
    # --- Chest ---
    "bench press": "Chest",
    "chest bench press": "Chest",
    "chest press": "Chest",
    "flat bench press": "Chest",
    "incline bench press": "Chest",
    "incline press": "Chest",
    "decline bench press": "Chest",
    "decline press": "Chest",
    "chest fly": "Chest",
    "chest flie": "Chest",
    "fly": "Chest",
    "flie": "Chest",
    "pec fly": "Chest",
    "pec deck": "Chest",
    "crossover": "Chest",
    "cable crossover": "Chest",
    "push up": "Chest",
    "push ups": "Chest",
    "pushup": "Chest",
    "dip": "Chest",
    "chest dip": "Chest",

    # --- Back ---
    "row": "Back",
    "bent over row": "Back",
    "barbell row": "Back",
    "pendlay row": "Back",
    "t bar row": "Back",
    "chest supported row": "Back",
    "inverted row": "Back",
    "lat pulldown": "Back",
    "pulldown": "Back",
    "straight arm pulldown": "Back",
    "pullover": "Back",
    "pull up": "Back",
    "pull ups": "Back",
    "pullup": "Back",
    "chin up": "Back",
    "chin ups": "Back",
    "chinup": "Back",
    "deadlift": "Back",
    "conventional deadlift": "Back",
    "rack pull": "Back",
    "shrug": "Back",
    "good morning": "Back",
    "back extension": "Back",
    "hyperextension": "Back",

    # --- Shoulders ---
    "shoulder press": "Shoulders",
    "overhead press": "Shoulders",
    "military press": "Shoulders",
    "arnold press": "Shoulders",
    "push press": "Shoulders",
    "lateral raise": "Shoulders",
    "side raise": "Shoulders",
    "side lateral raise": "Shoulders",
    "front raise": "Shoulders",
    "rear delt fly": "Shoulders",
    "rear delt flie": "Shoulders",
    "rear delt raise": "Shoulders",
    "rear delt row": "Shoulders",
    "reverse fly": "Shoulders",
    "reverse flie": "Shoulders",
    "reverse pec deck": "Shoulders",
    "upright row": "Shoulders",
    "face pull": "Shoulders",

    # --- Biceps ---
    "curl": "Biceps",
    "bicep curl": "Biceps",
    "hammer curl": "Biceps",
    "preacher curl": "Biceps",
    "concentration curl": "Biceps",
    "incline curl": "Biceps",
    "spider curl": "Biceps",
    "drag curl": "Biceps",
    "reverse curl": "Biceps",

    # --- Triceps ---
    "tricep extension": "Triceps",
    "overhead tricep extension": "Triceps",
    "overhead extension": "Triceps",
    "tricep pushdown": "Triceps",
    "pushdown": "Triceps",
    "rope pushdown": "Triceps",
    "tricep pressdown": "Triceps",
    "pressdown": "Triceps",
    "skullcrusher": "Triceps",
    "skull crusher": "Triceps",
    "close grip bench press": "Triceps",
    "close grip press": "Triceps",
    "tricep dip": "Triceps",
    "bench dip": "Triceps",
    "kickback": "Triceps",
    "tricep kickback": "Triceps",

    # --- Legs ---
    "squat": "Legs",
    "back squat": "Legs",
    "front squat": "Legs",
    "goblet squat": "Legs",
    "hack squat": "Legs",
    "split squat": "Legs",
    "bulgarian split squat": "Legs",
    "pistol squat": "Legs",
    "leg press": "Legs",
    "leg extension": "Legs",
    "quad extension": "Legs",
    "leg curl": "Legs",
    "hamstring curl": "Legs",
    "nordic curl": "Legs",
    "romanian deadlift": "Legs",
    "rdl": "Legs",
    "stiff leg deadlift": "Legs",
    "sumo deadlift": "Legs",
    "lunge": "Legs",
    "walking lunge": "Legs",
    "reverse lunge": "Legs",
    "step up": "Legs",
    "step ups": "Legs",
    "calf raise": "Legs",
    "calf press": "Legs",

    # --- Glutes ---
    "hip thrust": "Glutes",
    "glute bridge": "Glutes",
    "hip bridge": "Glutes",
    "glute kickback": "Glutes",
    "hip abduction": "Glutes",
    "abduction": "Glutes",
    "hip extension": "Glutes",

    # --- Core ---
    "plank": "Core",
    "side plank": "Core",
    "crunch": "Core",
    "crunche": "Core",
    "cable crunch": "Core",
    "cable crunche": "Core",
    "sit up": "Core",
    "sit ups": "Core",
    "situp": "Core",
    "leg raise": "Core",
    "hanging leg raise": "Core",
    "knee raise": "Core",
    "russian twist": "Core",
    "ab wheel": "Core",
    "ab rollout": "Core",
    "rollout": "Core",
    "mountain climber": "Core",
    "dead bug": "Core",
    "hollow hold": "Core",
    "woodchop": "Core",

    # --- Forearms ---
    "wrist curl": "Forearms",
    "reverse wrist curl": "Forearms",
    "farmer carry": "Forearms",
    "farmer carrie": "Forearms",
    "farmer walk": "Forearms",
    "wrist roller": "Forearms",

    # --- Full Body ---
    "clean": "Full Body",
    "power clean": "Full Body",
    "hang clean": "Full Body",
    "clean and jerk": "Full Body",
    "snatch": "Full Body",
    "thruster": "Full Body",
    "burpee": "Full Body",
    "kettlebell swing": "Full Body",
    "swing": "Full Body",
}

# A muscle named inside the exercise name. Consulted only after both table
# passes miss, so the misleading cases above ("chest supported row") never reach
# it. Tokens too vague to place - "arm", "upper", "lower" - are absent on
# purpose: no entry means no guess.
MUSCLE_TOKENS: dict[str, str] = {
    "chest": "Chest",
    "pec": "Chest",
    "back": "Back",
    "lat": "Back",
    "trap": "Back",
    "rhomboid": "Back",
    "shoulder": "Shoulders",
    "delt": "Shoulders",
    "bicep": "Biceps",
    "tricep": "Triceps",
    "leg": "Legs",
    "quad": "Legs",
    "hamstring": "Legs",
    "ham": "Legs",
    "calf": "Legs",
    "calve": "Legs",
    "glute": "Glutes",
    "core": "Core",
    "ab": "Core",
    "abs": "Core",
    "oblique": "Core",
    "forearm": "Forearms",
    "wrist": "Forearms",
}

# Last resort: the movement verb, for names the table has never seen and that
# name no muscle ("Cable Rope Pushdown"). Only verbs with one plausible primary
# mover appear; "press" and "raise" are ambiguous across chest, shoulders and
# legs, so they are deliberately absent.
MOVEMENT_TOKENS: dict[str, str] = {
    "pulldown": "Back",
    "row": "Back",
    "deadlift": "Back",
    "shrug": "Back",
    "curl": "Biceps",
    "pushdown": "Triceps",
    "pressdown": "Triceps",
    "skullcrusher": "Triceps",
    "squat": "Legs",
    "lunge": "Legs",
    "thrust": "Glutes",
    "plank": "Core",
    "crunch": "Core",
    "crunche": "Core",
}


def group_for_tokens(tokens: Sequence[str]) -> Optional[str]:
    """Primary muscle group for already-normalized, singularized name tokens.

    Four passes, most specific first:
      1. The full name against the explicit table.
      2. The name with equipment tokens removed, against the same table.
      3. A muscle named in the name.
      4. The movement verb.

    Returns None when nothing matches, which is stored as NULL rather than a
    guess. `pipeline.resolve_muscle_group` is the entry point that normalizes.
    """
    if not tokens:
        return None

    exact = EXERCISE_MUSCLE_GROUPS.get(" ".join(tokens))
    if exact is not None:
        return exact

    stripped = [token for token in tokens if token not in EQUIPMENT_TOKENS]
    if stripped and stripped != list(tokens):
        reduced = EXERCISE_MUSCLE_GROUPS.get(" ".join(stripped))
        if reduced is not None:
            return reduced

    # Rightmost muscle word wins: in "Cable Chest Fly" the qualifier sits left of
    # the movement, and in a compound like "Leg Press Calf Raise" the later word
    # is the one being trained.
    for token in reversed(stripped or list(tokens)):
        group = MUSCLE_TOKENS.get(token)
        if group is not None:
            return group

    for token in reversed(stripped or list(tokens)):
        group = MOVEMENT_TOKENS.get(token)
        if group is not None:
            return group

    return None
