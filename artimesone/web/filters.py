"""Jinja2 template filters for the web UI.

Registers custom filters for formatting durations, relative dates, and
text truncation. Called from ``create_app()`` after the templates
environment is set up.
"""

from __future__ import annotations

from datetime import UTC, datetime

from jinja2 import Environment

_RELATIVE_EMPTY = "—"


def format_duration(seconds: int | float | None) -> str:
    """Format seconds as ``H:MM:SS`` or ``M:SS``.

    Returns an empty string for ``None`` or non-positive values.
    """
    if seconds is None or seconds <= 0:
        return ""
    total = int(seconds)
    h, remainder = divmod(total, 3600)
    m, s = divmod(remainder, 60)
    if h > 0:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def relative_date(iso_string: str | None) -> str:
    """Convert an ISO 8601 timestamp to a short human-readable relative date.

    Returns the original string unchanged if parsing fails.
    """
    if not iso_string:
        return ""
    try:
        dt = datetime.fromisoformat(iso_string)
    except (ValueError, TypeError):
        return str(iso_string)

    now = datetime.now(UTC)
    # Normalize to UTC if naive
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)

    today = now.date()
    target = dt.date()
    delta = today - target

    if delta.days == 0:
        return "today"
    if delta.days == 1:
        return "yesterday"
    if delta.days < 7:
        return f"{delta.days} days ago"
    return f"{target.strftime('%b')} {target.day}"


def relative_time(value: datetime | str | None) -> str:
    """Render a datetime as a short relative span (``in 3h 27m``, ``2h ago``).

    Accepts a tz-aware ``datetime`` or an ISO 8601 string. Naive datetimes are
    assumed to be UTC. Returns :data:`_RELATIVE_EMPTY` for ``None`` or unparsable
    input. Units cascade: days+hours for ≥1d, hours+minutes for ≥1h, minutes for
    ≥1m, ``just now`` under a minute.
    """
    if value is None:
        return _RELATIVE_EMPTY

    if isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value)
        except (ValueError, TypeError):
            return _RELATIVE_EMPTY
    else:
        dt = value

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)

    now = datetime.now(UTC)
    delta = dt - now
    total_seconds = int(delta.total_seconds())
    future = total_seconds >= 0
    magnitude = abs(total_seconds)

    if magnitude < 60:
        return "just now"

    days, remainder = divmod(magnitude, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes = remainder // 60

    if days > 0:
        span = f"{days}d {hours}h" if hours else f"{days}d"
    elif hours > 0:
        span = f"{hours}h {minutes}m" if minutes else f"{hours}h"
    else:
        span = f"{minutes}m"

    return f"in {span}" if future else f"{span} ago"


def format_count(value: int | None) -> str:
    """Format a non-negative integer with K/M/B suffix (``1234`` → ``1.2K``).

    Returns an empty string for ``None`` or negative values. Values under 1000
    are rendered as-is. Suffixed values show one decimal unless it's zero
    (``1.0K`` → ``1K``). Uses 1000-based units (not KiB).
    """
    if value is None or value < 0:
        return ""
    if value < 1000:
        return str(value)
    for unit, threshold in (("K", 1_000), ("M", 1_000_000), ("B", 1_000_000_000)):
        next_threshold = threshold * 1000
        if value < next_threshold:
            scaled = value / threshold
            if scaled >= 100:
                return f"{int(scaled)}{unit}"
            formatted = f"{scaled:.1f}"
            if formatted.endswith(".0"):
                formatted = formatted[:-2]
            return f"{formatted}{unit}"
    # Values ≥ 1T: fall back to B with integer scaling.
    return f"{value // 1_000_000_000}B"


def first_paragraph(text: str | None) -> str:
    """Extract the first non-empty paragraph from *text*.

    Returns an empty string for ``None`` or empty input.
    """
    if not text:
        return ""
    for block in text.split("\n\n"):
        stripped = block.strip()
        if stripped:
            return stripped
    return text.strip()


def register_filters(env: Environment) -> None:
    """Register all custom filters on a Jinja2 environment."""
    env.filters["format_duration"] = format_duration
    env.filters["format_count"] = format_count
    env.filters["relative_date"] = relative_date
    env.filters["relative_time"] = relative_time
    env.filters["first_paragraph"] = first_paragraph
