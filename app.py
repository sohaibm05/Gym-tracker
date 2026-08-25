"""FastAPI web app — the phone-facing front end for the gym tracker.

Routes:
    GET  /               mobile-friendly entry form
    POST /log            parse a pasted journal entry into an editable review form
    POST /save           write the reviewed rows to the database
    GET  /progress       charts over this user's logged data
    GET  /weekly-report  report page with a "Generate weekly report" button
    POST /weekly-report  run Stage A + Stage B and upsert the report
    GET/POST /login      sign in, issuing a session cookie
    GET/POST /signup     register a new account
    POST /logout         drop the session
    GET/POST /account    change timezone or password
    GET  /healthz        unauthenticated liveness probe

Every route except /healthz, /login and /signup requires a signed-in user and
reads and writes only that user's rows. Ownership always comes from the session
cookie, never from anything the browser posted.

Saving is deliberately two steps. POST /log only extracts; it renders every
prospective database row for editing, with anything missing or invalid marked in
red. Nothing is written until POST /save, and what it writes is the reviewed
form, not the raw extraction.

All extraction/validation/insert logic is imported from pipeline.py; all report
logic from insights.py; the review form from review.py. Nothing is
reimplemented here.
"""

from __future__ import annotations

import base64
import binascii
import html
import logging
import re
import sys
import time
from datetime import date, datetime
from pathlib import Path
from typing import Optional
from urllib.parse import quote

from fastapi import Depends, FastAPI, Form, HTTPException, Request, status
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
)

# Serverless loaders import this file by path rather than as part of a package,
# and do not necessarily put its own directory on sys.path. Without this the
# sibling imports below raise ModuleNotFoundError at cold start. No effect when
# running normally, where the directory is already there.
sys.path.insert(0, str(Path(__file__).resolve().parent))

# These imports are the ones that break on a misconfigured serverless deploy,
# either because the sibling files were not bundled or because the loader did
# not put this directory on the path. Left unguarded the whole module fails to
# import and the platform reports an opaque "function crashed" with the real
# reason buried in provider logs. Capturing it here lets the running app serve
# the reason instead.
_STARTUP_ERROR: Optional[str] = None
try:
    import auth  # noqa: E402 - must follow the sys.path bootstrap above
    import charts  # noqa: E402
    import insights  # noqa: E402
    import pipeline  # noqa: E402
    import review  # noqa: E402
except Exception:  # noqa: BLE001 - report anything that stops the app booting
    import traceback

    _STARTUP_ERROR = traceback.format_exc()
    auth = charts = insights = pipeline = review = None  # type: ignore[assignment]

logging.basicConfig(
    level=pipeline.env("LOG_LEVEL", "INFO").upper() if pipeline else "INFO",
    format='%(asctime)s level=%(levelname)s logger=%(name)s %(message)s',
)
logger = logging.getLogger("gym_tracker.app")

app = FastAPI(title="Gym Tracker", docs_url=None, redoc_url=None)

_engine = None


def get_engine():
    """Lazily build the SQLAlchemy engine so import never needs a database."""
    global _engine
    if _engine is None:
        _engine = pipeline.get_engine()
    return _engine


# --------------------------------------------------------------------------
# Auth
# --------------------------------------------------------------------------


class LoginRequired(Exception):
    """No usable session. Handled below by redirecting to /login.

    A dependency cannot return a response, and a bare 401 would make the browser
    pop its own Basic Auth box, which is exactly the experience the login page
    replaces. Raising this instead lets the handler send a redirect that
    remembers where the person was going.
    """

    def __init__(self, next_url: str = "/") -> None:
        self.next_url = next_url


@app.exception_handler(LoginRequired)
async def _login_required_handler(request: Request, exc: LoginRequired) -> Response:
    target = "/login"
    if exc.next_url and exc.next_url != "/":
        # safe='/' only: a path is all this ever carries, and leaving '&' or
        # '=' unescaped would let one smuggle extra query parameters in.
        target = f"/login?next={quote(exc.next_url, safe='/')}"
    return RedirectResponse(target, status_code=status.HTTP_303_SEE_OTHER)


def _is_https(request: Request) -> bool:
    """Whether the browser's leg of the connection is TLS.

    Render and Vercel both terminate TLS and forward over HTTP, so the scheme on
    the request object is not the one the user saw; x-forwarded-proto is.
    """
    return request.headers.get("x-forwarded-proto", request.url.scheme) == "https"


def _is_local(request: Request) -> bool:
    return bool(request.client and request.client.host in {"127.0.0.1", "::1"})


def _client_key(request: Request) -> str:
    """Best available identifier for rate-limiting one caller.

    Behind a proxy every request carries the proxy's address, so the first hop
    in x-forwarded-for is used where present. A determined attacker can forge
    that header and get a fresh bucket per request; the point of the limit is to
    stop an unattended guessing loop, not a targeted one, and without it a
    single shared bucket would let one attacker lock out every real user.
    """
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


# Per-instance, so a serverless deploy running several instances allows a
# multiple of these. Still enough to make credential stuffing impractical.
_LOGIN_LIMITER = auth.RateLimiter(limit=10, window_seconds=300) if auth else None
_SIGNUP_LIMITER = auth.RateLimiter(limit=5, window_seconds=3600) if auth else None


def _set_session_cookie(response: Response, token: str, secure: bool) -> None:
    response.set_cookie(
        auth.SESSION_COOKIE,
        token,
        max_age=auth.session_cookie_max_age(),
        httponly=True,
        # Lax is what stands in for a CSRF token here: the browser withholds the
        # cookie on a cross-site POST, so another origin cannot submit these
        # forms on a logged-in user's behalf. Every form on the site is a
        # same-site POST, which Lax still allows.
        samesite="lax",
        secure=secure,
        path="/",
    )


def _clear_session_cookie(response: Response) -> None:
    response.delete_cookie(auth.SESSION_COOKIE, path="/")


def _basic_auth_user(request: Request) -> Optional[auth.User]:
    """Authenticate an `Authorization: Basic` header against the users table.

    Kept so curl and scripts still work against a deployment. Credentials are
    base64, not encrypted, so this is only safe over HTTPS; a request that
    arrives without TLS is logged loudly rather than silently trusted.
    """
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("basic "):
        return None

    if not _is_https(request) and not _is_local(request):
        logger.warning(
            "event=insecure_transport msg='Basic Auth credentials sent without TLS'"
        )
    try:
        decoded = base64.b64decode(header.split(" ", 1)[1], validate=True).decode("utf-8")
        username, _, password = decoded.partition(":")
    except (binascii.Error, UnicodeDecodeError, IndexError):
        return None

    if _LOGIN_LIMITER is not None and not _LOGIN_LIMITER.check(f"basic:{_client_key(request)}"):
        logger.warning("event=auth_rate_limited scheme=basic")
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many attempts. Wait a few minutes.",
        )

    with get_engine().begin() as conn:
        user = auth.authenticate(conn, username, password)

    # Successful calls do not count against the limit. A script polling this
    # endpoint legitimately would otherwise lock itself out inside a minute.
    if user is not None and _LOGIN_LIMITER is not None:
        _LOGIN_LIMITER.reset(f"basic:{_client_key(request)}")
    return user


def current_user(request: Request) -> Optional[auth.User]:
    """The signed-in user, or None. Never raises — for pages that work either way."""
    basic_header = request.headers.get("authorization", "").lower().startswith("basic ")
    if basic_header:
        return _basic_auth_user(request)

    token = request.cookies.get(auth.SESSION_COOKIE)
    if not token:
        return None
    # begin() rather than connect(): resolving a session can extend it, or drop
    # it if the account was suspended since the last request.
    with get_engine().begin() as conn:
        return auth.resolve_session(conn, token)


def require_user(request: Request) -> auth.User:
    """The signed-in user, or a redirect to the login page.

    Every data route depends on this, and every query underneath it is filtered
    by the id it returns. That is the whole of the isolation guarantee: there is
    no route that takes a user id from the request.
    """
    user = current_user(request)
    if user is not None:
        return user

    if request.headers.get("authorization", "").lower().startswith("basic "):
        logger.warning("event=auth_failed scheme=basic path=%s", request.url.path)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Basic"},
        )

    logger.info("event=login_required path=%s", request.url.path)
    raise LoginRequired(request.url.path)


# --------------------------------------------------------------------------
# Structured request logging
# --------------------------------------------------------------------------


@app.middleware("http")
async def log_requests(request: Request, call_next):
    if _STARTUP_ERROR is not None:
        # Every route depends on the modules that failed to import, so serve the
        # reason rather than a stack-less 500 from the platform.
        logger.error("event=startup_failed path=%s", request.url.path)
        return PlainTextResponse(
            "The application failed to start.\n\n"
            "This is an import error, not a configuration one - no secrets are "
            "shown below.\n\n" + _STARTUP_ERROR,
            status_code=500,
        )

    started = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        duration_ms = (time.perf_counter() - started) * 1000
        logger.exception(
            "event=request method=%s path=%s status=500 duration_ms=%.1f",
            request.method,
            request.url.path,
            duration_ms,
        )
        raise
    duration_ms = (time.perf_counter() - started) * 1000
    logger.info(
        "event=request method=%s path=%s status=%d duration_ms=%.1f",
        request.method,
        request.url.path,
        response.status_code,
        duration_ms,
    )
    return response


# --------------------------------------------------------------------------
# Rendering helpers (everything dynamic is escaped before it reaches the page)
# --------------------------------------------------------------------------

_STYLE = """
:root { color-scheme: light dark; }
* { box-sizing: border-box; }
body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
       margin: 0; padding: 1rem; max-width: 46rem; margin-inline: auto; line-height: 1.5; }
h1 { font-size: 1.35rem; margin: 0 0 1rem; }
h2 { font-size: 1.1rem; margin: 1.4rem 0 .5rem; }
label { display: block; font-weight: 600; margin: .9rem 0 .3rem; }
input[type=date], textarea, button, select { width: 100%; font-size: 1rem; padding: .7rem;
       border-radius: .5rem; border: 1px solid #8884; font-family: inherit; }
textarea { min-height: 11rem; resize: vertical; }
button { margin-top: 1rem; font-weight: 600; border: 0; background: #2563eb; color: #fff; }
button:active { background: #1d4ed8; }
.card { border: 1px solid #8884; border-radius: .6rem; padding: .8rem 1rem; margin: .8rem 0; }
.ok { border-left: 4px solid #16a34a; }
.warn { border-left: 4px solid #d97706; }
.err { border-left: 4px solid #dc2626; }
.muted { opacity: .75; font-size: .9rem; }
ul { padding-left: 1.2rem; }
nav { display: flex; flex-wrap: wrap; align-items: baseline; gap: 1rem;
      margin-bottom: 1rem; }
nav a { display: inline-block; }
nav .who { margin-left: auto; font-size: .9rem; opacity: .8; }
nav form { display: inline; }
nav button.link { width: auto; margin: 0; padding: 0; background: none; color: #2563eb;
      font: inherit; font-weight: 400; text-decoration: underline; cursor: pointer; }
pre { white-space: pre-wrap; word-wrap: break-word; }
input[type=text], input[type=password] { width: 100%; font-size: 1rem; padding: .7rem;
      border-radius: .5rem; border: 1px solid #8884; font-family: inherit; }
.auth { max-width: 24rem; margin-inline: auto; }
.auth .muted a { white-space: nowrap; }
"""


def _nav(user: Optional["auth.User"]) -> str:
    """Site navigation. Signed out, the only things on it are the two doors in."""
    if user is None:
        return (
            '<nav><a href="/login">Log in</a><a href="/signup">Create account</a></nav>'
        )
    return (
        '<nav>'
        '<a href="/">Log entry</a>'
        '<a href="/progress">Progress</a>'
        '<a href="/weekly-report">Weekly report</a>'
        # A div, not a span: <form> is flow content and a span may only hold
        # phrasing content, which browsers "fix" by relocating the form.
        f'<div class="who">{html.escape(user.label)} &middot; '
        '<a href="/account">Account</a> &middot; '
        '<form method="post" action="/logout">'
        '<button type="submit" class="link">Log out</button></form></div>'
        '</nav>'
    )


def _page(
    title: str,
    body: str,
    extra_css: str = "",
    extra_js: str = "",
    user: Optional["auth.User"] = None,
) -> HTMLResponse:
    css = f"<style>{_STYLE}{extra_css}</style>"
    # Deferred so the markup the charts measure exists before the script runs.
    js = f"<script>{extra_js}</script>" if extra_js else ""
    return HTMLResponse(
        f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
{css}
</head><body>
{_nav(user)}
{body}
{js}
</body></html>"""
    )


def _render_markdown(markdown_text: str) -> str:
    """Minimal markdown -> HTML. Escapes FIRST, so no user text can inject tags."""
    escaped = html.escape(markdown_text)
    escaped = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", escaped)
    lines_out: list[str] = []
    in_list = False
    for line in escaped.splitlines():
        stripped = line.strip()
        if stripped.startswith("## "):
            if in_list:
                lines_out.append("</ul>")
                in_list = False
            lines_out.append(f"<h2>{stripped[3:]}</h2>")
        elif stripped.startswith("# "):
            if in_list:
                lines_out.append("</ul>")
                in_list = False
            lines_out.append(f"<h2>{stripped[2:]}</h2>")
        elif stripped.startswith("- "):
            if not in_list:
                lines_out.append("<ul>")
                in_list = True
            lines_out.append(f"<li>{stripped[2:]}</li>")
        elif not stripped:
            if in_list:
                lines_out.append("</ul>")
                in_list = False
        else:
            if in_list:
                lines_out.append("</ul>")
                in_list = False
            lines_out.append(f"<p>{stripped}</p>")
    if in_list:
        lines_out.append("</ul>")
    return "\n".join(lines_out)


def _render_result(result: pipeline.PipelineResult, session_date: date) -> str:
    parts: list[str] = []

    if result.error:
        parts.append(f'<div class="card err"><strong>Error.</strong> {html.escape(result.error)}</div>')

    if result.duplicate_of_recent:
        parts.append(
            '<div class="card warn"><strong>Duplicate submission.</strong> '
            "This exact text was already processed within the last "
            f"{pipeline.DUPLICATE_WINDOW_MINUTES} minutes, so nothing was re-inserted. "
            f"The earlier run saved {result.inserted_sets} set(s) and "
            f"{result.inserted_bodyweight} bodyweight entry/entries.</div>"
        )
    else:
        parts.append(
            f'<div class="card ok"><strong>Inserted {result.inserted_sets} set(s)</strong> and '
            f"{result.inserted_bodyweight} bodyweight entry/entries for "
            f"{html.escape(session_date.isoformat())}.</div>"
        )

    if result.replaced:
        removed = result.replaced
        parts.append(
            '<div class="card warn"><strong>Replaced that day.</strong> Removed '
            f"{removed['sets']} set(s) and {removed['bodyweight']} bodyweight "
            "entry/entries logged earlier on this date before saving this one.</div>"
        )

    if result.exercises_created:
        names = ", ".join(html.escape(name) for name in result.exercises_created)
        parts.append(f'<div class="card muted">New exercises created: {names}</div>')

    if result.name_flags:
        rows = "".join(
            f"<li><strong>{html.escape(flag.exercise_name)}</strong> "
            f"&mdash; {html.escape(flag.detail)}</li>"
            for flag in result.name_flags
        )
        parts.append(
            '<div class="card warn"><strong>Check these names '
            f"({len(result.name_flags)}) &mdash; saved anyway</strong><ul>{rows}</ul>"
            "<p class=\"muted\">Flagged only when the name creates a new exercise. "
            "Fix a wrong one by re-submitting the entry with <strong>Replace</strong>.</p></div>"
        )

    if result.exercises_matched:
        rows = "".join(
            f"<li>{html.escape(proposed)} &rarr; matched existing <strong>{html.escape(matched)}</strong></li>"
            for proposed, matched in result.exercises_matched
        )
        parts.append(f'<div class="card muted"><strong>Fuzzy-matched names</strong><ul>{rows}</ul></div>')

    skipped = result.skipped_sets + result.skipped_bodyweight
    if skipped:
        parts.append(
            f'<div class="card muted">You unticked {skipped} row(s) on the review '
            "screen, so they were not saved.</div>"
        )

    if result.review_items:
        rows = []
        for item in result.review_items:
            confidence = f"{item.confidence:.2f}" if item.confidence is not None else "n/a"
            label = html.escape(str(item.payload.get("exercise_name") or item.kind))
            weight = item.payload.get("weight_kg")
            reps = item.payload.get("reps")
            detail = ""
            if weight is not None or reps is not None:
                detail = f" ({html.escape(str(weight))}kg &times; {html.escape(str(reps))})"
            rows.append(
                f"<li><strong>{label}</strong>{detail} &mdash; confidence {confidence}. "
                f"{html.escape(item.reason)}</li>"
            )
        parts.append(
            '<div class="card err"><strong>Could not be saved '
            f"({len(result.review_items)})</strong> &mdash; these parts of the entry "
            f"could not be turned into editable rows.<ul>{''.join(rows)}</ul></div>"
        )
    elif not result.error and not result.duplicate_of_recent and not skipped:
        parts.append('<div class="card muted">Everything you ticked was saved.</div>')

    return "".join(parts)


# --------------------------------------------------------------------------
# Accounts
# --------------------------------------------------------------------------


def _safe_next(raw: Optional[str]) -> str:
    """Where to send someone after login, if it is somewhere on this site.

    Only a bare absolute path is accepted. "//evil.example" and
    "https://evil.example" are both rejected: without this check the ?next=
    parameter would be an open redirect, and a login page that can bounce you to
    an attacker's copy of itself is worth more to them than no login page.
    """
    candidate = (raw or "").strip()
    if not candidate.startswith("/") or candidate.startswith("//"):
        return "/"
    return candidate


def _auth_form(
    title: str,
    action: str,
    submit_label: str,
    footer: str,
    error: str = "",
    username: str = "",
    next_url: str = "",
    extra_fields: str = "",
) -> str:
    """The login and signup forms differ by three strings, so they share a body."""
    banner = f'<div class="card err">{html.escape(error)}</div>' if error else ""
    hidden = (
        f'<input type="hidden" name="next" value="{html.escape(next_url)}">'
        if next_url else ""
    )
    return f"""
<div class="auth">
<h1>{html.escape(title)}</h1>
{banner}
<form method="post" action="{action}">
  {hidden}
  <label for="username">Username</label>
  <input type="text" id="username" name="username" value="{html.escape(username)}"
         autocomplete="username" autocapitalize="none" autocorrect="off" required>
  <label for="password">Password</label>
  <input type="password" id="password" name="password"
         autocomplete="current-password" required>
  {extra_fields}
  <button type="submit">{html.escape(submit_label)}</button>
</form>
<p class="muted">{footer}</p>
</div>
"""


@app.get("/login", response_class=HTMLResponse)
async def login_form(request: Request, next: str = "") -> Response:
    if current_user(request) is not None:
        return RedirectResponse(_safe_next(next), status_code=status.HTTP_303_SEE_OTHER)
    return _page(
        "Log in",
        _auth_form(
            "Log in", "/login", "Log in",
            'No account yet? <a href="/signup">Create one</a>.',
            next_url=_safe_next(next) if next else "",
        ),
    )


@app.post("/login", response_class=HTMLResponse)
async def login_submit(
    request: Request,
    username: str = Form(""),
    password: str = Form(""),
    next: str = Form(""),
) -> Response:
    target = _safe_next(next)

    if _LOGIN_LIMITER is not None and not _LOGIN_LIMITER.check(f"login:{_client_key(request)}"):
        logger.warning("event=auth_rate_limited scheme=form")
        return _page(
            "Log in",
            _auth_form(
                "Log in", "/login", "Log in",
                'No account yet? <a href="/signup">Create one</a>.',
                error="Too many attempts from here. Wait a few minutes and try again.",
                username=username, next_url=target,
            ),
        )

    with get_engine().begin() as conn:
        user = auth.authenticate(conn, username, password)
        # Issued inside the same transaction as the password check, so a session
        # cookie can never outlive a login that did not commit.
        token = auth.create_session(conn, user.user_id) if user else None

    if user is None:
        logger.warning("event=auth_failed scheme=form")
        return _page(
            "Log in",
            _auth_form(
                "Log in", "/login", "Log in",
                'No account yet? <a href="/signup">Create one</a>.',
                # Deliberately does not say which half was wrong: that would
                # turn the form into a test for which usernames exist.
                error="That username and password do not match an account.",
                username=username, next_url=target,
            ),
        )

    logger.info("event=login user_id=%d", user.user_id)
    # A successful login clears the bucket, so someone who mistypes twice and
    # then gets it right is not left one attempt from being locked out.
    if _LOGIN_LIMITER is not None:
        _LOGIN_LIMITER.reset(f"login:{_client_key(request)}")
    response = RedirectResponse(target, status_code=status.HTTP_303_SEE_OTHER)
    _set_session_cookie(response, token, secure=_is_https(request))
    return response


@app.get("/signup", response_class=HTMLResponse)
async def signup_form(request: Request) -> Response:
    if current_user(request) is not None:
        return RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)
    return _page("Create account", _signup_body())


def _signup_body(error: str = "", username: str = "", timezone_name: str = "") -> str:
    zone_field = f"""
  <label for="timezone">Timezone <span class="muted">(optional)</span></label>
  <input type="text" id="timezone" name="timezone" value="{html.escape(timezone_name)}"
         placeholder="{html.escape(pipeline.LOCAL_TIMEZONE)}"
         autocapitalize="none" autocorrect="off">
"""
    return _auth_form(
        "Create account", "/signup", "Create account",
        'Already have one? <a href="/login">Log in</a>. Your training is yours '
        'alone &mdash; nobody else using this app can see it.',
        error=error, username=username, extra_fields=zone_field,
    ).replace('autocomplete="current-password"', 'autocomplete="new-password"')


@app.post("/signup", response_class=HTMLResponse)
async def signup_submit(
    request: Request,
    username: str = Form(""),
    password: str = Form(""),
    timezone: str = Form(""),
) -> Response:
    def failed(message: str) -> HTMLResponse:
        return _page("Create account", _signup_body(message, username, timezone))

    if _SIGNUP_LIMITER is not None and not _SIGNUP_LIMITER.check(f"signup:{_client_key(request)}"):
        logger.warning("event=signup_rate_limited")
        return failed("Too many accounts created from here. Try again later.")

    try:
        with get_engine().begin() as conn:
            user = auth.create_user(conn, username, password, timezone)
            token = auth.create_session(conn, user.user_id)
    except auth.AuthError as exc:
        logger.info("event=signup_rejected reason=%s", exc)
        return failed(str(exc))

    logger.info("event=signup user_id=%d", user.user_id)
    response = RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)
    _set_session_cookie(response, token, secure=_is_https(request))
    return response


@app.post("/logout")
async def logout(request: Request) -> Response:
    token = request.cookies.get(auth.SESSION_COOKIE)
    if token:
        with get_engine().begin() as conn:
            auth.delete_session(conn, token)
    response = RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
    _clear_session_cookie(response)
    return response


def _account_body(user: "auth.User", message: str = "", error: str = "") -> str:
    banner = ""
    if error:
        banner = f'<div class="card err">{html.escape(error)}</div>'
    elif message:
        banner = f'<div class="card ok">{html.escape(message)}</div>'
    zone = user.timezone or ""
    return f"""
<h1>Account</h1>
{banner}
<p class="muted">Signed in as <strong>{html.escape(user.label)}</strong>.</p>

<h2>Timezone</h2>
<form method="post" action="/account">
  <input type="hidden" name="action" value="timezone">
  <label for="timezone">IANA timezone name</label>
  <input type="text" id="timezone" name="timezone" value="{html.escape(zone)}"
         placeholder="{html.escape(pipeline.LOCAL_TIMEZONE)}"
         autocapitalize="none" autocorrect="off">
  <button type="submit">Save timezone</button>
</form>
<p class="muted">This decides which calendar day &mdash; and so which week &mdash; a
session belongs to. Leave it blank to use the app default
(<code>{html.escape(pipeline.LOCAL_TIMEZONE)}</code>). Changing it does not move
anything already logged.</p>

<h2>Password</h2>
<form method="post" action="/account">
  <input type="hidden" name="action" value="password">
  <label for="current_password">Current password</label>
  <input type="password" id="current_password" name="current_password"
         autocomplete="current-password" required>
  <label for="new_password">New password</label>
  <input type="password" id="new_password" name="new_password"
         autocomplete="new-password" required>
  <button type="submit">Change password</button>
</form>
<p class="muted">Changing your password signs you out everywhere, including here.</p>
"""


@app.get("/account", response_class=HTMLResponse)
async def account_page(user: "auth.User" = Depends(require_user)) -> HTMLResponse:
    return _page("Account", _account_body(user), user=user)


@app.post("/account", response_class=HTMLResponse)
async def account_update(
    request: Request,
    action: str = Form(""),
    timezone: str = Form(""),
    current_password: str = Form(""),
    new_password: str = Form(""),
    user: "auth.User" = Depends(require_user),
) -> Response:
    if action == "timezone":
        try:
            with get_engine().begin() as conn:
                zone = auth.set_timezone(conn, user.user_id, timezone)
        except auth.AuthError as exc:
            return _page("Account", _account_body(user, error=str(exc)), user=user)
        logger.info("event=timezone_changed user_id=%d timezone=%s", user.user_id, zone)
        refreshed = auth.User(user.user_id, user.username, user.display_name, zone,
                              user.is_active)
        return _page(
            "Account",
            _account_body(refreshed, message=f"Timezone set to {zone or pipeline.LOCAL_TIMEZONE}."),
            user=refreshed,
        )

    if action == "password":
        with get_engine().begin() as conn:
            # Re-checked here rather than trusted from the session: a session
            # cookie proves who was at this browser at login, not who is at it
            # now, and a password change is what an unattended phone is worth.
            confirmed = auth.authenticate(conn, user.username, current_password)
            if confirmed is None:
                return _page(
                    "Account",
                    _account_body(user, error="Current password is not correct."),
                    user=user,
                )
            try:
                auth.set_password(conn, user.user_id, new_password)
            except auth.AuthError as exc:
                return _page("Account", _account_body(user, error=str(exc)), user=user)

        logger.info("event=password_changed user_id=%d", user.user_id)
        # set_password revoked every session including this one, so the cookie
        # in the browser is already dead; clear it and ask for the new password.
        response = RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
        _clear_session_cookie(response)
        return response

    return _page("Account", _account_body(user, error="Unknown action."), user=user)


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------


@app.get("/healthz")
async def healthz() -> JSONResponse:
    return JSONResponse({"status": "ok"})


@app.get("/", response_class=HTMLResponse)
async def index(user: "auth.User" = Depends(require_user)) -> HTMLResponse:
    today = pipeline.local_today(user.timezone).isoformat()
    return _page(
        "Log a workout",
        f"""
<h1>Log a workout</h1>
<form method="post" action="/log">
  <label for="session_date">Session date</label>
  <input type="date" id="session_date" name="session_date" value="{today}" required>
  <label for="raw_text">Journal entry</label>
  <textarea id="raw_text" name="raw_text" required
    placeholder="Paste straight from Notes. Messy is fine."></textarea>
  <label for="mode">If that day already has entries</label>
  <select id="mode" name="mode">
    <option value="add" selected>Add to them &mdash; a second session, or more sets</option>
    <option value="replace">Replace them &mdash; discard that day and use this instead</option>
  </select>
  <button type="submit">Parse &amp; review</button>
</form>
<p class="muted">Parsing shows you every row it is about to write, with anything
missing or invalid marked in red. Nothing is saved until you have looked at it
and pressed save. Bodyweight mentions in the same entry are picked up
automatically. <strong>Replace</strong> deletes everything already logged on that
date, so use it to correct a bad entry &mdash; not to add an evening session.</p>
""",
        user=user,
    )


def _parse_session_date(value: str) -> Optional[date]:
    try:
        return datetime.strptime((value or "").strip(), "%Y-%m-%d").date()
    except ValueError:
        return None


def _invalid_date_page(user: Optional["auth.User"] = None) -> HTMLResponse:
    return _page(
        "Invalid date",
        '<h1>Result</h1><div class="card err">Session date must be YYYY-MM-DD.</div>'
        '<p><a href="/">Back</a></p>',
        user=user,
    )


def _known_groups(user: "auth.User") -> Optional[dict[str, Optional[str]]]:
    """What this user already files each of their exercises under, for the suggestion.

    Best effort, like `_existing_counts`: without it the suggestion falls back
    to the static table, which is a worse answer but not a wrong one.
    """
    try:
        with get_engine().connect() as conn:
            return pipeline.load_exercise_groups(conn, user.user_id)
    except Exception:  # noqa: BLE001 - the save step will surface a real outage
        logger.warning("event=known_groups_failed user_id=%d", user.user_id, exc_info=True)
        return None


def _existing_counts(session_date: date, user: "auth.User") -> Optional[dict[str, int]]:
    """What this user has already logged on that date, for the review banner.

    Best effort: this only decorates the page, so a database that is briefly
    unreachable should not cost the user the extraction they just paid for.
    """
    try:
        return pipeline.count_entries_for_date(
            get_engine(), session_date, user.user_id, user.timezone
        )
    except Exception:  # noqa: BLE001 - the save step will surface a real outage
        logger.warning("event=existing_counts_failed date=%s user_id=%d",
                       session_date, user.user_id, exc_info=True)
        return None


def _review_page(draft: pipeline.EntryDraft, user: "auth.User") -> HTMLResponse:
    return _page(
        "Check before saving",
        review.render_review_body(
            draft, _existing_counts(draft.session_date, user), pipeline.CONFIDENCE_THRESHOLD
        ),
        extra_css=review.REVIEW_CSS,
        user=user,
    )


@app.post("/log", response_class=HTMLResponse)
async def log_entry(
    session_date: str = Form(...),
    raw_text: str = Form(...),
    mode: str = Form("add"),
    user: "auth.User" = Depends(require_user),
) -> HTMLResponse:
    """Extract only. Nothing reaches the database until POST /save."""
    parsed_date = _parse_session_date(session_date)
    if parsed_date is None:
        return _invalid_date_page(user)

    draft = pipeline.build_draft(raw_text, parsed_date, known_groups=_known_groups(user))
    draft.replace_existing = mode == "replace"
    logger.info(
        "event=entry_drafted user_id=%d date=%s mode=%s sets=%d bodyweight=%s "
        "flagged=%d blocked=%d error=%s",
        user.user_id,
        parsed_date,
        mode,
        len(draft.sets),
        draft.bodyweight is not None,
        draft.flagged_count,
        draft.blocked_count,
        bool(draft.error),
    )

    if draft.error:
        notes = "".join(
            f'<div class="card muted">{html.escape(item.reason)}</div>'
            for item in draft.review_items
        )
        return _page(
            "Could not parse",
            f'<h1>Could not parse that entry</h1>'
            f'<div class="card err">{html.escape(draft.error)}</div>{notes}'
            '<p class="muted">Nothing was saved. Go back and try again — the entry '
            "is unchanged.</p>"
            '<p><a href="/">Back</a></p>',
            user=user,
        )

    return _review_page(draft, user)


@app.post("/save", response_class=HTMLResponse)
async def save_reviewed(
    request: Request, user: "auth.User" = Depends(require_user)
) -> HTMLResponse:
    """Write the reviewed form, or hand it back with one more empty slot.

    The rows are rescored from the submitted values rather than from the
    extraction, so a corrected field is judged on what the user actually typed.
    A row the database would still reject sends the whole form back for another
    pass instead of being quietly dropped.
    """
    form = await request.form()
    try:
        draft = review.draft_from_form(form, known_groups=_known_groups(user))
    except ValueError:
        return _invalid_date_page(user)

    # "Add a set" posts the same form; it hands back the draft plus an empty slot
    # rather than saving, so a half-corrected row is not judged in passing.
    if review.add_row(draft, str(form.get("action", "save"))):
        return _review_page(draft, user)

    if not draft.ready:
        logger.info("event=save_blocked user_id=%d date=%s blocked=%d",
                    user.user_id, draft.session_date, draft.blocked_count)
        return _review_page(draft, user)

    # The owning user comes from the session, not from the form that was just
    # posted back — the draft's hidden fields are whatever the browser sent.
    result = pipeline.commit_draft(
        draft,
        user.user_id,
        engine=get_engine(),
        check_duplicates=True,
        timezone_name=user.timezone,
    )
    logger.info(
        "event=entry_saved user_id=%d date=%s replace=%s inserted_sets=%d "
        "inserted_bodyweight=%d skipped=%d review=%d duplicate=%s replaced=%s",
        user.user_id,
        draft.session_date,
        draft.replace_existing,
        result.inserted_sets,
        result.inserted_bodyweight,
        result.skipped_sets + result.skipped_bodyweight,
        len(result.review_items),
        result.duplicate_of_recent,
        result.replaced,
    )
    return _page(
        "Saved",
        f'<h1>Saved</h1>{_render_result(result, draft.session_date)}'
        '<p><a href="/">Log another</a></p>',
        user=user,
    )


@app.get("/progress", response_class=HTMLResponse)
async def progress(user: "auth.User" = Depends(require_user)) -> HTMLResponse:
    """Charts over this user's logged data. Every figure is computed in code, as
    in the weekly report - this page plots the same Stage A numbers."""
    weeks = insights.ANALYSIS_WEEKS if insights.ANALYSIS_WEEKS >= 8 else 12
    data = insights.build_dashboard(
        get_engine(), user.user_id, weeks=weeks, timezone_name=user.timezone
    )
    logger.info("event=progress_rendered user_id=%d weeks=%d has_data=%s",
                user.user_id, weeks, data["has_data"])
    return _page(
        "Progress",
        charts.render_progress_body(data),
        extra_css=charts.PROGRESS_CSS,
        extra_js=charts.PROGRESS_JS,
        user=user,
    )


@app.get("/weekly-report", response_class=HTMLResponse)
async def weekly_report_form(user: "auth.User" = Depends(require_user)) -> HTMLResponse:
    current_week = insights.week_start_for(pipeline.local_today(user.timezone))
    return _page(
        "Weekly report",
        f"""
<h1>Weekly report</h1>
<p class="muted">All figures are computed in code. The language model only writes
the prose around them.</p>
<form method="post" action="/weekly-report">
  <label for="week_start">Week starting (Monday)</label>
  <input type="date" id="week_start" name="week_start" value="{current_week.isoformat()}">
  <button type="submit">Generate weekly report</button>
</form>
<p class="muted">Regenerating a week overwrites that week's stored report.
Pain safeguard is currently <strong>{'on' if insights.PAIN_SAFEGUARD_ENABLED else 'OFF'}</strong>.</p>
""",
        user=user,
    )


@app.post("/weekly-report", response_class=HTMLResponse)
async def weekly_report_generate(
    week_start: Optional[str] = Form(None),
    user: "auth.User" = Depends(require_user),
) -> HTMLResponse:
    parsed_week: Optional[date] = None
    if week_start and week_start.strip():
        try:
            parsed_week = datetime.strptime(week_start.strip(), "%Y-%m-%d").date()
        except ValueError:
            return _page(
                "Invalid date",
                '<h1>Weekly report</h1><div class="card err">Week start must be YYYY-MM-DD.</div>'
                '<p><a href="/weekly-report">Back</a></p>',
                user=user,
            )

    report = insights.generate_weekly_report(
        get_engine(), user.user_id, week_start=parsed_week, timezone_name=user.timezone
    )
    logger.info(
        "event=report_generated user_id=%d week_start=%s records=%d narration_error=%s",
        user.user_id,
        report["week_start_date"],
        report["record_count"],
        bool(report["narration_error"]),
    )

    banner = ""
    if report["record_count"] == 0:
        banner = (
            '<div class="card warn">No sets logged in the analysis window, so the '
            "report has nothing to work from.</div>"
        )
    elif report["narration_error"]:
        banner = (
            '<div class="card warn">The narration step failed, so this is the '
            "deterministic summary. Every number is unaffected &mdash; they are all "
            "computed in code.</div>"
        )

    program_note = report["recommendations"]["program_note"]
    if program_note["stagnation_flagged"]:
        banner += (
            '<div class="card warn"><strong>Program-level stagnation flagged.</strong> '
            f"{html.escape(program_note['detail']['reason'])}</div>"
        )

    return _page(
        "Weekly report",
        f"""<h1>Week of {html.escape(str(report['week_start_date']))}</h1>
{banner}
<div class="card">{_render_markdown(report['summary_text'])}</div>
<p class="muted">Saved to <code>weekly_reports</code> ({report['record_count']} sets analysed).</p>
<p><a href="/weekly-report">Generate another</a></p>""",
        user=user,
    )
