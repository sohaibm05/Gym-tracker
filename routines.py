"""Routines: reusable workout templates and their ordered contents.

A routine is what you plan; a session is what you did. Keeping them apart is the
point — editing "Push Day" next month must not rewrite the sets you logged under
it last month, which is why a session copies the name it started with and holds
only a nullable reference back to the template.

Ownership
---------
Every function takes `user_id` and every statement names it. `routine_exercises`
has no `user_id` column of its own — it is scoped through its parent routine —
so each query against it joins to `routines` and filters there. An unscoped
query on this table would let somebody read or rewrite another account's
programme by guessing an integer, and `tests/test_multi_user.py` exists to catch
exactly that class of mistake.

Nothing here opens a connection. Callers pass one in, so a caller can wrap
several of these in one transaction — which `replace_exercises` needs, since a
half-applied routine edit is worse than a failed one.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Optional, Sequence

from sqlalchemy import text
from sqlalchemy.engine import Connection

import catalog

logger = logging.getLogger("gym_tracker.routines")

# A routine with more exercises than this is almost always a mistake — a pasted
# list, or a "routine" being used as a workout library. The cap keeps one bad
# request from writing thousands of rows.
MAX_EXERCISES_PER_ROUTINE = 60

# Default rest, used when a routine exercise does not set its own.
DEFAULT_REST_SECONDS = 120


class RoutineError(ValueError):
    """A routine could not be created or changed, with a reason for the user."""


# --------------------------------------------------------------------------
# Types
# --------------------------------------------------------------------------


@dataclass
class RoutineExercise:
    """One line of a routine: a lift, with targets."""

    exercise_id: int
    name: str
    position: int = 0
    routine_exercise_id: Optional[int] = None
    muscle_group: Optional[str] = None
    equipment: Optional[str] = None
    target_sets: Optional[int] = None
    target_reps_low: Optional[int] = None
    target_reps_high: Optional[int] = None
    target_weight_kg: Optional[float] = None
    rest_seconds: Optional[int] = None
    notes: Optional[str] = None

    @property
    def target_reps_label(self) -> str:
        """"8-12", "5", or "" — how the target reads on screen."""
        low, high = self.target_reps_low, self.target_reps_high
        if low is None and high is None:
            return ""
        if low is not None and high is not None:
            return str(low) if low == high else f"{low}-{high}"
        return str(low if low is not None else high)

    @property
    def is_bodyweight(self) -> bool:
        return (self.equipment or "") in catalog.BODYWEIGHT_EQUIPMENT

    def to_dict(self) -> dict[str, Any]:
        return {
            "routine_exercise_id": self.routine_exercise_id,
            "exercise_id": self.exercise_id,
            "name": self.name,
            "position": self.position,
            "muscle_group": self.muscle_group,
            "equipment": self.equipment,
            "equipment_label": catalog.equipment_label(self.equipment),
            "target_sets": self.target_sets,
            "target_reps_low": self.target_reps_low,
            "target_reps_high": self.target_reps_high,
            "target_reps_label": self.target_reps_label,
            "target_weight_kg": (
                float(self.target_weight_kg) if self.target_weight_kg is not None else None
            ),
            "rest_seconds": self.rest_seconds,
            "is_bodyweight": self.is_bodyweight,
            "notes": self.notes,
        }


@dataclass
class Routine:
    """A template, with its contents when they have been loaded."""

    routine_id: int
    user_id: int
    name: str
    notes: Optional[str] = None
    position: int = 0
    archived: bool = False
    exercises: list[RoutineExercise] = field(default_factory=list)

    @property
    def total_sets(self) -> int:
        """Planned working sets. What the routine list shows under the name."""
        return sum(item.target_sets or 0 for item in self.exercises)

    @property
    def muscle_groups(self) -> list[str]:
        """Distinct groups this routine trains, in the order they appear."""
        seen: list[str] = []
        for item in self.exercises:
            if item.muscle_group and item.muscle_group not in seen:
                seen.append(item.muscle_group)
        return seen

    def to_dict(self, include_exercises: bool = True) -> dict[str, Any]:
        body: dict[str, Any] = {
            "routine_id": self.routine_id,
            "name": self.name,
            "notes": self.notes,
            "position": self.position,
            "archived": self.archived,
            "exercise_count": len(self.exercises),
            "total_sets": self.total_sets,
            "muscle_groups": self.muscle_groups,
        }
        if include_exercises:
            body["exercises"] = [item.to_dict() for item in self.exercises]
        return body


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------


def clean_name(raw: str) -> str:
    """A usable routine name, or raise.

    Length-capped because the name is rendered in a list on a phone and copied
    onto every session started from it; an essay there is a display bug that
    outlives the routine.
    """
    name = " ".join((raw or "").split())
    if not name:
        raise RoutineError("A routine needs a name.")
    if len(name) > 80:
        raise RoutineError("That name is too long — keep it under 80 characters.")
    return name


def _clean_target(item: dict[str, Any]) -> dict[str, Any]:
    """Normalise one submitted routine line, or raise RoutineError.

    The database has CHECK constraints for all of this, and they are the real
    guarantee. This exists so the person gets "reps can't be 0" instead of a
    constraint violation they cannot act on.
    """

    def _int(key: str, low: int, high: int) -> Optional[int]:
        raw = item.get(key)
        if raw in (None, "", "None"):
            return None
        try:
            value = int(raw)
        except (TypeError, ValueError):
            raise RoutineError(f"{key.replace('_', ' ')} must be a whole number.") from None
        if not low <= value <= high:
            raise RoutineError(f"{key.replace('_', ' ')} must be between {low} and {high}.")
        return value

    def _float(key: str, low: float, high: float) -> Optional[float]:
        raw = item.get(key)
        if raw in (None, "", "None"):
            return None
        try:
            value = float(raw)
        except (TypeError, ValueError):
            raise RoutineError(f"{key.replace('_', ' ')} must be a number.") from None
        if not low <= value <= high:
            raise RoutineError(f"{key.replace('_', ' ')} must be between {low} and {high}.")
        return value

    low_reps = _int("target_reps_low", 1, 100)
    high_reps = _int("target_reps_high", 1, 100)
    # A single value in either field means a fixed target, not an open range.
    if low_reps is not None and high_reps is None:
        high_reps = low_reps
    elif high_reps is not None and low_reps is None:
        low_reps = high_reps
    if low_reps is not None and high_reps is not None and low_reps > high_reps:
        # Swapped rather than rejected: "12-8" is unambiguous about what was
        # meant, and refusing it would be pedantry.
        low_reps, high_reps = high_reps, low_reps

    return {
        "target_sets": _int("target_sets", 1, 50),
        "target_reps_low": low_reps,
        "target_reps_high": high_reps,
        "target_weight_kg": _float("target_weight_kg", 0, 1000),
        "rest_seconds": _int("rest_seconds", 0, 3600),
        "notes": (str(item.get("notes") or "").strip() or None),
    }


# --------------------------------------------------------------------------
# Reads
# --------------------------------------------------------------------------

_ROUTINE_COLUMNS = "routine_id, user_id, name, notes, position, archived"


def _row_to_routine(row: Any) -> Routine:
    return Routine(
        routine_id=row[0],
        user_id=row[1],
        name=row[2],
        notes=row[3],
        position=row[4],
        archived=bool(row[5]),
    )


def list_routines(
    conn: Connection, user_id: int, include_archived: bool = False
) -> list[Routine]:
    """This user's routines, in their chosen order, with contents loaded.

    Contents come back in a second query rather than a join, so a routine with
    no exercises is still a routine rather than disappearing (an inner join) or
    arriving as a row of nulls to unpick (a left join). Two queries regardless
    of how many routines there are — not one per routine.
    """
    clause = "" if include_archived else " AND archived = FALSE"
    rows = conn.execute(
        text(
            f"SELECT {_ROUTINE_COLUMNS} FROM routines "
            f"WHERE user_id = :user_id{clause} "
            f"ORDER BY position, routine_id"
        ),
        {"user_id": user_id},
    ).fetchall()
    routines = [_row_to_routine(row) for row in rows]
    if not routines:
        return []

    by_id = {routine.routine_id: routine for routine in routines}
    for item, routine_id in _load_exercises(conn, user_id, list(by_id)):
        by_id[routine_id].exercises.append(item)
    return routines


def _load_exercises(
    conn: Connection, user_id: int, routine_ids: Sequence[int]
) -> list[tuple[RoutineExercise, int]]:
    """Routine lines for several routines at once, scoped to the owner."""
    if not routine_ids:
        return []
    rows = conn.execute(
        text(
            """
            SELECT re.routine_exercise_id, re.routine_id, re.exercise_id, e.name,
                   e.muscle_group, e.equipment, re.position, re.target_sets,
                   re.target_reps_low, re.target_reps_high, re.target_weight_kg,
                   re.rest_seconds, re.notes
              FROM routine_exercises re
              JOIN routines  r ON r.routine_id  = re.routine_id
              JOIN exercises e ON e.exercise_id = re.exercise_id
             WHERE re.routine_id = ANY(:routine_ids)
               AND r.user_id = :user_id
             ORDER BY re.routine_id, re.position, re.routine_exercise_id
            """
        ),
        {"routine_ids": list(routine_ids), "user_id": user_id},
    ).fetchall()

    return [
        (
            RoutineExercise(
                routine_exercise_id=row[0],
                exercise_id=row[2],
                name=row[3],
                muscle_group=row[4],
                equipment=row[5],
                position=row[6],
                target_sets=row[7],
                target_reps_low=row[8],
                target_reps_high=row[9],
                target_weight_kg=float(row[10]) if row[10] is not None else None,
                rest_seconds=row[11],
                notes=row[12],
            ),
            row[1],
        )
        for row in rows
    ]


def get_routine(conn: Connection, user_id: int, routine_id: int) -> Optional[Routine]:
    """One routine with its contents, or None if it is not this user's.

    Not found and not yours deliberately return the same thing. Distinguishing
    them would confirm that a routine id exists, which is a small leak but a
    free one to avoid.
    """
    row = conn.execute(
        text(
            f"SELECT {_ROUTINE_COLUMNS} FROM routines "
            f"WHERE routine_id = :routine_id AND user_id = :user_id"
        ),
        {"routine_id": routine_id, "user_id": user_id},
    ).fetchone()
    if row is None:
        return None
    routine = _row_to_routine(row)
    routine.exercises = [item for item, _ in _load_exercises(conn, user_id, [routine_id])]
    return routine


# --------------------------------------------------------------------------
# Writes
# --------------------------------------------------------------------------


def create_routine(
    conn: Connection,
    user_id: int,
    name: str,
    notes: Optional[str] = None,
    exercises: Optional[Sequence[dict[str, Any]]] = None,
) -> Routine:
    """Create a routine and, optionally, fill it in one transaction."""
    clean = clean_name(name)

    existing = conn.execute(
        text("SELECT 1 FROM routines WHERE user_id = :user_id AND name = :name"),
        {"user_id": user_id, "name": clean},
    ).fetchone()
    if existing is not None:
        # Checked before inserting so the message names the problem. The UNIQUE
        # constraint is still the guarantee — two simultaneous creates would
        # race past this check and one would correctly fail there.
        raise RoutineError(f"You already have a routine called “{clean}”.")

    # New routines go to the top: the one just created is the one about to be
    # used. COALESCE covers the first routine, where MIN over no rows is NULL.
    position = conn.execute(
        text("SELECT COALESCE(MIN(position), 0) - 1 FROM routines WHERE user_id = :user_id"),
        {"user_id": user_id},
    ).scalar()

    routine_id = conn.execute(
        text(
            "INSERT INTO routines (user_id, name, notes, position) "
            "VALUES (:user_id, :name, :notes, :position) RETURNING routine_id"
        ),
        {
            "user_id": user_id,
            "name": clean,
            "notes": (notes or "").strip() or None,
            "position": position,
        },
    ).scalar()

    if exercises:
        replace_exercises(conn, user_id, routine_id, exercises)

    logger.info(
        "routine created",
        extra={
            "event.action": "routine_created",
            "user.id": user_id,
            "routine.id": routine_id,
            "routine.exercise_count": len(exercises or []),
        },
    )
    return get_routine(conn, user_id, routine_id)  # type: ignore[return-value]


def update_routine(
    conn: Connection,
    user_id: int,
    routine_id: int,
    name: Optional[str] = None,
    notes: Optional[str] = None,
    archived: Optional[bool] = None,
) -> Routine:
    """Rename, re-note or archive. Only the fields passed are touched."""
    if get_routine(conn, user_id, routine_id) is None:
        raise RoutineError("That routine does not exist.")

    updates: list[str] = []
    params: dict[str, Any] = {"routine_id": routine_id, "user_id": user_id}

    if name is not None:
        params["name"] = clean_name(name)
        updates.append("name = :name")
    if notes is not None:
        params["notes"] = notes.strip() or None
        updates.append("notes = :notes")
    if archived is not None:
        params["archived"] = archived
        updates.append("archived = :archived")

    if updates:
        updates.append("updated_at = now()")
        conn.execute(
            text(
                f"UPDATE routines SET {', '.join(updates)} "
                f"WHERE routine_id = :routine_id AND user_id = :user_id"
            ),
            params,
        )
        logger.info(
            "routine updated",
            extra={
                "event.action": "routine_updated",
                "user.id": user_id,
                "routine.id": routine_id,
                "routine.fields": [u.split(" =")[0] for u in updates],
            },
        )
    return get_routine(conn, user_id, routine_id)  # type: ignore[return-value]


def replace_exercises(
    conn: Connection,
    user_id: int,
    routine_id: int,
    items: Sequence[dict[str, Any]],
) -> Routine:
    """Set the routine's contents to exactly `items`, in the order given.

    Replace rather than diff. The editor hands back the whole list, ordering is
    part of what is being edited, and reconciling adds/moves/removes against the
    stored rows would be more code and more ways to be wrong for no behavioural
    difference. The caller's transaction makes it atomic.

    Each exercise id is re-checked against this user's own exercises. The ids
    arrive from the browser, so trusting them would let a crafted request
    reference somebody else's row — and because a routine line joins to
    `exercises` for its name, that would leak the name straight back.
    """
    if len(items) > MAX_EXERCISES_PER_ROUTINE:
        raise RoutineError(
            f"A routine can hold at most {MAX_EXERCISES_PER_ROUTINE} exercises."
        )
    if get_routine(conn, user_id, routine_id) is None:
        raise RoutineError("That routine does not exist.")

    cleaned: list[dict[str, Any]] = []
    for position, item in enumerate(items):
        try:
            exercise_id = int(item["exercise_id"])
        except (KeyError, TypeError, ValueError):
            raise RoutineError("Every routine line needs an exercise.") from None
        cleaned.append(
            {
                "routine_id": routine_id,
                "exercise_id": exercise_id,
                "position": position,
                **_clean_target(item),
            }
        )

    if cleaned:
        owned = conn.execute(
            text(
                "SELECT exercise_id FROM exercises "
                "WHERE user_id = :user_id AND exercise_id = ANY(:ids)"
            ),
            {"user_id": user_id, "ids": [c["exercise_id"] for c in cleaned]},
        ).fetchall()
        owned_ids = {row[0] for row in owned}
        missing = [c["exercise_id"] for c in cleaned if c["exercise_id"] not in owned_ids]
        if missing:
            logger.warning(
                "routine referenced exercises that are not this user's",
                extra={
                    "event.action": "routine_exercise_rejected",
                    "user.id": user_id,
                    "routine.id": routine_id,
                    "rejected.count": len(missing),
                },
            )
            raise RoutineError("One of those exercises is not in your list.")

    conn.execute(
        text(
            "DELETE FROM routine_exercises WHERE routine_id IN "
            "(SELECT routine_id FROM routines "
            " WHERE routine_id = :routine_id AND user_id = :user_id)"
        ),
        {"routine_id": routine_id, "user_id": user_id},
    )

    if cleaned:
        # One executemany rather than a statement per line: a 12-exercise
        # routine is 12 round trips otherwise, on a phone connection.
        conn.execute(
            text(
                """
                INSERT INTO routine_exercises
                    (routine_id, exercise_id, position, target_sets,
                     target_reps_low, target_reps_high, target_weight_kg,
                     rest_seconds, notes)
                VALUES
                    (:routine_id, :exercise_id, :position, :target_sets,
                     :target_reps_low, :target_reps_high, :target_weight_kg,
                     :rest_seconds, :notes)
                """
            ),
            cleaned,
        )

    conn.execute(
        text("UPDATE routines SET updated_at = now() "
             "WHERE routine_id = :routine_id AND user_id = :user_id"),
        {"routine_id": routine_id, "user_id": user_id},
    )

    logger.info(
        "routine contents replaced",
        extra={
            "event.action": "routine_exercises_replaced",
            "user.id": user_id,
            "routine.id": routine_id,
            "routine.exercise_count": len(cleaned),
        },
    )
    return get_routine(conn, user_id, routine_id)  # type: ignore[return-value]


def reorder_routines(conn: Connection, user_id: int, routine_ids: Sequence[int]) -> None:
    """Set list order from the given ids. Ids not owned by this user are ignored."""
    if not routine_ids:
        return
    conn.execute(
        text(
            "UPDATE routines SET position = :position "
            "WHERE routine_id = :routine_id AND user_id = :user_id"
        ),
        [
            {"position": position, "routine_id": routine_id, "user_id": user_id}
            for position, routine_id in enumerate(routine_ids)
        ],
    )


def delete_routine(conn: Connection, user_id: int, routine_id: int) -> bool:
    """Delete a routine. Its sessions and their sets are kept.

    `workout_sessions.routine_id` is ON DELETE SET NULL and the session's name
    was copied at start time, so history survives with its label intact. This is
    why deleting a template is safe enough to offer at all — archiving is still
    the better default, and what the UI suggests first.
    """
    deleted = conn.execute(
        text("DELETE FROM routines WHERE routine_id = :routine_id AND user_id = :user_id"),
        {"routine_id": routine_id, "user_id": user_id},
    ).rowcount
    if deleted:
        logger.info(
            "routine deleted",
            extra={
                "event.action": "routine_deleted",
                "user.id": user_id,
                "routine.id": routine_id,
            },
        )
    return bool(deleted)
