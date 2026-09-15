"""Live workout sessions: logging set by set, while it is happening.

The other half of the app. `pipeline.py` takes a journal entry written after the
fact and extracts rows from it; this takes one set at a time as it is performed.
Both write `workout_logs`, so the progress charts and the weekly report see
every set regardless of how it arrived.

What is different about logging live
------------------------------------
Each set is its own write, so the write has to be cheap and the answer has to
come back fast enough to be read between sets. That shapes three decisions:

  * The server holds the session, not the browser. `workout_sessions` with a
    NULL `finished_at` *is* the live state. A phone that locks, loses signal, or
    reloads finds the same session waiting, which a browser-side draft would not
    survive.
  * One session at a time per person, enforced by a partial unique index rather
    than by checking first. A double-tapped "start workout" is a real thing that
    happens with cold hands, and a check-then-insert races.
  * A logged set is answered with everything the screen needs to update — the
    set, the records it beat, the rest timer to start. Not a bare OK that sends
    the client back for more.

The "Previous" column
---------------------
Strive's set table shows what you did last time, and it is the single most
useful thing on the screen: it is what turns logging into progressive overload.
`previous_performance` finds the most recent *earlier* session containing that
lift and returns its sets — not the last few sets of the lift, which would run
together two different days when a workout is repeated.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

import catalog
import records
import routines as routines_module

logger = logging.getLogger("gym_tracker.sessions")

# Refuse a set beyond this many in one session for one exercise. Not a judgement
# about training — it is a guard against a stuck button writing rows forever.
MAX_SETS_PER_EXERCISE = 50

# Bounds on a single logged set. Deliberately generous: the world record
# deadlift is over 500kg, and somebody doing 100 bodyweight reps is training,
# not fat-fingering. These catch a decimal point in the wrong place.
MAX_WEIGHT_KG = 1000.0
MAX_REPS = 500


class SessionError(ValueError):
    """A session action failed, with a reason meant for the person."""


# --------------------------------------------------------------------------
# Types
# --------------------------------------------------------------------------


@dataclass
class LoggedSet:
    """One set already written."""

    log_id: int
    exercise_id: int
    exercise_name: str
    set_number: int
    weight_kg: Optional[float] = None
    reps: Optional[int] = None
    cheat_reps: int = 0
    is_warmup: bool = False
    is_dropset: bool = False
    rpe: Optional[float] = None
    rir: Optional[int] = None
    pain_flag: bool = False
    notes: Optional[str] = None
    logged_at: Optional[datetime] = None

    @property
    def volume_kg(self) -> float:
        if self.weight_kg is None or self.reps is None or self.is_warmup:
            return 0.0
        return float(self.weight_kg) * float(self.reps)

    @property
    def display(self) -> str:
        """"80 × 8", or "× 8" for a set with no external load."""
        reps = f"× {self.reps}" if self.reps is not None else ""
        if self.weight_kg in (None, 0):
            return reps or "—"
        return f"{_trim(float(self.weight_kg))} {reps}".strip()

    def to_dict(self) -> dict[str, Any]:
        return {
            "log_id": self.log_id,
            "exercise_id": self.exercise_id,
            "exercise_name": self.exercise_name,
            "set_number": self.set_number,
            "weight_kg": float(self.weight_kg) if self.weight_kg is not None else None,
            "reps": self.reps,
            "cheat_reps": self.cheat_reps,
            "is_warmup": self.is_warmup,
            "is_dropset": self.is_dropset,
            "rpe": float(self.rpe) if self.rpe is not None else None,
            "rir": self.rir,
            "pain_flag": self.pain_flag,
            "notes": self.notes,
            "volume_kg": round(self.volume_kg, 1),
            "display": self.display,
            "logged_at": self.logged_at.isoformat() if self.logged_at else None,
        }


@dataclass
class SessionExercise:
    """One lift on the live screen: its plan, and what has been done so far."""

    exercise_id: int
    name: str
    muscle_group: Optional[str] = None
    equipment: Optional[str] = None
    position: int = 0
    target_sets: Optional[int] = None
    target_reps_label: str = ""
    target_weight_kg: Optional[float] = None
    rest_seconds: Optional[int] = None
    notes: Optional[str] = None
    sets: list[LoggedSet] = field(default_factory=list)
    previous: list[LoggedSet] = field(default_factory=list)
    previous_date: Optional[datetime] = None
    # Set when the lift is not part of the routine but was added mid-session.
    added_ad_hoc: bool = False

    @property
    def working_sets_done(self) -> int:
        return sum(1 for s in self.sets if not s.is_warmup)

    @property
    def is_complete(self) -> bool:
        """Whether the plan for this lift has been met. Drives the tick."""
        if not self.target_sets:
            return bool(self.sets)
        return self.working_sets_done >= self.target_sets

    @property
    def is_bodyweight(self) -> bool:
        return (self.equipment or "") in catalog.BODYWEIGHT_EQUIPMENT

    def to_dict(self) -> dict[str, Any]:
        return {
            "exercise_id": self.exercise_id,
            "name": self.name,
            "muscle_group": self.muscle_group,
            "equipment": self.equipment,
            "equipment_label": catalog.equipment_label(self.equipment),
            "position": self.position,
            "target_sets": self.target_sets,
            "target_reps_label": self.target_reps_label,
            "target_weight_kg": (
                float(self.target_weight_kg) if self.target_weight_kg is not None else None
            ),
            "rest_seconds": self.rest_seconds or routines_module.DEFAULT_REST_SECONDS,
            "notes": self.notes,
            "is_bodyweight": self.is_bodyweight,
            "added_ad_hoc": self.added_ad_hoc,
            "working_sets_done": self.working_sets_done,
            "is_complete": self.is_complete,
            "sets": [s.to_dict() for s in self.sets],
            "previous": [s.to_dict() for s in self.previous],
            "previous_date": self.previous_date.isoformat() if self.previous_date else None,
        }


@dataclass
class Session:
    """A workout, live or finished."""

    session_id: int
    user_id: int
    name: Optional[str]
    started_at: datetime
    finished_at: Optional[datetime] = None
    routine_id: Optional[int] = None
    notes: Optional[str] = None
    exercises: list[SessionExercise] = field(default_factory=list)

    @property
    def is_active(self) -> bool:
        return self.finished_at is None

    @property
    def total_sets(self) -> int:
        return sum(len(e.sets) for e in self.exercises)

    @property
    def total_volume_kg(self) -> float:
        return round(sum(s.volume_kg for e in self.exercises for s in e.sets), 1)

    @property
    def duration_seconds(self) -> int:
        end = self.finished_at or datetime.now(tz=self.started_at.tzinfo)
        return max(0, int((end - self.started_at).total_seconds()))

    def to_dict(self, include_exercises: bool = True) -> dict[str, Any]:
        body: dict[str, Any] = {
            "session_id": self.session_id,
            "name": self.name,
            "routine_id": self.routine_id,
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "is_active": self.is_active,
            "notes": self.notes,
            "total_sets": self.total_sets,
            "total_volume_kg": self.total_volume_kg,
            "duration_seconds": self.duration_seconds,
        }
        if include_exercises:
            body["exercises"] = [e.to_dict() for e in self.exercises]
        return body


def _trim(value: float) -> str:
    return f"{float(value):.2f}".rstrip("0").rstrip(".")


# --------------------------------------------------------------------------
# Exercise resolution
# --------------------------------------------------------------------------


def resolve_exercise(conn: Connection, user_id: int, name: str) -> int:
    """This user's exercise id for `name`, creating the row if it is new.

    Exact match only, unlike `pipeline.get_or_create_exercise`, which fuzzy
    matches because it is cleaning up free text somebody typed into a journal.
    Here the name came from a catalog the person tapped, so a fuzzy match could
    only ever file the set under the wrong lift.

    A new row is enriched from the catalog, so a lift picked from it arrives
    with its equipment and muscle group already set rather than needing the
    backfill script later.
    """
    clean = " ".join((name or "").split())
    if not clean:
        raise SessionError("An exercise needs a name.")
    if len(clean) > 120:
        raise SessionError("That exercise name is too long.")

    key = catalog.search_key(clean)
    row = conn.execute(
        text(
            "SELECT exercise_id FROM exercises "
            "WHERE user_id = :user_id AND (search_key = :key OR lower(name) = lower(:name)) "
            "ORDER BY exercise_id LIMIT 1"
        ),
        {"user_id": user_id, "key": key, "name": clean},
    ).fetchone()
    if row is not None:
        return int(row[0])

    entry = catalog.resolve(clean)
    exercise_id = conn.execute(
        text(
            "INSERT INTO exercises (user_id, name, muscle_group, equipment, is_custom, search_key) "
            "VALUES (:user_id, :name, :muscle_group, :equipment, :is_custom, :search_key) "
            "RETURNING exercise_id"
        ),
        {
            "user_id": user_id,
            "name": entry.name if entry else clean,
            "muscle_group": entry.muscle_group if entry else None,
            "equipment": entry.equipment if entry else None,
            # A lift absent from the catalog is by definition the person's own.
            "is_custom": entry is None,
            "search_key": key,
        },
    ).scalar()

    logger.info(
        "exercise created",
        extra={
            "event.action": "exercise_created",
            "user.id": user_id,
            "exercise.id": exercise_id,
            "exercise.from_catalog": entry is not None,
        },
    )
    return int(exercise_id)


# --------------------------------------------------------------------------
# Previous performance
# --------------------------------------------------------------------------


def previous_performance(
    conn: Connection,
    user_id: int,
    exercise_id: int,
    before_session_id: Optional[int] = None,
) -> tuple[list[LoggedSet], Optional[datetime]]:
    """The sets from the last time this lift was trained, and when that was.

    Scoped to one earlier session rather than "the last N sets" — repeating a
    workout would otherwise splice two different days together and show a
    "previous" that never happened as a single session.

    Sets logged through the journal-paste flow have no session, so they are
    grouped by calendar day instead. Both paths are searched, and the most
    recent wins, so the Previous column works whichever way the last workout
    was recorded.
    """
    rows = conn.execute(
        text(
            """
            WITH candidate AS (
                SELECT wl.log_id, wl.weight_kg, wl.reps, wl.cheat_reps,
                       wl.is_warmup, wl.is_dropset, wl.rpe, wl.rir, wl.set_number,
                       wl.logged_at,
                       -- A session id where there is one, otherwise the calendar
                       -- day, so journal-entered sets still group into a workout.
                       COALESCE(wl.session_id::text, wl.logged_at::date::text) AS grp,
                       MAX(wl.logged_at) OVER (
                           PARTITION BY COALESCE(wl.session_id::text,
                                                 wl.logged_at::date::text)
                       ) AS grp_at
                  FROM workout_logs wl
                 WHERE wl.user_id = :user_id
                   AND wl.exercise_id = :exercise_id
                   AND (:before IS NULL OR wl.session_id IS DISTINCT FROM :before)
            )
            SELECT log_id, weight_kg, reps, cheat_reps, is_warmup, is_dropset,
                   rpe, rir, set_number, logged_at
              FROM candidate
             WHERE grp = (SELECT grp FROM candidate ORDER BY grp_at DESC LIMIT 1)
             ORDER BY COALESCE(set_number, 0), log_id
            """
        ),
        {"user_id": user_id, "exercise_id": exercise_id, "before": before_session_id},
    ).fetchall()

    if not rows:
        return [], None

    name = conn.execute(
        text("SELECT name FROM exercises WHERE exercise_id = :id AND user_id = :user_id"),
        {"id": exercise_id, "user_id": user_id},
    ).scalar() or ""

    sets = [
        LoggedSet(
            log_id=row[0],
            exercise_id=exercise_id,
            exercise_name=name,
            weight_kg=float(row[1]) if row[1] is not None else None,
            reps=row[2],
            cheat_reps=row[3] or 0,
            is_warmup=bool(row[4]),
            is_dropset=bool(row[5]),
            rpe=float(row[6]) if row[6] is not None else None,
            rir=row[7],
            set_number=row[8] or (index + 1),
            logged_at=row[9],
        )
        for index, row in enumerate(rows)
    ]
    return sets, sets[-1].logged_at


# --------------------------------------------------------------------------
# Session lifecycle
# --------------------------------------------------------------------------


def active_session(conn: Connection, user_id: int) -> Optional[Session]:
    """The session in progress, if there is one."""
    row = conn.execute(
        text(
            "SELECT session_id, user_id, name, started_at, finished_at, routine_id, notes "
            "FROM workout_sessions WHERE user_id = :user_id AND finished_at IS NULL"
        ),
        {"user_id": user_id},
    ).fetchone()
    if row is None:
        return None
    return load_session(conn, user_id, int(row[0]))


def start_session(
    conn: Connection,
    user_id: int,
    routine_id: Optional[int] = None,
    name: Optional[str] = None,
) -> Session:
    """Begin a workout, from a routine or freestyle.

    Returning the existing session when one is already live, rather than
    raising, is deliberate: "start workout" tapped twice, or on a second device,
    means "take me to my workout" both times.
    """
    existing = active_session(conn, user_id)
    if existing is not None:
        return existing

    routine = None
    if routine_id is not None:
        routine = routines_module.get_routine(conn, user_id, routine_id)
        if routine is None:
            raise SessionError("That routine does not exist.")

    session_name = (name or "").strip() or (routine.name if routine else "Workout")

    try:
        session_id = conn.execute(
            text(
                "INSERT INTO workout_sessions (user_id, routine_id, name) "
                "VALUES (:user_id, :routine_id, :name) RETURNING session_id"
            ),
            {
                "user_id": user_id,
                "routine_id": routine.routine_id if routine else None,
                "name": session_name[:80],
            },
        ).scalar()
    except Exception as exc:  # noqa: BLE001 - the partial unique index is the guard
        # Two "start workout" taps landing at once: one insert wins, the other
        # violates idx_workout_sessions_one_active_per_user. The loser returns
        # the winner's session, which is what the person wanted either way.
        loser = active_session(conn, user_id)
        if loser is not None:
            return loser
        raise SessionError("Could not start a workout.") from exc

    logger.info(
        "workout session started",
        extra={
            "event.action": "session_started",
            "user.id": user_id,
            "session.id": session_id,
            "routine.id": routine.routine_id if routine else None,
        },
    )
    return load_session(conn, user_id, int(session_id))


def load_session(conn: Connection, user_id: int, session_id: int) -> Optional[Session]:
    """A session with its plan, its logged sets, and last time's numbers."""
    row = conn.execute(
        text(
            "SELECT session_id, user_id, name, started_at, finished_at, routine_id, notes "
            "FROM workout_sessions WHERE session_id = :session_id AND user_id = :user_id"
        ),
        {"session_id": session_id, "user_id": user_id},
    ).fetchone()
    if row is None:
        return None

    session = Session(
        session_id=int(row[0]),
        user_id=int(row[1]),
        name=row[2],
        started_at=row[3],
        finished_at=row[4],
        routine_id=row[5],
        notes=row[6],
    )

    by_exercise: dict[int, SessionExercise] = {}

    # The plan first, so an exercise with no sets logged yet still appears.
    if session.routine_id is not None:
        routine = routines_module.get_routine(conn, user_id, session.routine_id)
        if routine is not None:
            for item in routine.exercises:
                by_exercise[item.exercise_id] = SessionExercise(
                    exercise_id=item.exercise_id,
                    name=item.name,
                    muscle_group=item.muscle_group,
                    equipment=item.equipment,
                    position=item.position,
                    target_sets=item.target_sets,
                    target_reps_label=item.target_reps_label,
                    target_weight_kg=item.target_weight_kg,
                    rest_seconds=item.rest_seconds,
                    notes=item.notes,
                )

    # Then what was actually logged, which may include lifts added mid-session.
    for logged in _session_sets(conn, user_id, session_id):
        entry = by_exercise.get(logged.exercise_id)
        if entry is None:
            details = conn.execute(
                text(
                    "SELECT muscle_group, equipment FROM exercises "
                    "WHERE exercise_id = :id AND user_id = :user_id"
                ),
                {"id": logged.exercise_id, "user_id": user_id},
            ).fetchone()
            entry = SessionExercise(
                exercise_id=logged.exercise_id,
                name=logged.exercise_name,
                muscle_group=details[0] if details else None,
                equipment=details[1] if details else None,
                position=1000 + len(by_exercise),  # ad-hoc lifts sort to the end
                added_ad_hoc=True,
            )
            by_exercise[logged.exercise_id] = entry
        entry.sets.append(logged)

    for entry in by_exercise.values():
        entry.previous, entry.previous_date = previous_performance(
            conn, user_id, entry.exercise_id, before_session_id=session_id
        )

    session.exercises = sorted(by_exercise.values(), key=lambda e: (e.position, e.name))
    return session


def _session_sets(conn: Connection, user_id: int, session_id: int) -> list[LoggedSet]:
    rows = conn.execute(
        text(
            """
            SELECT wl.log_id, wl.exercise_id, e.name, wl.set_number, wl.weight_kg,
                   wl.reps, wl.cheat_reps, wl.is_warmup, wl.is_dropset, wl.rpe,
                   wl.rir, wl.pain_flag, wl.notes, wl.logged_at
              FROM workout_logs wl
              JOIN exercises e ON e.exercise_id = wl.exercise_id
             WHERE wl.session_id = :session_id AND wl.user_id = :user_id
             ORDER BY wl.exercise_id, COALESCE(wl.set_number, 0), wl.log_id
            """
        ),
        {"session_id": session_id, "user_id": user_id},
    ).fetchall()
    return [
        LoggedSet(
            log_id=row[0],
            exercise_id=row[1],
            exercise_name=row[2],
            set_number=row[3] or 0,
            weight_kg=float(row[4]) if row[4] is not None else None,
            reps=row[5],
            cheat_reps=row[6] or 0,
            is_warmup=bool(row[7]),
            is_dropset=bool(row[8]),
            rpe=float(row[9]) if row[9] is not None else None,
            rir=row[10],
            pain_flag=bool(row[11]),
            notes=row[12],
            logged_at=row[13],
        )
        for row in rows
    ]


def finish_session(
    conn: Connection, user_id: int, session_id: int, notes: Optional[str] = None
) -> Session:
    """Close a session. An empty one is deleted rather than kept.

    A session with no sets is someone who opened the app and left; keeping it
    would put a phantom workout on the heatmap and in the weekly count.
    """
    session = load_session(conn, user_id, session_id)
    if session is None:
        raise SessionError("That workout does not exist.")
    if not session.is_active:
        return session

    if session.total_sets == 0:
        discard_session(conn, user_id, session_id)
        session.finished_at = datetime.now().astimezone()
        return session

    conn.execute(
        text(
            "UPDATE workout_sessions SET finished_at = now(), notes = COALESCE(:notes, notes) "
            "WHERE session_id = :session_id AND user_id = :user_id AND finished_at IS NULL"
        ),
        {"session_id": session_id, "user_id": user_id, "notes": (notes or "").strip() or None},
    )

    logger.info(
        "workout session finished",
        extra={
            "event.action": "session_finished",
            "user.id": user_id,
            "session.id": session_id,
            "session.sets": session.total_sets,
            "session.volume_kg": session.total_volume_kg,
            "session.duration_seconds": session.duration_seconds,
        },
    )
    return load_session(conn, user_id, session_id)  # type: ignore[return-value]


def discard_session(conn: Connection, user_id: int, session_id: int) -> bool:
    """Delete a session and every set logged in it.

    Genuinely destructive, and offered because the alternative — a mistaken
    session left in the history — corrupts every chart built on it. The UI
    confirms first. Sets are deleted explicitly rather than by cascade, because
    `workout_logs.session_id` is ON DELETE SET NULL: that is right for deleting
    a *routine*, and would leave orphaned sets here.
    """
    deleted_sets = conn.execute(
        text("DELETE FROM workout_logs WHERE session_id = :session_id AND user_id = :user_id"),
        {"session_id": session_id, "user_id": user_id},
    ).rowcount
    deleted = conn.execute(
        text("DELETE FROM workout_sessions WHERE session_id = :session_id AND user_id = :user_id"),
        {"session_id": session_id, "user_id": user_id},
    ).rowcount

    if deleted:
        logger.info(
            "workout session discarded",
            extra={
                "event.action": "session_discarded",
                "user.id": user_id,
                "session.id": session_id,
                "session.sets_deleted": deleted_sets,
            },
        )
    return bool(deleted)


def recent_sessions(conn: Connection, user_id: int, limit: int = 20) -> list[Session]:
    """Finished sessions, newest first, without loading their sets."""
    rows = conn.execute(
        text(
            """
            SELECT ws.session_id, ws.user_id, ws.name, ws.started_at, ws.finished_at,
                   ws.routine_id, ws.notes,
                   COUNT(wl.log_id),
                   COALESCE(SUM(CASE WHEN wl.is_warmup THEN 0
                                     ELSE wl.weight_kg * wl.reps END), 0)
              FROM workout_sessions ws
              LEFT JOIN workout_logs wl
                     ON wl.session_id = ws.session_id AND wl.user_id = ws.user_id
             WHERE ws.user_id = :user_id AND ws.finished_at IS NOT NULL
             GROUP BY ws.session_id
             ORDER BY ws.started_at DESC
             LIMIT :limit
            """
        ),
        {"user_id": user_id, "limit": max(1, min(int(limit), 100))},
    ).fetchall()

    built: list[Session] = []
    for row in rows:
        session = Session(
            session_id=int(row[0]),
            user_id=int(row[1]),
            name=row[2],
            started_at=row[3],
            finished_at=row[4],
            routine_id=row[5],
            notes=row[6],
        )
        # Counts come from the aggregate rather than from loaded sets, so the
        # list costs one query no matter how many sessions it shows.
        session._summary = (int(row[7]), float(row[8] or 0.0))  # type: ignore[attr-defined]
        built.append(session)
    return built


# --------------------------------------------------------------------------
# Logging a set
# --------------------------------------------------------------------------


def _validate_set(
    weight_kg: Optional[float],
    reps: Optional[int],
    cheat_reps: int,
    rpe: Optional[float],
    rir: Optional[int],
) -> dict[str, Any]:
    """Coerce and bounds-check one set, or raise with a usable message."""

    def _num(value: Any, label: str, low: float, high: float) -> Optional[float]:
        if value in (None, "", "None"):
            return None
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            raise SessionError(f"{label} must be a number.") from None
        if not low <= parsed <= high:
            raise SessionError(f"{label} must be between {low:g} and {high:g}.")
        return parsed

    weight = _num(weight_kg, "Weight", 0, MAX_WEIGHT_KG)
    rep_count = _num(reps, "Reps", 0, MAX_REPS)
    cheated = _num(cheat_reps, "Cheat reps", 0, MAX_REPS) or 0.0
    effort = _num(rpe, "RPE", 1, 10)
    reserve = _num(rir, "RIR", 0, 20)

    if rep_count is None:
        raise SessionError("A set needs a rep count.")
    rep_count = int(rep_count)
    cheated = int(cheated)
    if cheated > rep_count:
        raise SessionError("Cheat reps cannot exceed the reps performed.")

    return {
        "weight_kg": weight,
        "reps": rep_count,
        "cheat_reps": cheated,
        "rpe": effort,
        "rir": int(reserve) if reserve is not None else None,
    }


def log_set(
    conn: Connection,
    user_id: int,
    session_id: int,
    exercise_id: Optional[int] = None,
    exercise_name: Optional[str] = None,
    weight_kg: Optional[float] = None,
    reps: Optional[int] = None,
    cheat_reps: int = 0,
    is_warmup: bool = False,
    is_dropset: bool = False,
    rpe: Optional[float] = None,
    rir: Optional[int] = None,
    pain_flag: bool = False,
    notes: Optional[str] = None,
) -> tuple[LoggedSet, list[records.RecordBreak]]:
    """Write one set and report any record it beat.

    The record check runs in the caller's transaction alongside the insert, so a
    set and the PR it set commit together. A PR announced for a set that failed
    to save would be the worst of both.
    """
    session = conn.execute(
        text(
            "SELECT finished_at FROM workout_sessions "
            "WHERE session_id = :session_id AND user_id = :user_id"
        ),
        {"session_id": session_id, "user_id": user_id},
    ).fetchone()
    if session is None:
        raise SessionError("That workout does not exist.")
    if session[0] is not None:
        raise SessionError("That workout has already been finished.")

    if exercise_id is None:
        if not exercise_name:
            raise SessionError("A set needs an exercise.")
        exercise_id = resolve_exercise(conn, user_id, exercise_name)
    else:
        # The id arrived from the browser, so ownership is re-checked here
        # rather than trusted. Without this, a crafted request could append
        # sets to another account's exercise.
        owned = conn.execute(
            text(
                "SELECT name FROM exercises "
                "WHERE exercise_id = :id AND user_id = :user_id"
            ),
            {"id": exercise_id, "user_id": user_id},
        ).fetchone()
        if owned is None:
            raise SessionError("That exercise is not in your list.")
        exercise_name = owned[0]

    if not exercise_name:
        exercise_name = conn.execute(
            text("SELECT name FROM exercises WHERE exercise_id = :id AND user_id = :user_id"),
            {"id": exercise_id, "user_id": user_id},
        ).scalar() or ""

    values = _validate_set(weight_kg, reps, cheat_reps, rpe, rir)

    # Set numbers are per exercise per session and assigned server-side. The
    # client could send one, but two sets logged in quick succession would then
    # race to the same number.
    next_number = conn.execute(
        text(
            "SELECT COALESCE(MAX(set_number), 0) + 1 FROM workout_logs "
            "WHERE session_id = :session_id AND exercise_id = :exercise_id "
            "AND user_id = :user_id"
        ),
        {"session_id": session_id, "exercise_id": exercise_id, "user_id": user_id},
    ).scalar()
    if next_number > MAX_SETS_PER_EXERCISE:
        raise SessionError(
            f"That is more than {MAX_SETS_PER_EXERCISE} sets of one exercise in a "
            f"single workout. Start a new workout if that is really the plan."
        )

    log_id = conn.execute(
        text(
            """
            INSERT INTO workout_logs
                (user_id, exercise_id, session_id, logged_at, weight_kg, reps,
                 cheat_reps, set_number, is_warmup, is_dropset, rpe, rir,
                 pain_flag, notes, extraction_confidence)
            VALUES
                (:user_id, :exercise_id, :session_id, now(), :weight_kg, :reps,
                 :cheat_reps, :set_number, :is_warmup, :is_dropset, :rpe, :rir,
                 :pain_flag, :notes, 1.0)
            RETURNING log_id, logged_at
            """
        ),
        {
            "user_id": user_id,
            "exercise_id": exercise_id,
            "session_id": session_id,
            "set_number": next_number,
            "is_warmup": is_warmup,
            "is_dropset": is_dropset,
            "pain_flag": pain_flag,
            "notes": (notes or "").strip() or None,
            **values,
        },
    ).fetchone()

    written = LoggedSet(
        log_id=int(log_id[0]),
        exercise_id=int(exercise_id),
        exercise_name=exercise_name,
        set_number=int(next_number),
        is_warmup=is_warmup,
        is_dropset=is_dropset,
        pain_flag=pain_flag,
        notes=(notes or "").strip() or None,
        logged_at=log_id[1],
        **values,
    )

    breaks = records.check_and_update(
        conn,
        user_id=user_id,
        exercise_id=int(exercise_id),
        exercise_name=exercise_name,
        weight_kg=values["weight_kg"],
        reps=values["reps"],
        cheat_reps=values["cheat_reps"],
        is_warmup=is_warmup,
        log_id=written.log_id,
        session_id=session_id,
        achieved_at=written.logged_at,
    )

    logger.info(
        "set logged",
        extra={
            "event.action": "set_logged",
            "user.id": user_id,
            "session.id": session_id,
            "exercise.id": exercise_id,
            "set.number": written.set_number,
            "set.is_warmup": is_warmup,
            "set.records_broken": len(breaks),
        },
    )
    return written, breaks


def update_set(
    conn: Connection, user_id: int, log_id: int, **fields: Any
) -> LoggedSet:
    """Correct a set already logged. Records are not lowered here — see below."""
    row = conn.execute(
        text(
            "SELECT wl.exercise_id, e.name, wl.session_id, wl.set_number, wl.is_warmup "
            "FROM workout_logs wl JOIN exercises e ON e.exercise_id = wl.exercise_id "
            "WHERE wl.log_id = :log_id AND wl.user_id = :user_id"
        ),
        {"log_id": log_id, "user_id": user_id},
    ).fetchone()
    if row is None:
        raise SessionError("That set does not exist.")

    values = _validate_set(
        fields.get("weight_kg"),
        fields.get("reps"),
        fields.get("cheat_reps", 0) or 0,
        fields.get("rpe"),
        fields.get("rir"),
    )
    is_warmup = bool(fields.get("is_warmup", row[4]))

    conn.execute(
        text(
            "UPDATE workout_logs SET weight_kg = :weight_kg, reps = :reps, "
            "cheat_reps = :cheat_reps, rpe = :rpe, rir = :rir, is_warmup = :is_warmup "
            "WHERE log_id = :log_id AND user_id = :user_id"
        ),
        {**values, "is_warmup": is_warmup, "log_id": log_id, "user_id": user_id},
    )

    # A correction can only ever raise a record here, never lower one: the
    # cached best may now be held by a set that no longer exists as logged.
    # Lowering it correctly needs a full recompute, which is `records.rebuild`,
    # and doing that on every edit would make correcting a typo the most
    # expensive action in the app. The API exposes rebuild for when it matters.
    records.check_and_update(
        conn,
        user_id=user_id,
        exercise_id=int(row[0]),
        exercise_name=row[1],
        weight_kg=values["weight_kg"],
        reps=values["reps"],
        cheat_reps=values["cheat_reps"],
        is_warmup=is_warmup,
        log_id=log_id,
        session_id=row[2],
    )

    return LoggedSet(
        log_id=log_id,
        exercise_id=int(row[0]),
        exercise_name=row[1],
        set_number=int(row[3] or 0),
        is_warmup=is_warmup,
        **values,
    )


def delete_set(conn: Connection, user_id: int, log_id: int) -> bool:
    """Remove one logged set."""
    deleted = conn.execute(
        text("DELETE FROM workout_logs WHERE log_id = :log_id AND user_id = :user_id"),
        {"log_id": log_id, "user_id": user_id},
    ).rowcount
    if deleted:
        logger.info(
            "set deleted",
            extra={"event.action": "set_deleted", "user.id": user_id, "log.id": log_id},
        )
    return bool(deleted)
