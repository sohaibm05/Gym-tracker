"""Structured JSON logging, shaped for Filebeat -> Elasticsearch -> Kibana.

Why JSON
--------
The app used to log `event=request method=POST path=/save status=200`. That is
readable in a terminal and miserable downstream: every consumer has to re-parse
it with a grok pattern, and one unescaped space inside a value breaks the
pattern silently. Emitting JSON moves the parsing to the producer, which is the
only place that still knows the types. Filebeat then does nothing but read the
line and hand Elasticsearch a document whose fields are already correct.

Field names
-----------
Elastic Common Schema (ECS) names, so Kibana's built-in columns and the
`http.*`/`url.*`/`event.*` field groups work without custom mappings:

    @timestamp                 ISO-8601, UTC, milliseconds
    log.level                  debug|info|warning|error|critical
    message                    a short human sentence, not a key=value blob
    service.name               gym-tracker
    service.version            build identity
    event.dataset              which logger produced it
    http.request.id            the correlation id — one per HTTP request
    http.request.method        GET/POST
    url.path                   route template where known, raw path otherwise
    http.response.status_code  integer
    event.duration_ms          float milliseconds
    error.type / error.message / error.stack_trace  on exceptions

Dotted keys are what ECS specifies and what Elasticsearch expands back into
objects on ingest, so `http.request.id` is searchable as `http.request.id` in
Kibana without any Filebeat-side rewriting.

Correlation
-----------
`request_id_var` is a contextvar set once per request by the middleware. Every
log line emitted while handling that request — from any module, at any depth —
picks it up automatically, so one Kibana search on `http.request.id` returns the
whole story of a single click. A contextvar rather than a thread-local because
the app is async: several requests share a thread, and a thread-local would
cross their ids over.

What never gets logged
----------------------
This app handles a person's training journal, which is health data, plus
passwords and API keys. `SensitiveDataFilter` is a backstop, not the policy —
the policy is that call sites log ids and counts, never content. The filter
scrubs values under known-sensitive keys and redacts anything shaped like a Groq
key or an Authorization header if one ever reaches a log line by accident.
Journal text (`raw_text`) is never logged at any level: its length and the
number of rows extracted from it are what the pipeline records instead.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import uuid
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any, Optional

# --------------------------------------------------------------------------
# Request correlation
# --------------------------------------------------------------------------

request_id_var: ContextVar[str] = ContextVar("request_id", default="")

SERVICE_NAME = os.getenv("SERVICE_NAME", "gym-tracker")


def new_request_id() -> str:
    """A short, unique, non-guessable id for one request.

    16 hex characters out of a uuid4. Long enough that a collision is not a
    practical concern at this volume, short enough to read out of a Kibana
    column or paste into a support conversation.
    """
    return uuid.uuid4().hex[:16]


def set_request_id(value: Optional[str] = None) -> str:
    """Bind a request id to this context and return it."""
    token = value or new_request_id()
    request_id_var.set(token)
    return token


def get_request_id() -> str:
    return request_id_var.get()


# --------------------------------------------------------------------------
# Redaction
# --------------------------------------------------------------------------

REDACTED = "[redacted]"

# Keys whose value is never safe to store, matched case-insensitively against
# the whole key or its last dotted segment.
SENSITIVE_KEYS = frozenset(
    {
        "password",
        "new_password",
        "current_password",
        "password_hash",
        "token",
        "session_token",
        "session",
        "cookie",
        "set-cookie",
        "authorization",
        "api_key",
        "apikey",
        "groq_api_key",
        "secret",
        "database_url",
        # The journal entry itself. Health data: logged as a length, never as
        # content. See pipeline call sites.
        "raw_text",
        "raw_source",
    }
)

# Value-shaped redaction, for the accidental path where a secret arrives inside
# a message string rather than under a named key.
_VALUE_PATTERNS = (
    # Groq keys.
    re.compile(r"\bgsk_[A-Za-z0-9]{8,}\b"),
    # Bearer/Basic credentials in a header dump.
    re.compile(r"\b(?:Bearer|Basic)\s+[A-Za-z0-9._~+/=-]{8,}", re.IGNORECASE),
    # Anything carrying a password in a URL's userinfo, e.g. a DSN.
    re.compile(r"(?<=://)[^\s:@/]+:[^\s@/]+(?=@)"),
)


def scrub_text(value: str) -> str:
    """Redact secret-shaped substrings from a free-text value."""
    for pattern in _VALUE_PATTERNS:
        value = pattern.sub(REDACTED, value)
    return value


def scrub_value(key: str, value: Any) -> Any:
    """Redact by key name, recursing into dicts and lists."""
    leaf = key.rsplit(".", 1)[-1].lower()
    if leaf in SENSITIVE_KEYS or key.lower() in SENSITIVE_KEYS:
        return REDACTED
    if isinstance(value, dict):
        return {k: scrub_value(k, v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [scrub_value(key, item) for item in value]
    if isinstance(value, str):
        return scrub_text(value)
    return value


class SensitiveDataFilter(logging.Filter):
    """Last line of defence before a record is formatted.

    Installed on the handler rather than a logger so it applies to library log
    output too — a dependency that helpfully logs the request it just made with
    the Authorization header attached is exactly the case this catches.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = scrub_text(record.msg)
        if record.args:
            if isinstance(record.args, dict):
                record.args = {k: scrub_value(k, v) for k, v in record.args.items()}
            else:
                record.args = tuple(scrub_text(a) if isinstance(a, str) else a for a in record.args)
        return True


# --------------------------------------------------------------------------
# Formatter
# --------------------------------------------------------------------------

# LogRecord attributes that are machinery, not payload. Anything on a record
# that is NOT in here was put there by a call site via `extra=` and is promoted
# into the JSON document.
_RESERVED = frozenset(
    {
        "args", "asctime", "created", "exc_info", "exc_text", "filename",
        "funcName", "levelname", "levelno", "lineno", "module", "msecs",
        "message", "msg", "name", "pathname", "process", "processName",
        "relativeCreated", "stack_info", "thread", "threadName", "taskName",
    }
)


class EcsJsonFormatter(logging.Formatter):
    """One JSON object per line, in ECS field names.

    One line per event matters: Filebeat reads newline-delimited records, and a
    pretty-printed object would arrive as N unparseable lines. `json.dumps` with
    the default separators never emits a newline inside the object, and
    `ensure_ascii=False` keeps non-ASCII characters readable in Kibana rather
    than escaped.
    """

    def __init__(self, service: str = SERVICE_NAME, version: str = "") -> None:
        super().__init__()
        self.service = service
        self.version = version or os.getenv("APP_VERSION", "dev")

    def format(self, record: logging.LogRecord) -> str:
        document: dict[str, Any] = {
            "@timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"),
            "log.level": record.levelname.lower(),
            "message": record.getMessage(),
            "service.name": self.service,
            "service.version": self.version,
            "event.dataset": record.name,
            "log.logger": record.name,
            "log.origin.file.name": record.filename,
            "log.origin.file.line": record.lineno,
            "log.origin.function": record.funcName,
            "process.pid": record.process,
        }

        request_id = request_id_var.get()
        if request_id:
            document["http.request.id"] = request_id

        # Everything a call site attached with extra={...}.
        for key, value in record.__dict__.items():
            if key in _RESERVED or key.startswith("_"):
                continue
            document[key] = scrub_value(key, value)

        if record.exc_info:
            exc_type, exc_value, _ = record.exc_info
            document["error.type"] = getattr(exc_type, "__name__", str(exc_type))
            # The exception's own text can carry a connection string or a key
            # echoed back by a client library, so it goes through the scrubber.
            document["error.message"] = scrub_text(str(exc_value))
            document["error.stack_trace"] = scrub_text(self.formatException(record.exc_info))

        if record.stack_info:
            document["error.stack_trace"] = scrub_text(self.formatStack(record.stack_info))

        # default=str so a date, a Decimal or a model object degrades to its
        # string form instead of killing the log call it appeared in. A logging
        # failure must never be able to take a request down with it.
        return json.dumps(document, ensure_ascii=False, default=str)


class TextFormatter(logging.Formatter):
    """Human-readable fallback for `LOG_FORMAT=text` during local development.

    Carries the same request id so a terminal session and a Kibana search can be
    lined up against each other.
    """

    def format(self, record: logging.LogRecord) -> str:
        request_id = request_id_var.get()
        prefix = f"[{request_id}] " if request_id else ""
        extras = " ".join(
            f"{k}={scrub_value(k, v)}"
            for k, v in record.__dict__.items()
            if k not in _RESERVED and not k.startswith("_")
        )
        base = (
            f"{self.formatTime(record)} {record.levelname:<8} {record.name} "
            f"{prefix}{record.getMessage()}"
        )
        line = f"{base} {extras}".rstrip()
        if record.exc_info:
            line = f"{line}\n{self.formatException(record.exc_info)}"
        return line


# --------------------------------------------------------------------------
# Installation
# --------------------------------------------------------------------------


def configure_logging(
    level: str = "",
    log_format: str = "",
    stream: Any = None,
    service: str = SERVICE_NAME,
) -> logging.Logger:
    """Install the root handler. Idempotent — safe to call more than once.

    Logs go to stdout, not to a file. In a container, stdout is what the Docker
    json-file driver captures to
    /var/lib/docker/containers/<id>/<id>-json.log, which is the file Filebeat
    tails. An app that writes its own log file inside a container has to have
    that file mounted, rotated and cleaned up by hand; writing to stdout makes
    the runtime responsible for all three.
    """
    level_name = (level or os.getenv("LOG_LEVEL", "INFO")).upper()
    chosen = (log_format or os.getenv("LOG_FORMAT", "json")).lower()

    handler = logging.StreamHandler(stream or sys.stdout)
    handler.setFormatter(
        TextFormatter() if chosen == "text" else EcsJsonFormatter(service=service)
    )
    handler.addFilter(SensitiveDataFilter())

    root = logging.getLogger()
    # Replace rather than append: uvicorn and logging.basicConfig both install
    # their own handlers, and leaving them attached prints every line twice —
    # once as JSON and once in whatever format they chose. Filebeat would then
    # index the plain-text copy as an unparsed message.
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level_name)

    # uvicorn installs handlers on these at startup. Clearing them and letting
    # the records propagate to the root handler puts access and error logs in
    # the same JSON shape as the application's own.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers.clear()
        uvicorn_logger.propagate = True

    return root


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
