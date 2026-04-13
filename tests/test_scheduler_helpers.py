"""Unit tests for scheduler.get_next_run_times."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

from artimesone.scheduler import get_next_run_times


def _stub_scheduler(jobs: list[Any]) -> Any:
    return SimpleNamespace(get_jobs=lambda: jobs)


def test_get_next_run_times_none_scheduler() -> None:
    assert get_next_run_times(None) == {}


def test_get_next_run_times_empty() -> None:
    assert get_next_run_times(_stub_scheduler([])) == {}


def test_get_next_run_times_maps_source_jobs() -> None:
    dt1 = datetime(2026, 4, 13, 18, 0, tzinfo=UTC)
    dt2 = datetime(2026, 4, 13, 19, 30, tzinfo=UTC)
    jobs = [
        SimpleNamespace(id="source-1", next_run_time=dt1),
        SimpleNamespace(id="source-7", next_run_time=dt2),
    ]
    result = get_next_run_times(_stub_scheduler(jobs))
    assert result == {1: dt1, 7: dt2}


def test_get_next_run_times_paused_job_yields_none() -> None:
    jobs = [SimpleNamespace(id="source-3", next_run_time=None)]
    result = get_next_run_times(_stub_scheduler(jobs))
    assert result == {3: None}


def test_get_next_run_times_ignores_non_source_jobs() -> None:
    jobs = [
        SimpleNamespace(id="cleanup", next_run_time=datetime.now(UTC)),
        SimpleNamespace(id="source-abc", next_run_time=datetime.now(UTC)),
        SimpleNamespace(id="source-5", next_run_time=datetime.now(UTC)),
    ]
    result = get_next_run_times(_stub_scheduler(jobs))
    assert set(result.keys()) == {5}
