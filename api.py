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
no token: the browser already has a session cookie from logging in, and the PWA
is served from the same origin, so it is sent automatically. A script can sign
in with HTTP Basic instead (see `app.require_user`).

Paths are nouns, plural for collections (`/sessions`, `/routines`, `/sets`),
and the method says what happens to them. Two actions that are not CRUD on a
resource keep a verb sub-path — `/sessions/{id}/finish` and `/records/rebuild` —
because modelling them as a field update would hide what they actually do.
The singular `/session/...` paths from before Assignment 2 still answer, marked
deprecated: an installed PWA keeps its cached app.js for one launch after a
deploy, and its offline outbox must not lose queued sets to a 404.

Ownership comes from the cookie, never from the payload. Every handler resolves
`user.user_id` from the session and passes it down; an `exercise_id` or
`routine_id` in a request body is treated as a claim to be checked, not a fact.
The data layers re-check it too — belt and braces, because this is the surface
where a crafted request arrives.

Errors
------
Every error under /api has one shape, whatever produced it:

    {"error": {"code": "not_found", "message": "That workout does not exist.",
               "request_id": "9f2c41d07ab3e811"}}

`message` is written for the person; `code` is for the program and maps 1:1 to
the status (see ERROR_CODES). `request_id` is the same id that is in every log
line for the request and in the X-Request-ID response header.

A data layer's `ValueError` subclass carries a message written for the person:
SessionNotFound becomes 404, SessionConflict 409, anything else of theirs 400.
FastAPI's own body/query validation is also answered 400, so "your input is
wrong" is one status, not two. Anything unexpected becomes a 500 with no
detail, because an unexpected exception's text is for the logs.
"""

from __future__ import annotations

import logging
import os
from datetime import date, datetime
from typing import Any, Callable, Optional

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text
from starlette.exceptions import HTTPException as StarletteHTTPException

import auth
import catalog
import measurements as measurements_module
import records
import routines as routines_module
import sessions as sessions_module

try:
    import logging_setup
except Exception:  # pragma: no cover - the API works without request ids
    logging_setup = None  # type: ignore[assignment]

try:
    from metrics import METRICS
except Exception:  # pragma: no cover - the API works unobserved
    METRICS = None  # type: ignore[assignment]

logger = logging.getLogger("gym_tracker.api")


# ==========================================================================
# Error format
# ==========================================================================

ERROR_CODES = {
    400: "invalid_input",
    401: "unauthenticated",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    422: "invalid_input",
    429: "rate_limited",
    500: "internal_error",
    503: "unavailable",
}


class ErrorDetail(BaseModel):
    code: str = Field(description="Machine-readable; one per status, see ERROR_CODES.")
    message: str = Field(description="Written for the person using the app.")
    request_id: str = Field(description="Search Kibana for this as http.request.id.")


class ErrorBody(BaseModel):
    error: ErrorDetail


def _request_id() -> str:
    return logging_setup.get_request_id() if logging_setup is not None else ""


def error_response(
    status_code: int, message: str, headers: Optional[dict[str, str]] = None
) -> JSONResponse:
    """The one error shape every /api route answers with."""
    return JSONResponse(
        {
            "error": {
                "code": ERROR_CODES.get(status_code, "error"),
                "message": message,
                "request_id": _request_id(),
            }
        },
        status_code=status_code,
        headers=headers,
    )


def _is_api(request: Request) -> bool:
    return request.url.path.startswith("/api/")


def install_error_handlers(app: Any) -> None:
    """Answer /api errors in the shared format; leave every other path alone.

    Registered on the app rather than the router because FastAPI only supports
    exception handlers at app level. Outside /api each handler hands straight
    back to FastAPI's default, so the HTML pages behave exactly as before.
    """
    from fastapi.exception_handlers import (
        http_exception_handler,
        request_validation_exception_handler,
    )

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException):
        if not _is_api(request):
            return await http_exception_handler(request, exc)
        message = exc.detail if isinstance(exc.detail, str) else "Request failed."
        if exc.status_code == 404 and message == "Not Found":
            message = "There is no such API route."
        return error_response(exc.status_code, message, getattr(exc, "headers", None))

    @app.exception_handler(RequestValidationError)
    async def _invalid_input(request: Request, exc: RequestValidationError):
        if not _is_api(request):
            return await request_validation_exception_handler(request, exc)
        # The first problem, named by where it is. One clear sentence the PWA can
        # put in a toast beats Pydantic's full list of every failed constraint.
        first = exc.errors()[0] if exc.errors() else {}
        where = ".".join(str(part) for part in first.get("loc", ()) if part != "body")
        message = f"{where}: {first.get('msg', 'invalid value')}" if where else "Invalid request."
        return error_response(400, message)

    @app.exception_handler(Exception)
    async def _unexpected(request: Request, exc: Exception):
        # Runs in Starlette's outermost middleware, after the request middleware
        # in app.py has already logged the traceback and counted the 500.
        if not _is_api(request):
            return PlainTextResponse("Internal Server Error", status_code=500)
        return error_response(500, "Something went wrong on our side.")


_ERR = {"model": ErrorBody}
# Every route can answer these two.
router = APIRouter(
    prefix="/api",
    tags=["api"],
    responses={
        401: {**_ERR, "description": "Not signed in, or the session expired."},
        500: {**_ERR, "description": "Unexpected server error. Details are in the logs."},
    },
)
BAD_INPUT = {400: {**_ERR, "description": "The input failed validation."}}
NOT_FOUND = {404: {**_ERR, "description": "No such resource for this account."}}
CONFLICT = {409: {**_ERR, "description": "Valid request refused by the resource's state."}}


# ==========================================================================
# Wiring
# ==========================================================================

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
    """Turn a data-layer error into an HTTP error carrying its message.

    Only ever called from inside `except _USER_FACING`, so the exception is by
    construction one whose message was written for a person to read. The status
    comes from the exception's class — the same classes grpc_server.py maps to
    gRPC codes, so the two transports cannot disagree about what went wrong.
    """
    if isinstance(exc, sessions_module.SessionNotFound):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, sessions_module.SessionConflict):
        return HTTPException(status_code=409, detail=str(exc))
    return HTTPException(status_code=400, detail=str(exc))


def _parse_date(raw: Optional[str], label: str) -> Optional[date]:
    if not raw:
        return None
    try:
        return datetime.strptime(raw.strip(), "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(status_code=400, detail=f"{label} must be YYYY-MM-DD.") from None


# ==========================================================================
# Request and response bodies
# ==========================================================================
#
# These check *types* — an int is an int, a list is a list — and nothing more.
# Bounds and business rules (reps 1-500, cheat reps <= reps, "that workout is
# finished") live in the data layers, because grpc_server.py calls the same
# functions and has to get the same answers. Unknown fields are ignored rather
# than refused, so an older client sending one extra key is not broken.


class _In(BaseModel):
    model_config = ConfigDict(extra="ignore")


class CreateExerciseIn(_In):
    name: str = Field(min_length=1, max_length=200)


class RoutineIn(_In):
    name: Optional[str] = None
    notes: Optional[str] = None
    archived: Optional[bool] = None
    # Each item's fields are validated by routines.validate_exercise_list, which
    # owns the rules (targets, ranges) and the wording of the messages.
    exercises: Optional[list[dict[str, Any]]] = None


class ReorderIn(_In):
    routine_ids: list[int]


class StartSessionIn(_In):
    routine_id: Optional[int] = Field(None, ge=1)
    name: Optional[str] = Field(None, max_length=80)


class LogSetIn(_In):
    """One set. Name the exercise by id (from the catalog) or by name."""

    exercise_id: Optional[int] = Field(None, ge=1)
    exercise_name: Optional[str] = Field(None, max_length=200)
    weight_kg: Optional[float] = Field(None, description="Omit for a bodyweight set.")
    reps: Optional[int] = Field(None, description="Required. 1-500.")
    cheat_reps: Optional[int] = Field(0, description="0 to reps.")
    is_warmup: bool = False
    is_dropset: bool = False
    rpe: Optional[float] = Field(None, description="1-10.")
    rir: Optional[int] = Field(None, description="0-20.")
    pain_flag: bool = False
    notes: Optional[str] = Field(None, max_length=1000)


class UpdateSetIn(_In):
    """Only the fields sent are changed. Send null to clear one."""

    weight_kg: Optional[float] = None
    reps: Optional[int] = None
    cheat_reps: Optional[int] = None
    rpe: Optional[float] = None
    rir: Optional[int] = None
    is_warmup: Optional[bool] = None


class FinishSessionIn(_In):
    notes: Optional[str] = Field(None, max_length=1000)


class MeasurementsIn(_In):
    values: dict[str, Any] = Field(description='Site -> value in cm, e.g. {"waist": 81.5}.')
    measured_at: Optional[str] = Field(None, description="YYYY-MM-DD; defaults to now.")


class LoggedSetOut(BaseModel):
    log_id: int
    exercise_id: int
    exercise_name: str
    set_number: int
    weight_kg: Optional[float]
    reps: Optional[int]
    cheat_reps: int
    is_warmup: bool
    is_dropset: bool
    rpe: Optional[float]
    rir: Optional[int]
    pain_flag: bool
    notes: Optional[str]
    volume_kg: float
    display: str
    logged_at: Optional[str]


class RecordBreakOut(BaseModel):
    record_type: str
    label: str
    exercise_name: str
    value: float
    previous_value: Optional[float]


class LogSetOut(BaseModel):
    set: LoggedSetOut
    records: list[RecordBreakOut] = Field(
        description="Personal records this set beat. Always empty on a replay."
    )
    session: Optional[dict[str, Any]] = Field(description="The refreshed workout.")


class DeletedOut(BaseModel):
    deleted: bool


class RebuiltOut(BaseModel):
    rebuilt: int


# ==========================================================================
# Catalog
# ==========================================================================


@router.get("/catalog", responses=BAD_INPUT)
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


@router.post("/exercises", status_code=201, responses=BAD_INPUT)
async def create_exercise(
    payload: CreateExerciseIn, user: Any = Depends(current_user)
) -> JSONResponse:
    """Resolve a name to one of this user's exercises, creating it if new."""
    try:
        with engine().begin() as conn:
            exercise_id = sessions_module.resolve_exercise(
                conn, user.user_id, payload.name.strip()
            )
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


@router.get("/routines/{routine_id}", responses=NOT_FOUND)
async def get_routine(routine_id: int, user: Any = Depends(current_user)) -> JSONResponse:
    with engine().begin() as conn:
        found = routines_module.get_routine(conn, user.user_id, routine_id)
    if found is None:
        raise HTTPException(status_code=404, detail="That routine does not exist.")
    return JSONResponse(found.to_dict())


@router.post("/routines", status_code=201, responses=BAD_INPUT)
async def create_routine(payload: RoutineIn, user: Any = Depends(current_user)) -> JSONResponse:
    try:
        with engine().begin() as conn:
            created = routines_module.create_routine(
                conn,
                user.user_id,
                name=payload.name or "",
                notes=payload.notes,
                exercises=payload.exercises or [],
            )
    except _USER_FACING as exc:
        raise _fail(exc) from None
    return JSONResponse(created.to_dict(), status_code=201)


@router.patch("/routines/{routine_id}", responses={**BAD_INPUT, **NOT_FOUND})
async def update_routine(
    routine_id: int, payload: RoutineIn, user: Any = Depends(current_user)
) -> JSONResponse:
    """Change a routine. Only the fields sent are touched.

    PATCH, not PUT: a body with just `{"archived": true}` archives the routine
    and leaves its name and exercises alone. That is a partial update, and PUT
    promises the opposite — replace the resource with exactly what was sent.
    """
    sent = payload.model_fields_set
    try:
        # Name, notes and contents in one transaction, so a rejected exercise
        # list does not leave a half-applied rename behind.
        with engine().begin() as conn:
            if sent & {"name", "notes", "archived"}:
                routines_module.update_routine(
                    conn,
                    user.user_id,
                    routine_id,
                    name=payload.name,
                    notes=payload.notes,
                    archived=payload.archived,
                )
            if "exercises" in sent:
                routines_module.replace_exercises(
                    conn, user.user_id, routine_id, payload.exercises or []
                )
            updated = routines_module.get_routine(conn, user.user_id, routine_id)
    except _USER_FACING as exc:
        raise _fail(exc) from None
    if updated is None:
        raise HTTPException(status_code=404, detail="That routine does not exist.")
    return JSONResponse(updated.to_dict())


@router.delete("/routines/{routine_id}", response_model=DeletedOut, responses=NOT_FOUND)
async def delete_routine(routine_id: int, user: Any = Depends(current_user)) -> JSONResponse:
    with engine().begin() as conn:
        deleted = routines_module.delete_routine(conn, user.user_id, routine_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="That routine does not exist.")
    return JSONResponse({"deleted": True})


@router.post("/routines/reorder", responses=BAD_INPUT)
async def reorder_routines(payload: ReorderIn, user: Any = Depends(current_user)) -> JSONResponse:
    with engine().begin() as conn:
        routines_module.reorder_routines(conn, user.user_id, payload.routine_ids)
        found = routines_module.list_routines(conn, user.user_id)
    return JSONResponse({"routines": [item.to_dict(include_exercises=False) for item in found]})


# ==========================================================================
# Live sessions
# ==========================================================================


@router.get("/sessions/active")
async def get_active_session(user: Any = Depends(current_user)) -> JSONResponse:
    """The workout in progress, if any. Polled by the PWA on load.

    This is what makes a locked phone or a dropped connection recoverable: the
    live state is here, not in the browser.
    """
    with engine().begin() as conn:
        session = sessions_module.active_session(conn, user.user_id)
    return JSONResponse({"session": session.to_dict() if session else None})


@router.post("/sessions", status_code=201, responses={**BAD_INPUT, **NOT_FOUND})
async def start_session(
    payload: StartSessionIn = Body(default_factory=StartSessionIn),
    user: Any = Depends(current_user),
) -> JSONResponse:
    """Start a workout, or return the one already in progress.

    201 when a workout was created, 200 when one was already live. Starting is
    safe to repeat - a double tap, or a retry - because there is only ever one
    live workout per person, and the status says which happened.
    """
    try:
        with engine().begin() as conn:
            already = sessions_module.active_session(conn, user.user_id)
            session = already or sessions_module.start_session(
                conn, user.user_id, routine_id=payload.routine_id, name=payload.name
            )
    except _USER_FACING as exc:
        raise _fail(exc) from None
    return JSONResponse({"session": session.to_dict()}, status_code=200 if already else 201)


@router.get("/sessions/{session_id}", responses=NOT_FOUND)
async def get_session(session_id: int, user: Any = Depends(current_user)) -> JSONResponse:
    with engine().begin() as conn:
        session = sessions_module.load_session(conn, user.user_id, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="That workout does not exist.")
    return JSONResponse({"session": session.to_dict()})


@router.post(
    "/sessions/{session_id}/sets",
    status_code=201,
    response_model=LogSetOut,
    responses={
        200: {
            "model": LogSetOut,
            "description": "Replay: this Idempotency-Key was already used, so the set "
            "it wrote is returned and nothing new is written. "
            "The response carries `Idempotent-Replayed: true`.",
        },
        201: {"description": "The set was written."},
        **BAD_INPUT,
        **NOT_FOUND,
        **CONFLICT,
    },
)
async def log_set(
    session_id: int,
    payload: LogSetIn,
    idempotency_key: Optional[str] = Header(
        None,
        max_length=sessions_module.MAX_IDEMPOTENCY_KEY,
        description="A new random id per set (a UUID), resent unchanged on every "
        "retry of that set. Makes the retry safe: the set is written at most once.",
    ),
    user: Any = Depends(current_user),
) -> JSONResponse:
    """Log one set, and report anything it just beat.

    The response carries the written set, the records broken, and the refreshed
    session. One round trip per set: the screen has everything it needs to
    update without asking again, which matters when each request is a second of
    gym wifi.
    """
    try:
        with engine().begin() as conn:
            written, breaks = sessions_module.log_set(
                conn,
                user_id=user.user_id,
                session_id=session_id,
                exercise_id=payload.exercise_id,
                exercise_name=payload.exercise_name,
                weight_kg=payload.weight_kg,
                reps=payload.reps,
                cheat_reps=payload.cheat_reps or 0,
                is_warmup=payload.is_warmup,
                is_dropset=payload.is_dropset,
                rpe=payload.rpe,
                rir=payload.rir,
                pain_flag=payload.pain_flag,
                notes=payload.notes,
                idempotency_key=idempotency_key,
            )
            session = sessions_module.load_session(conn, user.user_id, session_id)
    except _USER_FACING as exc:
        raise _fail(exc) from None

    if METRICS is not None:
        METRICS.live_sets_total.labels(
            "http", "replayed" if written.replayed else "written"
        ).inc()

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
        status_code=200 if written.replayed else 201,
        headers={"Idempotent-Replayed": "true"} if written.replayed else None,
    )


@router.patch("/sets/{log_id}", responses={**BAD_INPUT, **NOT_FOUND})
async def update_set(
    log_id: int, payload: UpdateSetIn, user: Any = Depends(current_user)
) -> JSONResponse:
    """Correct a set. PATCH: only the fields sent change; null clears one."""
    # Forward only the keys the client actually sent.
    #
    # Passing every field would send None for the ones that were omitted — and
    # `update_set` cannot tell "clear the RPE" from "I did not mention the RPE",
    # so a request correcting a rep count would null the weight and the effort
    # of the set it was fixing. Filtering by presence keeps the two intentions
    # distinct all the way down.
    changes = {key: getattr(payload, key) for key in payload.model_fields_set}
    if not changes:
        raise HTTPException(status_code=400, detail="Send at least one field to change.")

    try:
        with engine().begin() as conn:
            updated = sessions_module.update_set(conn, user.user_id, log_id, **changes)
    except _USER_FACING as exc:
        raise _fail(exc) from None
    return JSONResponse({"set": updated.to_dict()})


@router.delete("/sets/{log_id}", response_model=DeletedOut, responses=NOT_FOUND)
async def delete_set(log_id: int, user: Any = Depends(current_user)) -> JSONResponse:
    with engine().begin() as conn:
        deleted = sessions_module.delete_set(conn, user.user_id, log_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="That set does not exist.")
    return JSONResponse({"deleted": True})


@router.post("/sessions/{session_id}/finish", responses={**BAD_INPUT, **NOT_FOUND})
async def finish_session(
    session_id: int,
    payload: FinishSessionIn = Body(default_factory=FinishSessionIn),
    user: Any = Depends(current_user),
) -> JSONResponse:
    """Finish a workout. Safe to repeat: finishing a finished one returns it."""
    try:
        with engine().begin() as conn:
            session = sessions_module.finish_session(
                conn, user.user_id, session_id, notes=payload.notes
            )
    except _USER_FACING as exc:
        raise _fail(exc) from None
    return JSONResponse({"session": session.to_dict(include_exercises=False)})


@router.delete("/sessions/{session_id}", response_model=DeletedOut, responses=NOT_FOUND)
async def discard_session(session_id: int, user: Any = Depends(current_user)) -> JSONResponse:
    with engine().begin() as conn:
        deleted = sessions_module.discard_session(conn, user.user_id, session_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="That workout does not exist.")
    return JSONResponse({"deleted": True})


@router.get("/sessions", responses=BAD_INPUT)
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


@router.get("/exercises/{exercise_id}/previous", responses=BAD_INPUT)
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


@router.get("/records", responses=BAD_INPUT)
async def list_records(
    limit: int = Query(20, ge=1, le=200), user: Any = Depends(current_user)
) -> JSONResponse:
    with engine().begin() as conn:
        found = records.recent(conn, user.user_id, limit)
    return JSONResponse({"records": [item.to_dict() for item in found]})


# Rebuilding re-reads every set the person has ever logged, so it is the one
# call here whose cost grows without bound - and it is behind a button that is
# easy to tap repeatedly. The limit is per signed-in account (not per IP, which
# a gym's shared wifi would turn into one bucket for everybody), counted in this
# process only. A sliding window: a call is allowed once fewer than LIMIT calls
# from that account fall in the last WINDOW seconds.
REBUILD_LIMIT = int(os.getenv("RECORDS_REBUILD_LIMIT", "3"))
REBUILD_WINDOW_SECONDS = float(os.getenv("RECORDS_REBUILD_WINDOW_SECONDS", "60"))
REBUILD_LIMITER = auth.RateLimiter(REBUILD_LIMIT, REBUILD_WINDOW_SECONDS)


@router.post(
    "/records/rebuild",
    response_model=RebuiltOut,
    responses={
        429: {
            **_ERR,
            "description": f"More than {REBUILD_LIMIT} rebuilds by this account in "
            f"{REBUILD_WINDOW_SECONDS:g}s. `Retry-After` says how many seconds to wait.",
            "headers": {
                "Retry-After": {
                    "description": "Seconds until a call is allowed again.",
                    "schema": {"type": "integer"},
                }
            },
        }
    },
)
async def rebuild_records(user: Any = Depends(current_user)) -> JSONResponse:
    """Recompute every record from the logged sets. Rate limited per account.

    Exposed because the incremental path can only raise a record, never lower
    one — correcting or deleting a set that held a best leaves it stale until
    this runs. Idempotent: running it twice gives the same records as once.
    """
    key = f"user:{user.user_id}"
    if not REBUILD_LIMITER.check(key):
        wait = REBUILD_LIMITER.retry_after(key)
        logger.warning(
            "records rebuild rate limited",
            extra={
                "event.action": "rate_limited",
                "user.id": user.user_id,
                "rate_limit.retry_after_s": wait,
            },
        )
        raise HTTPException(
            status_code=429,
            detail=f"Records were just recalculated. Try again in {wait} seconds.",
            headers={"Retry-After": str(wait)},
        )
    with engine().begin() as conn:
        count = records.rebuild(conn, user.user_id)
    return JSONResponse({"rebuilt": count})


# ==========================================================================
# Measurements
# ==========================================================================


@router.get("/measurements", responses=BAD_INPUT)
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


@router.post("/measurements", status_code=201, responses=BAD_INPUT)
async def add_measurements(
    payload: MeasurementsIn, user: Any = Depends(current_user)
) -> JSONResponse:
    """Record one sitting: several sites sharing a timestamp."""
    if not payload.values:
        raise HTTPException(status_code=400, detail="Send at least one measurement.")

    measured_at: Optional[datetime] = None
    if payload.measured_at:
        parsed = _parse_date(payload.measured_at[:10], "measured_at")
        if parsed is not None:
            measured_at = datetime.combine(parsed, datetime.min.time()).astimezone()

    try:
        with engine().begin() as conn:
            written = measurements_module.record_many(
                conn, user.user_id, payload.values, measured_at=measured_at
            )
    except _USER_FACING as exc:
        raise _fail(exc) from None
    return JSONResponse(
        {"recorded": [item.to_dict() for item in written]}, status_code=201
    )


@router.delete(
    "/measurements/{measurement_id}", response_model=DeletedOut, responses=NOT_FOUND
)
async def remove_measurement(
    measurement_id: int, user: Any = Depends(current_user)
) -> JSONResponse:
    with engine().begin() as conn:
        deleted = measurements_module.delete(conn, user.user_id, measurement_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="That measurement does not exist.")
    return JSONResponse({"deleted": True})


# ==========================================================================
# Deprecated paths
# ==========================================================================
#
# The paths as they were before Assignment 2, still answering, so a phone
# running last week's cached app.js can drain its offline outbox instead of
# having every queued set refused with a 404 and dropped. Same handlers, so
# same rules; `deprecated` marks them in the OpenAPI document.

for _path, _endpoint, _method in (
    ("/session/active", get_active_session, "GET"),
    ("/session/start", start_session, "POST"),
    ("/session/{session_id}", get_session, "GET"),
    ("/session/{session_id}/sets", log_set, "POST"),
    ("/session/{session_id}/finish", finish_session, "POST"),
    ("/session/{session_id}", discard_session, "DELETE"),
    ("/routines/{routine_id}", update_routine, "PUT"),
    ("/sets/{log_id}", update_set, "PUT"),
):
    router.add_api_route(_path, _endpoint, methods=[_method], deprecated=True)


# ==========================================================================
# OpenAPI
# ==========================================================================


def openapi_document() -> dict[str, Any]:
    """The OpenAPI 3 description of every /api route, built from the code.

    Built from this router alone, so the HTML pages and form posts in app.py
    are not in it. `scripts/export_openapi.py` writes it to docs/openapi.json,
    and a test fails if that file and this function ever disagree.
    """
    from fastapi.openapi.utils import get_openapi

    document = get_openapi(
        title="Gym Tracker API",
        version="2.0.0",
        description=(
            "JSON API for the live workout logger. Every route needs a signed-in "
            "account: the browser's session cookie, or HTTP Basic for scripts. "
            "Errors share one shape, ErrorBody."
        ),
        routes=router.routes,
    )
    # FastAPI documents its own validation failures as 422 HTTPValidationError.
    # This API answers them 400 in the shared ErrorBody shape (see
    # install_error_handlers), so the generated entry would be a lie.
    for operations in document["paths"].values():
        for operation in operations.values():
            if operation.get("responses", {}).pop("422", None) is not None:
                operation["responses"].setdefault(
                    "400",
                    {
                        "description": "The input failed validation.",
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/ErrorBody"}
                            }
                        },
                    },
                )
    for unused in ("HTTPValidationError", "ValidationError"):
        document["components"]["schemas"].pop(unused, None)
    document["components"]["securitySchemes"] = {
        "sessionCookie": {"type": "apiKey", "in": "cookie", "name": auth.SESSION_COOKIE},
        "basicAuth": {"type": "http", "scheme": "basic"},
    }
    document["security"] = [{"sessionCookie": []}, {"basicAuth": []}]
    document["servers"] = [{"url": "http://localhost:8000", "description": "Local stack"}]
    return document
