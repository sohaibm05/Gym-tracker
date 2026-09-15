"""JSON API for the live workout PWA.

The rest of the app renders HTML on the server, and that is the right shape for
a form you fill in once. The live workout screen is not that: a set is logged
every thirty seconds, a rest timer counts down between them, and the phone is on
gym wifi. A full page round-trip per set would lose the timer on every
navigation and leave the screen blank whenever the connection dropped. So that
one screen is a client-side app, and this is what it talks to.

Conventions
-----------
Every route is `/api/...`, returns JSON, and is mounted on the same FastAPI app
as everything else — same process, same database, same session cookie. There is
no second service and no token: the browser already has a session cookie from
logging in, and the PWA is served from the same origin, so it is sent
automatically.

Ownership comes from the cookie, never from the payload. Every handler resolves
`user.user_id` from the session and passes it down; an `exercise_id` or
`routine_id` in a request body is treated as a claim to be checked, not a fact.
The data layers re-check it too — belt and braces, because this is the surface
where a crafted request arrives.

Errors
------
A `ValueError` subclass from a data layer (`RoutineError`, `SessionError`,
`MeasurementError`) carries a message written for the person, and becomes a 400
with that message. Anything else becomes a 500 with no detail, because an
unexpected exception's text is for the logs, not for the browser.
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Any, Callable, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text

import catalog
import measurements as measurements_module
import records
import routines as routines_module
import sessions as sessions_module

logger = logging.getLogger("gym_tracker.api")

router = APIRouter(prefix="/api", tags=["api"])

# Set by app.py at import. Injected rather than imported to avoid a circular
# import: app.py imports this module, so this module cannot import app.py.
_get_engine: Optional[Callable[[], Any]] = None
_require_user: Optional[Callable[..., Any]] = None
# app.LoginRequired. Needed to tell "not signed in" from "the database is
# briefly unreachable", which must not produce the same answer.
_LOGIN_REQUIRED: Optional[type] = None


def configure(
    get_engine: Callable[[], Any],
    require_user: Callable[..., Any],
    login_required: Optional[type] = None,
) -> None:
    """Wire the router to the app's engine and auth dependency."""
    global _get_engine, _require_user, _LOGIN_REQUIRED
    _get_engine = get_engine
    _require_user = require_user
    _LOGIN_REQUIRED = login_required


def current_user(request: Request) -> Any:
    """The signed-in user, or a 401.

    A redirect to the login page is right for a browser following a link and
    wrong for `fetch`: the client would receive the login page's HTML with a
    200 and try to parse it as JSON. A 401 lets the PWA notice the session has
    expired and send the person to log in itself.
    """
    if _require_user is None:  # pragma: no cover - configure() not called
        raise HTTPException(status_code=500, detail="API is not configured")
    try:
        return _require_user(request)
    except HTTPException:
        # Already an HTTP answer with a considered status — the rate limiter's
        # 429, for instance. Re-raising it unchanged matters: rewriting a 429 to
        # a 401 tells somebody being rate-limited to go and log in, at the
        # endpoint that is rate-limiting them.
        raise
    except Exception as exc:
        # Only "no usable session" becomes a 401. Anything else — a dropped
        # database connection, a pool timeout — is a server fault, and reporting
        # it as "sign in to continue" makes the PWA redirect to the login page
        # and throws somebody out of a workout in progress over a blip that
        # would have resolved itself.
        if _LOGIN_REQUIRED is not None and isinstance(exc, _LOGIN_REQUIRED):
            raise HTTPException(status_code=401, detail="Sign in to continue.") from None
        logger.exception(
            "could not resolve the session for an API request",
            extra={"event.action": "api_auth_failed", "url.path": request.url.path},
        )
        raise HTTPException(
            status_code=503, detail="Something went wrong. Try that again."
        ) from exc


def engine() -> Any:
    if _get_engine is None:  # pragma: no cover - configure() not called
        raise HTTPException(status_code=500, detail="API is not configured")
    return _get_engine()


# The exceptions whose message is safe to show. Each data layer raises its own
# subclass of ValueError with wording meant for the person.
_USER_FACING = (
    routines_module.RoutineError,
    sessions_module.SessionError,
    measurements_module.MeasurementError,
)


def _fail(exc: _USER_FACING) -> HTTPException:  # type: ignore[valid-type]
    """Turn a data-layer error into a 400 carrying its message.

    Only ever called from inside `except _USER_FACING`, so the exception is by
    construction one whose message was written for a person to read. There is
    no fallback branch for other exception types, because reaching this with one
    is impossible — an unexpected exception propagates and FastAPI answers 500
    with no detail, which is the right outcome for text meant for the logs.
    """
    return HTTPException(status_code=400, detail=str(exc))


def _parse_date(raw: Optional[str], label: str) -> Optional[date]:
    if not raw:
        return None
    try:
        return datetime.strptime(raw.strip(), "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(status_code=400, detail=f"{label} must be YYYY-MM-DD.") from None


# ==========================================================================
# Catalog
# ==========================================================================


@router.get("/catalog")
async def get_catalog(
    q: str = Query("", max_length=80),
    equipment: Optional[str] = Query(None, max_length=30),
    muscle_group: Optional[str] = Query(None, max_length=40),
    limit: int = Query(50, ge=1, le=200),
    user: Any = Depends(current_user),
) -> JSONResponse:
    """Search the built-in catalog and this user's own exercises together.

    Both, not one or the other: somebody who added "Sohaib Special Curl" last
    month expects to find it in the same search box as everything else. Their
    own exercises rank first on an equal match, because a lift they have
    actually logged is more likely to be the one they mean.
    """
    found = catalog.search(q, equipment=equipment, muscle_group=muscle_group, limit=limit)
    catalog_names = {item.key for item in found}

    with engine().begin() as conn:
        rows = conn.execute(
            text(
                """
                SELECT e.exercise_id, e.name, e.muscle_group, e.equipment, e.is_custom,
                       (SELECT COUNT(*) FROM workout_logs wl
                         WHERE wl.exercise_id = e.exercise_id AND wl.user_id = e.user_id)
                  FROM exercises e
                 WHERE e.user_id = :user_id
                   AND (:q = '' OR e.search_key LIKE :like OR lower(e.name) LIKE :like)
                   AND (CAST(:equipment AS text) IS NULL OR e.equipment = :equipment)
                   AND (CAST(:muscle_group AS text) IS NULL
                        OR lower(e.muscle_group) = lower(:muscle_group))
                 ORDER BY e.name
                 LIMIT :limit
                """
            ),
            {
                "user_id": user.user_id,
                "q": catalog.search_key(q),
                "like": f"%{catalog.search_key(q)}%",
                "equipment": equipment,
                "muscle_group": muscle_group,
                "limit": limit,
            },
        ).fetchall()

    mine = [
        {
            "exercise_id": int(row[0]),
            "name": row[1],
            "muscle_group": row[2],
            "equipment": row[3],
            "equipment_label": catalog.equipment_label(row[3]),
            "is_custom": bool(row[4]),
            "log_count": int(row[5]),
            "in_catalog": catalog.search_key(row[1]) in catalog_names,
        }
        for row in rows
    ]

    # A catalog entry the person already has as an exercise is dropped from the
    # catalog half — otherwise every lift they train appears twice, once with an
    # id and once without, and tapping the wrong one creates a duplicate.
    mine_keys = {catalog.search_key(item["name"]) for item in mine}
    return JSONResponse(
        {
            "mine": mine,
            "catalog": [item.to_dict() for item in found if item.key not in mine_keys],
            "equipment": [
                {"key": key, "label": catalog.EQUIPMENT_LABELS[key]} for key in catalog.EQUIPMENT
            ],
            "muscle_groups": list(catalog.MUSCLE_GROUPS),
        }
    )


@router.post("/exercises")
async def create_exercise(
    payload: dict[str, Any] = Body(...), user: Any = Depends(current_user)
) -> JSONResponse:
    """Resolve a name to one of this user's exercises, creating it if new."""
    name = str(payload.get("name") or "").strip()
    try:
        with engine().begin() as conn:
            exercise_id = sessions_module.resolve_exercise(conn, user.user_id, name)
            row = conn.execute(
                text(
                    "SELECT exercise_id, name, muscle_group, equipment, is_custom "
                    "FROM exercises WHERE exercise_id = :id AND user_id = :user_id"
                ),
                {"id": exercise_id, "user_id": user.user_id},
            ).fetchone()
    except _USER_FACING as exc:
        raise _fail(exc) from None

    return JSONResponse(
        {
            "exercise_id": int(row[0]),
            "name": row[1],
            "muscle_group": row[2],
            "equipment": row[3],
            "equipment_label": catalog.equipment_label(row[3]),
            "is_custom": bool(row[4]),
        },
        status_code=201,
    )


# ==========================================================================
# Routines
# ==========================================================================


@router.get("/routines")
async def list_routines(
    include_archived: bool = Query(False), user: Any = Depends(current_user)
) -> JSONResponse:
    with engine().begin() as conn:
        found = routines_module.list_routines(conn, user.user_id, include_archived)
    return JSONResponse({"routines": [item.to_dict() for item in found]})


@router.get("/routines/{routine_id}")
async def get_routine(routine_id: int, user: Any = Depends(current_user)) -> JSONResponse:
    with engine().begin() as conn:
        found = routines_module.get_routine(conn, user.user_id, routine_id)
    if found is None:
        raise HTTPException(status_code=404, detail="That routine does not exist.")
    return JSONResponse(found.to_dict())


@router.post("/routines")
async def create_routine(
    payload: dict[str, Any] = Body(...), user: Any = Depends(current_user)
) -> JSONResponse:
    try:
        with engine().begin() as conn:
            created = routines_module.create_routine(
                conn,
                user.user_id,
                name=str(payload.get("name") or ""),
                notes=payload.get("notes"),
                exercises=payload.get("exercises") or [],
            )
    except _USER_FACING as exc:
        raise _fail(exc) from None
    return JSONResponse(created.to_dict(), status_code=201)


@router.put("/routines/{routine_id}")
async def update_routine(
    routine_id: int,
    payload: dict[str, Any] = Body(...),
    user: Any = Depends(current_user),
) -> JSONResponse:
    try:
        # Name, notes and contents in one transaction, so a rejected exercise
        # list does not leave a half-applied rename behind.
        with engine().begin() as conn:
            if any(key in payload for key in ("name", "notes", "archived")):
                routines_module.update_routine(
                    conn,
                    user.user_id,
                    routine_id,
                    name=payload.get("name"),
                    notes=payload.get("notes"),
                    archived=payload.get("archived"),
                )
            if "exercises" in payload:
                routines_module.replace_exercises(
                    conn, user.user_id, routine_id, payload.get("exercises") or []
                )
            updated = routines_module.get_routine(conn, user.user_id, routine_id)
    except _USER_FACING as exc:
        raise _fail(exc) from None
    if updated is None:
        raise HTTPException(status_code=404, detail="That routine does not exist.")
    return JSONResponse(updated.to_dict())


@router.delete("/routines/{routine_id}")
async def delete_routine(routine_id: int, user: Any = Depends(current_user)) -> JSONResponse:
    with engine().begin() as conn:
        deleted = routines_module.delete_routine(conn, user.user_id, routine_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="That routine does not exist.")
    return JSONResponse({"deleted": True})


@router.post("/routines/reorder")
async def reorder_routines(
    payload: dict[str, Any] = Body(...), user: Any = Depends(current_user)
) -> JSONResponse:
    ids = [int(value) for value in (payload.get("routine_ids") or [])]
    with engine().begin() as conn:
        routines_module.reorder_routines(conn, user.user_id, ids)
        found = routines_module.list_routines(conn, user.user_id)
    return JSONResponse({"routines": [item.to_dict(include_exercises=False) for item in found]})


# ==========================================================================
# Live sessions
# ==========================================================================


@router.get("/session/active")
async def get_active_session(user: Any = Depends(current_user)) -> JSONResponse:
    """The workout in progress, if any. Polled by the PWA on load.

    This is what makes a locked phone or a dropped connection recoverable: the
    live state is here, not in the browser.
    """
    with engine().begin() as conn:
        session = sessions_module.active_session(conn, user.user_id)
    return JSONResponse({"session": session.to_dict() if session else None})


@router.post("/session/start")
async def start_session(
    payload: dict[str, Any] = Body(default_factory=dict), user: Any = Depends(current_user)
) -> JSONResponse:
    routine_id = payload.get("routine_id")
    try:
        with engine().begin() as conn:
            session = sessions_module.start_session(
                conn,
                user.user_id,
                routine_id=int(routine_id) if routine_id else None,
                name=payload.get("name"),
            )
    except _USER_FACING as exc:
        raise _fail(exc) from None
    return JSONResponse({"session": session.to_dict()}, status_code=201)


@router.get("/session/{session_id}")
async def get_session(session_id: int, user: Any = Depends(current_user)) -> JSONResponse:
    with engine().begin() as conn:
        session = sessions_module.load_session(conn, user.user_id, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="That workout does not exist.")
    return JSONResponse({"session": session.to_dict()})


@router.post("/session/{session_id}/sets")
async def log_set(
    session_id: int,
    payload: dict[str, Any] = Body(...),
    user: Any = Depends(current_user),
) -> JSONResponse:
    """Log one set, and report anything it just beat.

    The response carries the written set, the records broken, and the refreshed
    exercise block. One round trip per set: the screen has everything it needs
    to update without asking again, which matters when each request is a second
    of gym wifi.
    """
    try:
        with engine().begin() as conn:
            written, breaks = sessions_module.log_set(
                conn,
                user_id=user.user_id,
                session_id=session_id,
                exercise_id=payload.get("exercise_id"),
                exercise_name=payload.get("exercise_name"),
                weight_kg=payload.get("weight_kg"),
                reps=payload.get("reps"),
                cheat_reps=payload.get("cheat_reps") or 0,
                is_warmup=bool(payload.get("is_warmup")),
                is_dropset=bool(payload.get("is_dropset")),
                rpe=payload.get("rpe"),
                rir=payload.get("rir"),
                pain_flag=bool(payload.get("pain_flag")),
                notes=payload.get("notes"),
            )
            session = sessions_module.load_session(conn, user.user_id, session_id)
    except _USER_FACING as exc:
        raise _fail(exc) from None

    # The whole refreshed session comes back, not just the one block.
    #
    # The client used to follow every POST with a GET to re-read the session,
    # which meant two round trips and two load_session() calls per logged set —
    # on the connection the module docstring says this has to be cheap on.
    # Returning the session the write already built makes it one.
    return JSONResponse(
        {
            "set": written.to_dict(),
            "records": [b.to_dict() for b in breaks],
            "session": session.to_dict() if session else None,
        },
        status_code=201,
    )


@router.put("/sets/{log_id}")
async def update_set(
    log_id: int, payload: dict[str, Any] = Body(...), user: Any = Depends(current_user)
) -> JSONResponse:
    # Forward only the keys the client actually sent.
    #
    # `payload.get(key)` would pass every field on every call, with None for the
    # ones that were omitted — and `update_set` cannot tell "clear the RPE" from
    # "I did not mention the RPE", so a request correcting a rep count would null
    # the weight and the effort of the set it was fixing. Filtering by presence
    # keeps the two intentions distinct all the way down.
    editable = ("weight_kg", "reps", "cheat_reps", "rpe", "rir", "is_warmup")
    changes = {key: payload[key] for key in editable if key in payload}
    if not changes:
        raise HTTPException(status_code=400, detail="Send at least one field to change.")

    try:
        with engine().begin() as conn:
            updated = sessions_module.update_set(conn, user.user_id, log_id, **changes)
    except _USER_FACING as exc:
        raise _fail(exc) from None
    return JSONResponse({"set": updated.to_dict()})


@router.delete("/sets/{log_id}")
async def delete_set(log_id: int, user: Any = Depends(current_user)) -> JSONResponse:
    with engine().begin() as conn:
        deleted = sessions_module.delete_set(conn, user.user_id, log_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="That set does not exist.")
    return JSONResponse({"deleted": True})


@router.post("/session/{session_id}/finish")
async def finish_session(
    session_id: int,
    payload: dict[str, Any] = Body(default_factory=dict),
    user: Any = Depends(current_user),
) -> JSONResponse:
    try:
        with engine().begin() as conn:
            session = sessions_module.finish_session(
                conn, user.user_id, session_id, notes=payload.get("notes")
            )
    except _USER_FACING as exc:
        raise _fail(exc) from None
    return JSONResponse({"session": session.to_dict(include_exercises=False)})


@router.delete("/session/{session_id}")
async def discard_session(session_id: int, user: Any = Depends(current_user)) -> JSONResponse:
    with engine().begin() as conn:
        deleted = sessions_module.discard_session(conn, user.user_id, session_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="That workout does not exist.")
    return JSONResponse({"deleted": True})


@router.get("/sessions")
async def list_sessions(
    limit: int = Query(20, ge=1, le=100), user: Any = Depends(current_user)
) -> JSONResponse:
    with engine().begin() as conn:
        found = sessions_module.recent_sessions(conn, user.user_id, limit)
    payload = []
    for session in found:
        body = session.to_dict(include_exercises=False)
        total_sets, volume = getattr(session, "_summary", (0, 0.0))
        body["total_sets"] = total_sets
        body["total_volume_kg"] = round(volume, 1)
        payload.append(body)
    return JSONResponse({"sessions": payload})


@router.get("/exercises/{exercise_id}/previous")
async def previous_performance(
    exercise_id: int,
    exclude_session_id: Optional[int] = Query(None),
    user: Any = Depends(current_user),
) -> JSONResponse:
    """What was done last time, for the Previous column."""
    with engine().begin() as conn:
        sets, when = sessions_module.previous_performance(
            conn, user.user_id, exercise_id, before_session_id=exclude_session_id
        )
        current = records.current(conn, user.user_id, exercise_id)
    return JSONResponse(
        {
            "previous": [s.to_dict() for s in sets],
            "previous_date": when.isoformat() if when else None,
            "records": {key: value.to_dict() for key, value in current.items()},
        }
    )


# ==========================================================================
# Personal records
# ==========================================================================


@router.get("/records")
async def list_records(
    limit: int = Query(20, ge=1, le=200), user: Any = Depends(current_user)
) -> JSONResponse:
    with engine().begin() as conn:
        found = records.recent(conn, user.user_id, limit)
    return JSONResponse({"records": [item.to_dict() for item in found]})


@router.post("/records/rebuild")
async def rebuild_records(user: Any = Depends(current_user)) -> JSONResponse:
    """Recompute every record from the logged sets.

    Exposed because the incremental path can only raise a record, never lower
    one — correcting or deleting a set that held a best leaves it stale until
    this runs. Cheap enough to offer as a button and too expensive to run after
    every edit.
    """
    with engine().begin() as conn:
        count = records.rebuild(conn, user.user_id)
    return JSONResponse({"rebuilt": count})


# ==========================================================================
# Measurements
# ==========================================================================


@router.get("/measurements")
async def get_measurements(
    days: int = Query(90, ge=1, le=3650),
    since: Optional[str] = Query(None),
    until: Optional[str] = Query(None),
    user: Any = Depends(current_user),
) -> JSONResponse:
    since_date = _parse_date(since, "since")
    until_date = _parse_date(until, "until")
    with engine().begin() as conn:
        trends = measurements_module.trends(conn, user.user_id, days=days)
        chart = measurements_module.series(
            conn, user.user_id, since=since_date, until=until_date
        )
        weight = measurements_module.bodyweight_series(
            conn, user.user_id, since=since_date, until=until_date
        )
        log = measurements_module.history(conn, user.user_id, limit=100)

    return JSONResponse(
        {
            "trends": [item.to_dict() for item in trends],
            "series": {
                site: [
                    {"at": when.isoformat(), "value_cm": value} for when, value in points
                ]
                for site, points in chart.items()
            },
            "bodyweight": [
                {"at": when.isoformat(), "weight_kg": kg, "body_fat_pct": bf}
                for when, kg, bf in weight
            ],
            "history": [item.to_dict() for item in log],
            "sites": [
                {"key": key, "label": label} for key, label in measurements_module.SITES
            ],
        }
    )


@router.post("/measurements")
async def add_measurements(
    payload: dict[str, Any] = Body(...), user: Any = Depends(current_user)
) -> JSONResponse:
    """Record one sitting: several sites sharing a timestamp."""
    values = payload.get("values")
    if not isinstance(values, dict) or not values:
        raise HTTPException(status_code=400, detail="Send at least one measurement.")

    measured_at: Optional[datetime] = None
    if payload.get("measured_at"):
        parsed = _parse_date(str(payload["measured_at"])[:10], "measured_at")
        if parsed is not None:
            measured_at = datetime.combine(parsed, datetime.min.time()).astimezone()

    try:
        with engine().begin() as conn:
            written = measurements_module.record_many(
                conn, user.user_id, values, measured_at=measured_at
            )
    except _USER_FACING as exc:
        raise _fail(exc) from None
    return JSONResponse(
        {"recorded": [item.to_dict() for item in written]}, status_code=201
    )


@router.delete("/measurements/{measurement_id}")
async def remove_measurement(
    measurement_id: int, user: Any = Depends(current_user)
) -> JSONResponse:
    with engine().begin() as conn:
        deleted = measurements_module.delete(conn, user.user_id, measurement_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="That measurement does not exist.")
    return JSONResponse({"deleted": True})
