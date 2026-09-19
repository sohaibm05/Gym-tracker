"""Personal records: detecting them as a set is logged, and announcing them.

Three kinds, because "personal record" means three different things depending on
what you are training for and a single number hides two of them:

    heaviest_weight      most weight moved for at least one clean rep.
                         What people mean colloquially. Insensitive to reps, so
                         a hard single beats an easy set of ten.
    best_1rm             best Epley estimate from one set. Catches the
                         improvement that heaviest_weight misses — 100kg x 8
                         is a better set than 100kg x 5 and the same "weight PR".
    best_session_volume  most weight x reps for this lift in one session. The
                         hypertrophy-facing one: more total work, at any load.

Why these are cached
--------------------
All three are derivable from `workout_logs` by aggregation, and are stored in
`personal_records` anyway. The reason is the interaction: telling somebody they
just hit a PR has to happen in the moment they tap the checkmark, and scanning
their entire history for that lift on every set is the wrong thing to do on gym
wifi. One indexed row per (person, exercise, type) makes the check a single-row
read and the update a single-row upsert.

A cache has to be rebuildable or it becomes a liability, so `rebuild()`
recomputes the whole table from `workout_logs`. That is also the answer for
existing accounts: migration 003 deliberately does not backfill.

Agreement with the weekly report
--------------------------------
Every figure here comes from `insights` — `epley_1rm`, `clean_rep_count`, and
the working-set rule. A PR announced mid-workout that disagreed with the report
generated on Sunday would make both untrustworthy, and the only way to guarantee
they agree is to have one implementation.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

import insights

logger = logging.getLogger("gym_tracker.records")

HEAVIEST_WEIGHT = "heaviest_weight"
BEST_1RM = "best_1rm"
BEST_SESSION_VOLUME = "best_session_volume"

RECORD_TYPES = (HEAVIEST_WEIGHT, BEST_1RM, BEST_SESSION_VOLUME)

RECORD_LABELS = {
    HEAVIEST_WEIGHT: "Heaviest weight",
    BEST_1RM: "Best estimated 1RM",
    BEST_SESSION_VOLUME: "Best session volume",
}

# A new record has to beat the old one by at least this much to count.
#
# Without it, floating-point noise in an estimated 1RM announces a "record" for
# a set identical to last week's, and an app that congratulates you for nothing
# is one you stop believing. Expressed in the unit of the record: 0.01kg, or
# 0.01 kg-reps of volume.
MIN_IMPROVEMENT = 0.01


@dataclass(frozen=True)
class PersonalRecord:
    """A stored record."""

    exercise_id: int
    record_type: str
    value: float
    weight_kg: Optional[float] = None
    reps: Optional[int] = None
    achieved_at: Optional[datetime] = None
    exercise_name: Optional[str] = None
    log_id: Optional[int] = None
    # Which session set it. Used to tell "this session already holds the
    # cumulative volume record" from "a previous session held it", which is
    # what stops every set of a good workout announcing the same PR.
    session_id: Optional[int] = None

    @property
    def label(self) -> str:
        return RECORD_LABELS.get(self.record_type, self.record_type)

    @property
    def display(self) -> str:
        """How the record reads on screen."""
        if self.record_type == HEAVIEST_WEIGHT:
            if self.reps:
                return f"{_trim(self.value)} kg × {self.reps}"
            return f"{_trim(self.value)} kg"
        if self.record_type == BEST_1RM:
            base = f"{_trim(self.value)} kg e1RM"
            if self.weight_kg and self.reps:
                return f"{base} ({_trim(float(self.weight_kg))} × {self.reps})"
            return base
        return f"{_trim(self.value)} kg total"

    def to_dict(self) -> dict[str, Any]:
        return {
            "exercise_id": self.exercise_id,
            "exercise_name": self.exercise_name,
            "record_type": self.record_type,
            "label": self.label,
            "value": round(float(self.value), 2),
            "weight_kg": float(self.weight_kg) if self.weight_kg is not None else None,
            "reps": self.reps,
            "display": self.display,
            "achieved_at": self.achieved_at.isoformat() if self.achieved_at else None,
        }


@dataclass(frozen=True)
class RecordBreak:
    """A record that was just beaten. What the UI announces."""

    record_type: str
    exercise_name: str
    value: float
    previous_value: Optional[float]
    weight_kg: Optional[float] = None
    reps: Optional[int] = None

    @property
    def label(self) -> str:
        return RECORD_LABELS.get(self.record_type, self.record_type)

    @property
    def is_first(self) -> bool:
        """No previous record — the first logged set of a lift sets all three.

        Worth distinguishing: "first time logging this" deserves different
        wording from "you beat your best", and calling the former a PR is the
        quickest way to make the feature feel fake.
        """
        return self.previous_value is None

    @property
    def improvement(self) -> Optional[float]:
        if self.previous_value is None:
            return None
        return round(self.value - self.previous_value, 2)

    def to_dict(self) -> dict[str, Any]:
        return {
            "record_type": self.record_type,
            "label": self.label,
            "exercise_name": self.exercise_name,
            "value": round(float(self.value), 2),
            "previous_value": (
                round(float(self.previous_value), 2)
                if self.previous_value is not None
                else None
            ),
            "improvement": self.improvement,
            "is_first": self.is_first,
            "weight_kg": float(self.weight_kg) if self.weight_kg is not None else None,
            "reps": self.reps,
        }


def _trim(value: float) -> str:
    """Numbers as people write them: 100 not 100.0, 102.5 not 102.50."""
    return f"{float(value):.2f}".rstrip("0").rstrip(".")


# --------------------------------------------------------------------------
# Candidates
# --------------------------------------------------------------------------


def _is_working_set(weight_kg: Optional[float], reps: Optional[int], is_warmup: bool) -> bool:
    """The working-set rule from insights, over plain numbers.

    A warm-up single at a heavy load is not a record, and neither is a set with
    a missing field — both would otherwise poison the cache with a number no
    later real set could beat.
    """
    return (
        not is_warmup
        and weight_kg is not None
        and reps is not None
        and weight_kg > 0
        and reps > 0
    )


def candidates(
    weight_kg: Optional[float],
    reps: Optional[int],
    cheat_reps: int = 0,
    is_warmup: bool = False,
) -> dict[str, tuple[float, Optional[float], Optional[int]]]:
    """What one set is worth, per record type: {type: (value, weight, reps)}.

    Session volume is absent here because it is a property of the session rather
    than the set — see `_session_volume`.
    """
    if not _is_working_set(weight_kg, reps, is_warmup):
        return {}

    weight = float(weight_kg)  # type: ignore[arg-type]
    clean = insights.clean_rep_count(reps, cheat_reps) or 0

    found: dict[str, tuple[float, Optional[float], Optional[int]]] = {}

    # A set of entirely cheated reps demonstrates nothing about strength at that
    # load, so it sets no weight or 1RM record. It still counts toward volume,
    # because the work happened.
    if clean > 0:
        found[HEAVIEST_WEIGHT] = (weight, weight, clean)
        estimated = insights.epley_1rm(weight, clean)
        if estimated is not None:
            found[BEST_1RM] = (estimated, weight, clean)

    return found


def _session_volume(
    conn: Connection, user_id: int, exercise_id: int, session_id: int
) -> float:
    """Total weight x reps for one lift in one session, warm-ups excluded.

    Recomputed from the rows after each set rather than accumulated in memory,
    so an edited or deleted set cannot leave the running total wrong.
    """
    total = conn.execute(
        text(
            """
            SELECT COALESCE(SUM(weight_kg * reps), 0)
              FROM workout_logs
             WHERE user_id = :user_id
               AND exercise_id = :exercise_id
               AND session_id = :session_id
               AND is_warmup = FALSE
               AND weight_kg IS NOT NULL
               AND reps IS NOT NULL
            """
        ),
        {"user_id": user_id, "exercise_id": exercise_id, "session_id": session_id},
    ).scalar()
    return float(total or 0.0)


# --------------------------------------------------------------------------
# Reads
# --------------------------------------------------------------------------


def current(
    conn: Connection, user_id: int, exercise_id: int
) -> dict[str, PersonalRecord]:
    """This user's stored records for one lift, keyed by type."""
    rows = conn.execute(
        text(
            """
            SELECT pr.record_type, pr.value, pr.weight_kg, pr.reps, pr.achieved_at,
                   e.name, pr.log_id, pr.session_id
              FROM personal_records pr
              JOIN exercises e ON e.exercise_id = pr.exercise_id
             WHERE pr.user_id = :user_id AND pr.exercise_id = :exercise_id
            """
        ),
        {"user_id": user_id, "exercise_id": exercise_id},
    ).fetchall()
    return {
        row[0]: PersonalRecord(
            exercise_id=exercise_id,
            record_type=row[0],
            value=float(row[1]),
            weight_kg=float(row[2]) if row[2] is not None else None,
            reps=row[3],
            achieved_at=row[4],
            exercise_name=row[5],
            log_id=row[6],
            session_id=row[7],
        )
        for row in rows
    }


def recent(conn: Connection, user_id: int, limit: int = 20) -> list[PersonalRecord]:
    """Most recently achieved records across every lift, newest first."""
    rows = conn.execute(
        text(
            """
            SELECT pr.exercise_id, pr.record_type, pr.value, pr.weight_kg, pr.reps,
                   pr.achieved_at, e.name, pr.log_id, pr.session_id
              FROM personal_records pr
              JOIN exercises e ON e.exercise_id = pr.exercise_id
             WHERE pr.user_id = :user_id
             ORDER BY pr.achieved_at DESC
             LIMIT :limit
            """
        ),
        {"user_id": user_id, "limit": max(1, min(int(limit), 200))},
    ).fetchall()
    return [
        PersonalRecord(
            exercise_id=row[0],
            record_type=row[1],
            value=float(row[2]),
            weight_kg=float(row[3]) if row[3] is not None else None,
            reps=row[4],
            achieved_at=row[5],
            exercise_name=row[6],
            log_id=row[7],
            session_id=row[8],
        )
        for row in rows
    ]


# --------------------------------------------------------------------------
# Writes
# --------------------------------------------------------------------------


def _upsert(
    conn: Connection,
    user_id: int,
    exercise_id: int,
    record_type: str,
    value: float,
    weight_kg: Optional[float],
    reps: Optional[int],
    log_id: Optional[int],
    session_id: Optional[int],
    achieved_at: datetime,
) -> None:
    """Store a record, replacing the previous one for that type.

    The WHERE on the DO UPDATE is what makes this safe to call concurrently:
    two sets logged at once both attempt the upsert, and the row only moves if
    the incoming value actually beats what is stored. Without it the second
    write would win on arrival order rather than on merit, and could lower a
    record.
    """
    conn.execute(
        text(
            """
            INSERT INTO personal_records
                (user_id, exercise_id, record_type, value, weight_kg, reps,
                 log_id, session_id, achieved_at)
            VALUES
                (:user_id, :exercise_id, :record_type, :value, :weight_kg, :reps,
                 :log_id, :session_id, :achieved_at)
            ON CONFLICT (user_id, exercise_id, record_type) DO UPDATE
               SET value       = EXCLUDED.value,
                   weight_kg   = EXCLUDED.weight_kg,
                   reps        = EXCLUDED.reps,
                   log_id      = EXCLUDED.log_id,
                   session_id  = EXCLUDED.session_id,
                   achieved_at = EXCLUDED.achieved_at
             WHERE EXCLUDED.value > personal_records.value
            """
        ),
        {
            "user_id": user_id,
            "exercise_id": exercise_id,
            "record_type": record_type,
            "value": round(float(value), 2),
            "weight_kg": weight_kg,
            "reps": reps,
            "log_id": log_id,
            "session_id": session_id,
            "achieved_at": achieved_at,
        },
    )


def check_and_update(
    conn: Connection,
    user_id: int,
    exercise_id: int,
    exercise_name: str,
    weight_kg: Optional[float],
    reps: Optional[int],
    cheat_reps: int = 0,
    is_warmup: bool = False,
    log_id: Optional[int] = None,
    session_id: Optional[int] = None,
    achieved_at: Optional[datetime] = None,
) -> list[RecordBreak]:
    """Record the set's achievements and return whatever it just beat.

    Called once per logged set, inside the same transaction as the insert, so a
    set and the record it set are committed together or not at all.
    """
    found = candidates(weight_kg, reps, cheat_reps, is_warmup)

    existing = current(conn, user_id, exercise_id)

    if session_id is not None and _is_working_set(weight_kg, reps, is_warmup):
        volume = _session_volume(conn, user_id, exercise_id, session_id)
        if volume > 0:
            found[BEST_SESSION_VOLUME] = (volume, None, None)

    if not found:
        return []

    when = achieved_at or datetime.now().astimezone()
    breaks: list[RecordBreak] = []

    for record_type, (value, set_weight, set_reps) in found.items():
        previous = existing.get(record_type)
        previous_value = previous.value if previous else None

        if previous_value is not None and value <= previous_value + MIN_IMPROVEMENT:
            continue

        # Session volume is cumulative, so once this session takes the record
        # every later set in it beats the total this session itself just stored.
        # Announcing that is congratulating somebody for continuing to train.
        #
        # So it is stored — the record really did go up — but reported only the
        # first time this session takes it. `previous.session_id` is how we
        # know: a record already held by THIS session is one we set minutes ago.
        announce = True
        if (
            record_type == BEST_SESSION_VOLUME
            and previous is not None
            and previous.session_id is not None
            and previous.session_id == session_id
        ):
            announce = False

        _upsert(
            conn, user_id, exercise_id, record_type, value,
            set_weight, set_reps, log_id, session_id, when,
        )
        if not announce:
            continue
        breaks.append(
            RecordBreak(
                record_type=record_type,
                exercise_name=exercise_name,
                value=value,
                previous_value=previous_value,
                weight_kg=set_weight,
                reps=set_reps,
            )
        )

    if breaks:
        logger.info(
            "personal record beaten",
            extra={
                "event.action": "personal_record",
                "user.id": user_id,
                "exercise.id": exercise_id,
                "record.types": [b.record_type for b in breaks],
                "record.first_ever": all(b.is_first for b in breaks),
            },
        )
    return breaks


def rebuild(conn: Connection, user_id: int) -> int:
    """Recompute every record for one user from `workout_logs`. Returns the count.

    The answer to three situations: an account that predates migration 003, a
    cache that has drifted, and sets deleted or corrected after the fact —
    where a record may need to come *down*, which the incremental path
    deliberately cannot do.

    Done as three set-based statements rather than by walking the person's sets
    in Python. A few thousand rows is nothing to Postgres and a great deal of
    round trips to a web process.
    """
    conn.execute(
        text("DELETE FROM personal_records WHERE user_id = :user_id"),
        {"user_id": user_id},
    )

    # DISTINCT ON is Postgres-specific and exactly right here: it keeps the
    # first row per exercise under the ORDER BY, so each record arrives with the
    # set that achieved it attached — which a plain GROUP BY MAX() would lose.
    conn.execute(
        text(
            """
            INSERT INTO personal_records
                (user_id, exercise_id, record_type, value, weight_kg, reps,
                 log_id, session_id, achieved_at)
            SELECT DISTINCT ON (exercise_id)
                   user_id, exercise_id, 'heaviest_weight',
                   weight_kg, weight_kg, GREATEST(reps - cheat_reps, 0),
                   log_id, session_id, logged_at
              FROM workout_logs
             WHERE user_id = :user_id
               AND is_warmup = FALSE
               AND weight_kg > 0
               AND reps > 0
               AND (reps - cheat_reps) > 0
             ORDER BY exercise_id, weight_kg DESC, logged_at ASC
            """
        ),
        {"user_id": user_id},
    )

    # Epley in SQL: weight * (1 + clean_reps / 30). Restated here rather than
    # called, because the whole point is to do it in one statement; the unit
    # tests pin it against insights.epley_1rm so the two cannot drift.
    conn.execute(
        text(
            """
            INSERT INTO personal_records
                (user_id, exercise_id, record_type, value, weight_kg, reps,
                 log_id, session_id, achieved_at)
            SELECT DISTINCT ON (exercise_id)
                   user_id, exercise_id, 'best_1rm',
                   ROUND(weight_kg * (1 + (reps - cheat_reps) / 30.0), 2),
                   weight_kg, GREATEST(reps - cheat_reps, 0),
                   log_id, session_id, logged_at
              FROM workout_logs
             WHERE user_id = :user_id
               AND is_warmup = FALSE
               AND weight_kg > 0
               AND reps > 0
               AND (reps - cheat_reps) > 0
             ORDER BY exercise_id,
                      weight_kg * (1 + (reps - cheat_reps) / 30.0) DESC,
                      logged_at ASC
            """
        ),
        {"user_id": user_id},
    )

    # Volume is per session, so only sets that belong to one are eligible.
    # Sets from the journal-paste flow have no session_id and are skipped —
    # correctly: "best session volume" is undefined without sessions.
    conn.execute(
        text(
            """
            INSERT INTO personal_records
                (user_id, exercise_id, record_type, value, log_id, session_id, achieved_at)
            SELECT DISTINCT ON (exercise_id)
                   user_id, exercise_id, 'best_session_volume',
                   total, NULL, session_id, achieved_at
              FROM (
                    SELECT user_id, exercise_id, session_id,
                           SUM(weight_kg * reps) AS total,
                           MAX(logged_at)        AS achieved_at
                      FROM workout_logs
                     WHERE user_id = :user_id
                       AND session_id IS NOT NULL
                       AND is_warmup = FALSE
                       AND weight_kg IS NOT NULL
                       AND reps IS NOT NULL
                     GROUP BY user_id, exercise_id, session_id
                   ) per_session
             WHERE total > 0
             ORDER BY exercise_id, total DESC, achieved_at ASC
            """
        ),
        {"user_id": user_id},
    )

    count = conn.execute(
        text("SELECT count(*) FROM personal_records WHERE user_id = :user_id"),
        {"user_id": user_id},
    ).scalar()

    logger.info(
        "personal records rebuilt",
        extra={
            "event.action": "records_rebuilt",
            "user.id": user_id,
            "record.count": int(count or 0),
        },
    )
    return int(count or 0)
