"""Redaction helpers.

Two things must never reach a log line, an event body, an HTTP error payload or
the LLM:

1. SQL text, bound parameters, or database result rows.
2. Page bodies, cookies and any synthetic session identity beyond a random ID.

Driver exceptions routinely embed the failing statement, so exception text is
never passed through verbatim. We keep the exception *class* and, for database
errors, the SQLSTATE code -- both are safe, closed vocabularies that are enough
to classify a failure.
"""

from __future__ import annotations

import re

_SQLSTATE_RE = re.compile(r"^[0-9A-Z]{5}$")

# Substrings that indicate the message is likely to carry statement text.
_UNSAFE_HINTS = (
    "select",
    "insert",
    "update",
    "delete",
    "from",
    "where",
    "join",
    "explain",
    "values",
    "--",
    ";",
)


def redact_exception(exc: BaseException) -> str:
    """Return a safe, low-cardinality description of an exception.

    Always safe to log. Never contains statement text, parameters or row values.
    """
    name = type(exc).__name__
    sqlstate = getattr(exc, "sqlstate", None)
    if isinstance(sqlstate, str) and _SQLSTATE_RE.match(sqlstate):
        return f"{name}[sqlstate={sqlstate}]"
    return name


def safe_error_category(exc: BaseException) -> str:
    """Coarse error category for metrics. Closed vocabulary."""
    name = type(exc).__name__.lower()
    if "timeout" in name:
        return "timeout"
    if "connect" in name or "connection" in name:
        return "connection"
    if "ssl" in name or "certificate" in name:
        return "tls"
    return "other"


def assert_no_statement_text(value: str, *, field: str) -> None:
    """Defensive guard for strings about to be exported.

    Raises rather than exporting something that looks like SQL. Used on fields
    that are meant to be sanitised summaries, so a future refactor that
    accidentally threads raw text through fails loudly in tests.
    """
    lowered = value.lower()
    for hint in _UNSAFE_HINTS:
        if hint in lowered:
            raise ValueError(
                f"refusing to export field {field!r}: value resembles statement text"
            )


def scrub_text(value: str, *, max_length: int = 120) -> str:
    """Last-resort scrubber for free text that must still be human readable."""
    collapsed = " ".join(value.split())
    for hint in _UNSAFE_HINTS:
        if hint in collapsed.lower():
            return "<redacted>"
    return collapsed[:max_length]
