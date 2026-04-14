"""Unit tests for Jinja template filters — web/filters.py."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from artimesone.web.filters import format_count, relative_time


def _delta(**kwargs: float) -> datetime:
    return datetime.now(UTC) + timedelta(**kwargs)


def test_relative_time_none() -> None:
    assert relative_time(None) == "—"


def test_relative_time_unparsable_string() -> None:
    assert relative_time("not-a-date") == "—"


def test_relative_time_just_now_future() -> None:
    assert relative_time(_delta(seconds=10)) == "just now"


def test_relative_time_just_now_past() -> None:
    assert relative_time(_delta(seconds=-10)) == "just now"


def test_relative_time_future_minutes() -> None:
    assert relative_time(_delta(minutes=45)) == "in 45m"


def test_relative_time_past_minutes() -> None:
    assert relative_time(_delta(minutes=-3)) == "3m ago"


def test_relative_time_future_hours_and_minutes() -> None:
    assert relative_time(_delta(hours=3, minutes=27)) == "in 3h 27m"


def test_relative_time_past_hours_only() -> None:
    # Exactly 2h ago — no trailing minutes component.
    assert relative_time(_delta(hours=-2)) == "2h ago"


def test_relative_time_future_days_and_hours() -> None:
    assert relative_time(_delta(days=2, hours=4)) == "in 2d 4h"


def test_relative_time_past_days_only() -> None:
    assert relative_time(_delta(days=-3)) == "3d ago"


def test_relative_time_accepts_iso_string() -> None:
    future_iso = _delta(hours=1, minutes=5).isoformat()
    assert relative_time(future_iso) == "in 1h 5m"


def test_relative_time_naive_datetime_treated_as_utc() -> None:
    naive = datetime.now(UTC).replace(tzinfo=None) + timedelta(minutes=10)
    assert relative_time(naive) == "in 10m"


def test_format_count_none_and_negative() -> None:
    assert format_count(None) == ""
    assert format_count(-1) == ""


def test_format_count_below_thousand() -> None:
    assert format_count(0) == "0"
    assert format_count(42) == "42"
    assert format_count(999) == "999"


def test_format_count_thousands() -> None:
    assert format_count(1000) == "1K"
    assert format_count(1234) == "1.2K"
    assert format_count(12_345) == "12.3K"
    assert format_count(123_456) == "123K"  # no decimal for 3-digit scaled


def test_format_count_millions() -> None:
    assert format_count(1_000_000) == "1M"
    assert format_count(1_500_000) == "1.5M"
    assert format_count(12_300_000) == "12.3M"


def test_format_count_billions() -> None:
    assert format_count(1_500_000_000) == "1.5B"
