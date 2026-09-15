"""Body measurements: circumferences over time, alongside bodyweight.

Two things get tracked here and they live in different tables on purpose.
Bodyweight already had `bodyweight_logs` — the weekly report reads it, and it
carries body-fat percentage — so it stays there. Circumferences are new and go
in `measurements`, in long format: one row per site per date.

Long rather than wide
---------------------
The obvious table has a column per body part. Adding "forearm" to it is a
migration; here it is a new value in an existing column. The cost is that
reading "the latest of everything" needs a window function rather than one row,
which is a few lines once, in `latest()`.

Units
-----
Centimetres in the database, always. The display unit is a presentation concern
and belongs at the edge — storing whatever the person happened to type is how a
dataset ends up with a waist of 32 and a waist of 81 meaning the same thing.
`to_inches` is here for the UI; nothing writes inches.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Optional, Sequence

from sqlalchemy import text
from sqlalchemy.engine import Connection

logger = logging.getLogger("gym_tracker.measurements")

CM_PER_INCH = 2.54

# The sites offered in the UI, in head-to-toe order so the list reads naturally.
# A bounded set keeps the chart's site filter usable and stops "left bicep",
# "Left Bicep" and "l bicep" becoming three lines on one graph.
#
# Not a database constraint: `measurements.site` is free text so somebody can
# track something unusual without a migration. This is the offered list, not the
# permitted one.
SITES: tuple[tuple[str, str], ...] = (
    ("neck", "Neck"),
    ("shoulders", "Shoulders"),
    ("chest", "Chest"),
    ("left_arm", "Left arm"),
    ("right_arm", "Right arm"),
    ("forearm", "Forearm"),
    ("waist", "Waist"),
    ("hips", "Hips"),
    ("left_thigh", "Left thigh"),
    ("right_thigh", "Right thigh"),
    ("left_calf", "Left calf"),
    ("right_calf", "Right calf"),
)

SITE_LABELS = dict(SITES)

# Sites where a smaller number is usually the goal. Used only to colour a
# trend arrow, never to judge anybody: the direction someone wants their waist
# to move is their business, and the app does not comment on it.
LOWER_IS_TYPICALLY_GOAL = frozenset({"waist", "hips", "neck"})

MAX_VALUE_CM = 400.0


class MeasurementError(ValueError):
    """A measurement could not be recorded, with a reason for the person."""


def site_label(site: str) -> str:
    """Human label for a site key, falling back to a tidied version of it."""
    if site in SITE_LABELS:
        return SITE_LABELS[site]
    return site.replace("_", " ").strip().capitalize() or site


def normalise_site(raw: str) -> str:
    """Fold a typed site name to a key: "Left Arm" and "left arm" agree."""
    key = "_".join((raw or "").lower().split())
    if not key:
        raise MeasurementError("A measurement needs a body part.")
    if len(key) > 40:
        raise MeasurementError("That body part name is too long.")
    return key


def to_inches(value_cm: Optional[float]) -> Optional[float]:
    return None if value_cm is None else round(float(value_cm) / CM_PER_INCH, 1)


@dataclass(frozen=True)
class Measurement:
    measurement_id: int
    site: str
    value_cm: float
    measured_at: datetime
    notes: Optional[str] = None

    @property
    def label(self) -> str:
        return site_label(self.site)

    def to_dict(self) -> dict[str, Any]:
        return {
            "measurement_id": self.measurement_id,
            "site": self.site,
            "label": self.label,
            "value_cm": round(float(self.value_cm), 1),
            "value_in": to_inches(self.value_cm),
            "measured_at": self.measured_at.isoformat(),
            "notes": self.notes,
        }


@dataclass
class SiteTrend:
    """One site's latest value and how it has moved over the window."""

    site: str
    latest_cm: float
    latest_at: datetime
    first_cm: Optional[float] = None
    first_at: Optional[datetime] = None
    reading_count: int = 1

    @property
    def label(self) -> str:
        return site_label(self.site)

    @property
    def change_cm(self) -> Optional[float]:
        if self.first_cm is None:
            return None
        return round(float(self.latest_cm) - float(self.first_cm), 1)

    @property
    def change_pct(self) -> Optional[float]:
        if not self.first_cm:
            return None
        return round((float(self.latest_cm) / float(self.first_cm) - 1) * 100, 1)

    def to_dict(self) -> dict[str, Any]:
        return {
            "site": self.site,
            "label": self.label,
            "latest_cm": round(float(self.latest_cm), 1),
            "latest_in": to_inches(self.latest_cm),
            "latest_at": self.latest_at.isoformat(),
            "first_cm": round(float(self.first_cm), 1) if self.first_cm is not None else None,
            "first_at": self.first_at.isoformat() if self.first_at else None,
            "change_cm": self.change_cm,
            "change_pct": self.change_pct,
            "reading_count": self.reading_count,
            "lower_is_typically_goal": self.site in LOWER_IS_TYPICALLY_GOAL,
        }


# --------------------------------------------------------------------------
# Writes
# --------------------------------------------------------------------------


def record(
    conn: Connection,
    user_id: int,
    site: str,
    value_cm: Any,
    measured_at: Optional[datetime] = None,
    notes: Optional[str] = None,
) -> Measurement:
    """Store one reading."""
    key = normalise_site(site)
    try:
        value = float(value_cm)
    except (TypeError, ValueError):
        raise MeasurementError("A measurement must be a number.") from None
    if not 0 < value <= MAX_VALUE_CM:
        # The upper bound catches inches typed into a centimetre field and a
        # stray extra digit, both of which otherwise ruin the chart's scale and
        # are tedious to find later.
        raise MeasurementError(f"That measurement must be between 0 and {MAX_VALUE_CM:g} cm.")

    row = conn.execute(
        text(
            "INSERT INTO measurements (user_id, site, value_cm, measured_at, notes) "
            "VALUES (:user_id, :site, :value_cm, COALESCE(:measured_at, now()), :notes) "
            "RETURNING measurement_id, measured_at"
        ),
        {
            "user_id": user_id,
            "site": key,
            "value_cm": round(value, 1),
            "measured_at": measured_at,
            "notes": (notes or "").strip() or None,
        },
    ).fetchone()

    logger.info(
        "measurement recorded",
        extra={
            "event.action": "measurement_recorded",
            "user.id": user_id,
            "measurement.site": key,
        },
    )
    return Measurement(
        measurement_id=int(row[0]),
        site=key,
        value_cm=round(value, 1),
        measured_at=row[1],
        notes=(notes or "").strip() or None,
    )


def record_many(
    conn: Connection,
    user_id: int,
    values: dict[str, Any],
    measured_at: Optional[datetime] = None,
) -> list[Measurement]:
    """Store several sites measured at one sitting.

    Sharing a timestamp matters: measurements taken together should line up on
    the same x position on a chart, and letting each row default to now() would
    scatter them across the seconds it took to type them in.
    """
    when = measured_at or datetime.now().astimezone()
    written: list[Measurement] = []
    for site, value in values.items():
        if value in (None, "", "None"):
            continue  # a blank field is "not measured today", not zero
        written.append(record(conn, user_id, site, value, measured_at=when))
    return written


def delete(conn: Connection, user_id: int, measurement_id: int) -> bool:
    deleted = conn.execute(
        text(
            "DELETE FROM measurements "
            "WHERE measurement_id = :measurement_id AND user_id = :user_id"
        ),
        {"measurement_id": measurement_id, "user_id": user_id},
    ).rowcount
    return bool(deleted)


# --------------------------------------------------------------------------
# Reads
# --------------------------------------------------------------------------


def latest(conn: Connection, user_id: int) -> dict[str, Measurement]:
    """The most recent reading for each site.

    DISTINCT ON is the Postgres way to say "first row per group under this
    ordering" and keeps the whole row, which a GROUP BY MAX(measured_at) would
    not — that would need a self-join to get the value back.
    """
    rows = conn.execute(
        text(
            """
            SELECT DISTINCT ON (site)
                   measurement_id, site, value_cm, measured_at, notes
              FROM measurements
             WHERE user_id = :user_id
             ORDER BY site, measured_at DESC
            """
        ),
        {"user_id": user_id},
    ).fetchall()
    return {
        row[1]: Measurement(
            measurement_id=int(row[0]),
            site=row[1],
            value_cm=float(row[2]),
            measured_at=row[3],
            notes=row[4],
        )
        for row in rows
    }


def series(
    conn: Connection,
    user_id: int,
    sites: Optional[Sequence[str]] = None,
    since: Optional[date] = None,
    until: Optional[date] = None,
) -> dict[str, list[tuple[datetime, float]]]:
    """Readings per site over a window, oldest first — what a chart plots.

    Casts are written CAST(:name AS type), never :name::type. SQLAlchemy's
    text() scans the string for bind parameters itself and does not understand
    the Postgres :: operator beside one — it reads ":sites::text" as a parameter
    called `sites::text`, leaves the real placeholder unsubstituted, and the
    statement fails at the database.

    That scan covers SQL comments too, so the explanation lives here rather than
    inside the query: a comment mentioning a colon-name is itself read as a bind
    parameter, and the statement then fails asking for a value nobody meant to
    pass.
    """
    rows = conn.execute(
        text(
            """
            SELECT site, measured_at, value_cm
              FROM measurements
             WHERE user_id = :user_id
               AND (CAST(:sites AS text[]) IS NULL OR site = ANY(CAST(:sites AS text[])))
               AND (CAST(:since AS date) IS NULL OR measured_at >= CAST(:since AS date))
               AND (CAST(:until AS date) IS NULL OR measured_at < CAST(:until AS date) + 1)
             ORDER BY site, measured_at
            """
        ),
        {
            "user_id": user_id,
            "sites": list(sites) if sites else None,
            "since": since,
            "until": until,
        },
    ).fetchall()

    grouped: dict[str, list[tuple[datetime, float]]] = {}
    for site, measured_at, value in rows:
        grouped.setdefault(site, []).append((measured_at, float(value)))
    return grouped


def trends(
    conn: Connection, user_id: int, days: int = 90
) -> list[SiteTrend]:
    """Latest value and movement per site across the trailing window.

    The comparison is the first reading *inside* the window, not the all-time
    first — "since I started tracking" and "over the last 90 days" are different
    questions, and the tiles on the measurements screen ask the second.
    """
    since = datetime.now().astimezone() - timedelta(days=max(1, int(days)))
    rows = conn.execute(
        text(
            """
            SELECT site,
                   (ARRAY_AGG(value_cm  ORDER BY measured_at DESC))[1] AS latest_cm,
                   (ARRAY_AGG(measured_at ORDER BY measured_at DESC))[1] AS latest_at,
                   (ARRAY_AGG(value_cm  ORDER BY measured_at ASC))[1]  AS first_cm,
                   (ARRAY_AGG(measured_at ORDER BY measured_at ASC))[1] AS first_at,
                   COUNT(*)
              FROM measurements
             WHERE user_id = :user_id AND measured_at >= :since
             GROUP BY site
            """
        ),
        {"user_id": user_id, "since": since},
    ).fetchall()

    built = [
        SiteTrend(
            site=row[0],
            latest_cm=float(row[1]),
            latest_at=row[2],
            # One reading in the window is a latest with nothing to compare to,
            # so the change is None rather than a misleading zero.
            first_cm=float(row[3]) if row[5] > 1 else None,
            first_at=row[4] if row[5] > 1 else None,
            reading_count=int(row[5]),
        )
        for row in rows
    ]

    # Ordered by the canonical site list so the tiles keep a stable, head-to-toe
    # order between visits; anything unrecognised follows, alphabetically.
    order = {key: index for index, (key, _) in enumerate(SITES)}
    built.sort(key=lambda t: (order.get(t.site, len(order)), t.site))
    return built


def bodyweight_series(
    conn: Connection,
    user_id: int,
    since: Optional[date] = None,
    until: Optional[date] = None,
) -> list[tuple[datetime, float, Optional[float]]]:
    """Bodyweight readings over a window: (when, kg, body-fat %).

    Reads `bodyweight_logs` rather than `measurements` — bodyweight predates
    this module and the weekly report already depends on that table. Exposed
    here so the measurements screen can chart weight and circumferences
    together without its caller needing to know they live apart.
    """
    rows = conn.execute(
        text(
            """
            SELECT logged_at, weight_kg, body_fat_pct
              FROM bodyweight_logs
             WHERE user_id = :user_id
               AND (CAST(:since AS date) IS NULL OR logged_at >= CAST(:since AS date))
               AND (CAST(:until AS date) IS NULL OR logged_at < CAST(:until AS date) + 1)
             ORDER BY logged_at
            """
        ),
        {"user_id": user_id, "since": since, "until": until},
    ).fetchall()
    return [
        (row[0], float(row[1]), float(row[2]) if row[2] is not None else None)
        for row in rows
    ]


def history(
    conn: Connection, user_id: int, limit: int = 100
) -> list[Measurement]:
    """Recent readings across every site, newest first — the log view."""
    rows = conn.execute(
        text(
            "SELECT measurement_id, site, value_cm, measured_at, notes "
            "FROM measurements WHERE user_id = :user_id "
            "ORDER BY measured_at DESC, measurement_id DESC LIMIT :limit"
        ),
        {"user_id": user_id, "limit": max(1, min(int(limit), 500))},
    ).fetchall()
    return [
        Measurement(
            measurement_id=int(row[0]),
            site=row[1],
            value_cm=float(row[2]),
            measured_at=row[3],
            notes=row[4],
        )
        for row in rows
    ]
