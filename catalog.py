"""The built-in exercise catalog: what you can pick from when building a routine.

Why this is code and not rows
-----------------------------
`exercises` is per-user and deliberately so — two people can file "Lat Pulldown"
under different muscle groups without arguing. The obvious way to give everyone
a catalog is to copy a few hundred rows into `exercises` at signup, and it is
the wrong way: it fills a person's exercise list with lifts they have never
done, and every screen that reads `exercises` (the progress charts, the weekly
report, fuzzy name matching) then has to filter out the ones with no history.

So the catalog lives here, as data in the codebase, and a row is only written to
`exercises` when somebody actually logs that lift — which `pipeline.get_or_create_exercise`
already does lazily for the journal-paste flow. The effect is that `exercises`
keeps meaning "lifts this person actually does", and the catalog is a search
index laid over it rather than a source of rows.

The shape of an entry
---------------------
An exercise is a *movement* plus the *equipment* it is done with. "Bench Press"
and "Bench Press (Dumbbell)" are different lifts with different loads and
different histories, and they must not share a progression chart. Equipment is
therefore its own field, not a suffix on the name — which is what lets the
catalog be filtered by it the way Strive's is, rather than by substring-matching
"(Barbell)" out of a string.

Most entries are generated from a compact spec: a movement, its muscle group,
and the equipment it is commonly done with. Writing every variant by hand would
be several hundred near-identical lines and would drift the moment one was
edited.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Iterable, Optional

from muscle_groups import CANONICAL_GROUPS

# --------------------------------------------------------------------------
# Equipment
# --------------------------------------------------------------------------

# The filter chips in the catalog UI, in the order they are shown. Bounded and
# small on purpose: equipment is a facet people browse by, and a long tail of
# one-off values would make it useless as a filter.
EQUIPMENT = (
    "barbell",
    "dumbbell",
    "machine",
    "cable",
    "bodyweight",
    "smith",
    "kettlebell",
    "ez_bar",
    "band",
    "other",
)

EQUIPMENT_LABELS = {
    "barbell": "Barbell",
    "dumbbell": "Dumbbell",
    "machine": "Machine",
    "cable": "Cable",
    "bodyweight": "Bodyweight",
    "smith": "Smith Machine",
    "kettlebell": "Kettlebell",
    "ez_bar": "EZ Bar",
    "band": "Band",
    "other": "Other",
}

# Equipment whose load is the person's own body, so "weight" means added weight
# and an empty weight field is a complete log rather than a missing one. The
# live logger uses this to stop marking a bodyweight set as incomplete.
BODYWEIGHT_EQUIPMENT = frozenset({"bodyweight", "band"})

# What people actually type. "db incline" is how a dumbbell incline press gets
# searched for in a gym, and matching only the full word "Dumbbell" would return
# nothing for it. The common misspellings are here for the same reason: the
# search is there to find the lift, not to grade the spelling.
EQUIPMENT_ALIASES: dict[str, tuple[str, ...]] = {
    "barbell": ("bb", "bar"),
    "dumbbell": ("db", "dbs", "dumbell", "dumbells"),
    "kettlebell": ("kb",),
    "bodyweight": ("bw", "body weight", "bodyweight"),
    "smith": ("smith machine",),
    "ez_bar": ("ez", "ez bar", "ez curl"),
    "band": ("resistance band", "bands"),
    "machine": (),
    "cable": (),
    "other": (),
}


def equipment_label(equipment: Optional[str]) -> str:
    return EQUIPMENT_LABELS.get(equipment or "", "Other")


# --------------------------------------------------------------------------
# Normalisation
# --------------------------------------------------------------------------

_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def search_key(name: str) -> str:
    """Lowercase, accent-free, punctuation-flattened form of a name.

    Matches the `exercises.search_key` column so a catalog entry and a stored
    row compare identically. Accents are stripped rather than preserved because
    somebody typing "peck deck" on a phone keyboard should still find it.
    """
    folded = unicodedata.normalize("NFKD", name or "")
    folded = "".join(ch for ch in folded if not unicodedata.combining(ch))
    return _NON_ALNUM.sub(" ", folded.lower()).strip()


@dataclass(frozen=True)
class CatalogExercise:
    """One pickable exercise."""

    name: str  # display name, e.g. "Incline Bench Press (Dumbbell)"
    movement: str  # the movement alone, e.g. "Incline Bench Press"
    equipment: str
    muscle_group: str
    aliases: tuple[str, ...] = ()

    @property
    def key(self) -> str:
        return search_key(self.name)

    @property
    def equipment_label(self) -> str:
        return equipment_label(self.equipment)

    @property
    def is_bodyweight(self) -> bool:
        return self.equipment in BODYWEIGHT_EQUIPMENT

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "movement": self.movement,
            "equipment": self.equipment,
            "equipment_label": self.equipment_label,
            "muscle_group": self.muscle_group,
            "is_bodyweight": self.is_bodyweight,
            "is_custom": False,
        }


# --------------------------------------------------------------------------
# The catalog spec
# --------------------------------------------------------------------------
#
# (movement, muscle group, equipment it is commonly done with, aliases)
#
# Equipment order matters: the first entry is the default variant, which is the
# one shown when somebody picks the movement without choosing equipment.
#
# An entry earns its place by being a lift people actually program, not by
# existing. A catalog padded with every nameable variation is harder to search,
# which defeats the point of having one.

_Spec = tuple[str, str, tuple[str, ...], tuple[str, ...]]

_SPEC: tuple[_Spec, ...] = (
    # ---- Chest ----------------------------------------------------------
    ("Bench Press", "Chest", ("barbell", "dumbbell", "smith", "machine"), ("bench", "flat bench")),
    ("Incline Bench Press", "Chest", ("barbell", "dumbbell", "smith", "machine"), ("incline press",)),
    ("Decline Bench Press", "Chest", ("barbell", "dumbbell", "smith"), ()),
    ("Close-Grip Bench Press", "Chest", ("barbell", "smith"), ("close grip bench",)),
    ("Chest Fly", "Chest", ("dumbbell", "cable", "machine"), ("fly", "flye", "pec deck")),
    ("Incline Chest Fly", "Chest", ("dumbbell", "cable"), ()),
    ("Cable Crossover", "Chest", ("cable",), ("crossover",)),
    ("Chest Press", "Chest", ("machine", "cable"), ()),
    ("Landmine Chest Press", "Chest", ("barbell",), ("landmine press",)),
    ("Push-Up", "Chest", ("bodyweight",), ("pushup", "press up")),
    ("Dip", "Chest", ("bodyweight", "machine"), ("chest dip", "dips")),
    ("Pullover", "Chest", ("dumbbell", "cable"), ()),
    # ---- Back -----------------------------------------------------------
    ("Deadlift", "Back", ("barbell",), ("conventional deadlift",)),
    ("Romanian Deadlift", "Back", ("barbell", "dumbbell"), ("rdl",)),
    ("Sumo Deadlift", "Back", ("barbell",), ()),
    ("Trap Bar Deadlift", "Back", ("barbell",), ("hex bar deadlift",)),
    ("Rack Pull", "Back", ("barbell",), ()),
    ("Bent Over Row", "Back", ("barbell", "dumbbell", "smith"), ("bent-over row", "bor")),
    ("Pendlay Row", "Back", ("barbell",), ()),
    ("Seated Cable Row", "Back", ("cable", "machine"), ("cable row", "seated row")),
    ("Single-Arm Row", "Back", ("dumbbell", "cable"), ("one arm row", "db row")),
    ("Chest-Supported Row", "Back", ("dumbbell", "machine"), ()),
    ("T-Bar Row", "Back", ("barbell", "machine"), ("t bar row",)),
    ("Lat Pulldown", "Back", ("cable", "machine"), ("pulldown",)),
    ("Close-Grip Pulldown", "Back", ("cable",), ()),
    ("Straight-Arm Pulldown", "Back", ("cable",), ()),
    ("Pull-Up", "Back", ("bodyweight", "machine"), ("pullup", "pull ups")),
    ("Chin-Up", "Back", ("bodyweight", "machine"), ("chinup",)),
    ("Face Pull", "Back", ("cable", "band"), ()),
    ("Shrug", "Back", ("barbell", "dumbbell", "cable", "smith"), ("shrugs", "trap shrug")),
    ("Back Extension", "Back", ("bodyweight", "machine"), ("hyperextension",)),
    ("Good Morning", "Back", ("barbell",), ()),
    # ---- Shoulders ------------------------------------------------------
    ("Overhead Press", "Shoulders", ("barbell", "dumbbell", "smith", "machine"),
     ("ohp", "shoulder press", "military press")),
    ("Arnold Press", "Shoulders", ("dumbbell",), ()),
    ("Seated Shoulder Press", "Shoulders", ("dumbbell", "machine"), ()),
    ("Lateral Raise", "Shoulders", ("dumbbell", "cable", "machine"), ("side raise", "lat raise")),
    ("Front Raise", "Shoulders", ("dumbbell", "cable", "barbell"), ()),
    ("Rear Delt Fly", "Shoulders", ("dumbbell", "cable", "machine"), ("reverse fly", "rear delt")),
    ("Upright Row", "Shoulders", ("barbell", "cable", "dumbbell"), ()),
    ("Push Press", "Shoulders", ("barbell", "dumbbell"), ()),
    # ---- Biceps ---------------------------------------------------------
    ("Bicep Curl", "Biceps", ("dumbbell", "barbell", "cable", "ez_bar", "machine"),
     ("curl", "biceps curl")),
    ("Hammer Curl", "Biceps", ("dumbbell", "cable"), ()),
    ("Preacher Curl", "Biceps", ("ez_bar", "dumbbell", "machine"), ()),
    ("Incline Curl", "Biceps", ("dumbbell",), ()),
    ("Concentration Curl", "Biceps", ("dumbbell",), ()),
    ("Reverse Curl", "Biceps", ("ez_bar", "barbell", "cable"), ("reverse grip curl",)),
    ("Spider Curl", "Biceps", ("dumbbell", "ez_bar"), ()),
    ("Cable Curl", "Biceps", ("cable",), ()),
    # ---- Triceps --------------------------------------------------------
    ("Tricep Pushdown", "Triceps", ("cable",), ("pushdown", "cable push down", "triceps pushdown")),
    ("Rope Pushdown", "Triceps", ("cable",), ()),
    ("Overhead Tricep Extension", "Triceps", ("cable", "dumbbell", "ez_bar"),
     ("overhead extension", "french press")),
    ("Skull Crusher", "Triceps", ("ez_bar", "barbell", "dumbbell"), ("lying tricep extension",)),
    ("Tricep Dip", "Triceps", ("bodyweight", "machine"), ()),
    ("Tricep Kickback", "Triceps", ("dumbbell", "cable"), ("kickback",)),
    ("JM Press", "Triceps", ("barbell",), ()),
    # ---- Legs -----------------------------------------------------------
    ("Squat", "Legs", ("barbell", "smith", "machine"), ("back squat",)),
    ("Front Squat", "Legs", ("barbell", "smith"), ()),
    ("Goblet Squat", "Legs", ("dumbbell", "kettlebell"), ()),
    ("Hack Squat", "Legs", ("machine",), ()),
    ("Leg Press", "Legs", ("machine",), ()),
    ("Bulgarian Split Squat", "Legs", ("dumbbell", "barbell", "smith"), ("split squat",)),
    ("Lunge", "Legs", ("dumbbell", "barbell", "bodyweight"), ("forward lunge", "lunges")),
    ("Walking Lunge", "Legs", ("dumbbell", "barbell"), ()),
    ("Reverse Lunge", "Legs", ("dumbbell", "barbell"), ()),
    ("Step-Up", "Legs", ("dumbbell", "bodyweight"), ()),
    ("Leg Extension", "Legs", ("machine",), ("quad extension",)),
    ("Leg Curl", "Legs", ("machine",), ("hamstring curl", "seated leg curl", "lying leg curl")),
    ("Nordic Curl", "Legs", ("bodyweight",), ()),
    ("Calf Raise", "Legs", ("machine", "dumbbell", "barbell", "smith"),
     ("standing calf raise", "seated calf raise")),
    ("Sissy Squat", "Legs", ("bodyweight", "machine"), ()),
    # ---- Glutes ---------------------------------------------------------
    ("Hip Thrust", "Glutes", ("barbell", "machine", "smith"), ()),
    ("Glute Bridge", "Glutes", ("barbell", "bodyweight"), ()),
    ("Cable Kickback", "Glutes", ("cable",), ("glute kickback",)),
    ("Hip Abduction", "Glutes", ("machine", "band", "cable"), ("abduction",)),
    ("Hip Adduction", "Glutes", ("machine", "cable"), ("adduction",)),
    # ---- Core -----------------------------------------------------------
    ("Plank", "Core", ("bodyweight",), ()),
    ("Hanging Leg Raise", "Core", ("bodyweight",), ("leg raise",)),
    ("Cable Crunch", "Core", ("cable",), ()),
    ("Crunch", "Core", ("bodyweight", "machine"), ("sit up", "situp")),
    ("Russian Twist", "Core", ("bodyweight", "dumbbell"), ()),
    ("Ab Wheel Rollout", "Core", ("other",), ("ab wheel",)),
    ("Pallof Press", "Core", ("cable", "band"), ()),
    ("Farmer's Carry", "Core", ("dumbbell", "kettlebell"), ("farmers walk",)),
    # ---- Forearms / other ----------------------------------------------
    ("Wrist Curl", "Forearms", ("dumbbell", "barbell", "cable"), ()),
    ("Reverse Wrist Curl", "Forearms", ("dumbbell", "barbell"), ()),
    ("Dead Hang", "Forearms", ("bodyweight",), ()),
    # ---- Olympic / full body -------------------------------------------
    ("Power Clean", "Full Body", ("barbell",), ("clean",)),
    ("Clean and Jerk", "Full Body", ("barbell",), ()),
    ("Snatch", "Full Body", ("barbell",), ()),
    ("Thruster", "Full Body", ("barbell", "dumbbell"), ()),
    ("Kettlebell Swing", "Full Body", ("kettlebell",), ("swing",)),
    ("Burpee", "Full Body", ("bodyweight",), ()),
)


def _build() -> tuple[CatalogExercise, ...]:
    """Expand the spec into one entry per (movement, equipment) pair.

    The first equipment listed is the movement's default, and its display name
    carries the equipment suffix like every other variant. Naming the default
    "Squat" and the rest "Squat (Smith Machine)" would read better and search
    worse — someone filtering by Barbell would not find the plain one.
    """
    built: list[CatalogExercise] = []
    seen: set[str] = set()
    for movement, group, equipment_options, aliases in _SPEC:
        for equipment in equipment_options:
            name = f"{movement} ({equipment_label(equipment)})"
            key = search_key(name)
            if key in seen:  # pragma: no cover - guards a duplicated spec row
                raise ValueError(f"duplicate catalog entry: {name}")
            seen.add(key)
            built.append(
                CatalogExercise(
                    name=name,
                    movement=movement,
                    equipment=equipment,
                    muscle_group=group,
                    aliases=aliases,
                )
            )
    return tuple(built)


CATALOG: tuple[CatalogExercise, ...] = _build()

# name key -> entry, for an exact lookup when resolving a logged name.
BY_KEY: dict[str, CatalogExercise] = {item.key: item for item in CATALOG}

# Every muscle group present in the catalog, in the canonical order where one
# exists so the filter chips do not reorder themselves between releases.
MUSCLE_GROUPS: tuple[str, ...] = tuple(
    [group for group in CANONICAL_GROUPS if any(e.muscle_group == group for e in CATALOG)]
    + sorted({e.muscle_group for e in CATALOG} - set(CANONICAL_GROUPS))
)


# --------------------------------------------------------------------------
# Search
# --------------------------------------------------------------------------


def _score(entry: CatalogExercise, query_key: str, tokens: list[str]) -> Optional[int]:
    """How well an entry matches, higher is better. None means no match.

    Ranked rather than filtered, because the useful ordering is not
    alphabetical: someone typing "bench" wants "Bench Press (Barbell)" first and
    "Close-Grip Bench Press (Smith Machine)" further down, and both are matches.
    """
    key = entry.key
    movement_key = search_key(entry.movement)

    if key == query_key or movement_key == query_key:
        return 100
    if movement_key.startswith(query_key):
        return 90
    if key.startswith(query_key):
        return 80
    for alias in entry.aliases:
        alias_key = search_key(alias)
        if alias_key == query_key:
            return 85
        if alias_key.startswith(query_key):
            return 70
    if query_key in key:
        return 60
    # Every query token appears somewhere. The haystack includes the equipment's
    # own aliases, which is what makes "db incline" find "Incline Bench Press
    # (Dumbbell)" — the words are in two different fields and neither alone is a
    # match.
    haystack = " ".join(
        [key]
        + [search_key(a) for a in entry.aliases]
        + [search_key(a) for a in EQUIPMENT_ALIASES.get(entry.equipment, ())]
    )
    if tokens and all(token in haystack for token in tokens):
        return 40
    return None


def search(
    query: str = "",
    equipment: Optional[str] = None,
    muscle_group: Optional[str] = None,
    limit: int = 50,
) -> list[CatalogExercise]:
    """Catalog entries matching a query and optional facets, best first.

    An empty query is a browse rather than a search, so it returns the catalog
    in its declared order — which groups movements by body part — instead of
    alphabetically, where "Ab Wheel Rollout" would greet everyone.
    """
    entries: Iterable[CatalogExercise] = CATALOG
    if equipment:
        entries = [e for e in entries if e.equipment == equipment]
    if muscle_group:
        entries = [e for e in entries if e.muscle_group.lower() == muscle_group.lower()]

    query_key = search_key(query)
    if not query_key:
        return list(entries)[:limit]

    tokens = query_key.split()
    scored: list[tuple[int, int, CatalogExercise]] = []
    for index, entry in enumerate(entries):
        score = _score(entry, query_key, tokens)
        if score is not None:
            # index as the tiebreak keeps the declared order stable within a
            # score band, so results do not shuffle between identical queries.
            scored.append((-score, index, entry))
    scored.sort()
    return [entry for _, _, entry in scored[:limit]]


def resolve(name: str) -> Optional[CatalogExercise]:
    """The catalog entry for an exact name, if there is one.

    Used when a logged name needs its equipment and muscle group filled in.
    Deliberately exact: guessing here would file somebody's set under the wrong
    lift, and the fuzzy matching in pipeline.py already handles near-misses with
    a person in the loop.
    """
    if not name:
        return None
    return BY_KEY.get(search_key(name))


def equipment_counts() -> dict[str, int]:
    """How many catalog entries each equipment filter would show."""
    counts = {equipment: 0 for equipment in EQUIPMENT}
    for entry in CATALOG:
        counts[entry.equipment] = counts.get(entry.equipment, 0) + 1
    return counts
