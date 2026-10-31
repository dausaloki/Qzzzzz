"""Crash-safe parsing of numeric environment variables.

``int(os.getenv("DEFAULT_TIMER", "45"))`` raises ``ValueError`` at import time
when the variable is set but empty (Railway lets you save a variable with no
value) or contains a typo such as ``45s`` -- the bot then crash-loops on start.

``env_int`` / ``env_float`` return exactly what ``int()`` / ``float()`` return
for every valid value (surrounding whitespace was already ignored by both), so
valid configurations behave identically.  For an empty value the default is
used silently (same as "not set"); for an invalid value the default is used and
the variable NAME is recorded in ``INVALID`` so start-up can log a warning.
The raw value is deliberately not stored or logged: a secret pasted into the
wrong variable must never end up in the logs.

No imports from the project: ``config`` and ``pdf_extract`` both use it.
"""
from __future__ import annotations

import os

# (variable name, default used) for every variable whose value was rejected.
INVALID: list[tuple[str, str]] = []


def _record(name: str, default) -> None:
    item = (name, str(default))
    if item not in INVALID:
        INVALID.append(item)


def env_int(name: str, default) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return int(default)
    try:
        return int(raw)
    except ValueError:
        _record(name, default)
        return int(default)


def env_float(name: str, default) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return float(default)
    try:
        return float(raw)
    except ValueError:
        _record(name, default)
        return float(default)


def invalid_settings() -> list[tuple[str, str]]:
    """Return a copy of the rejected variables as ``(name, default_used)``."""
    return list(INVALID)
