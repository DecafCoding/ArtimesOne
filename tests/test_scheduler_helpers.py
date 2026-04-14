"""Unit tests for scheduler.get_next_round_time."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

from artimesone.scheduler import ROUND_JOB_ID, get_next_round_time


def _stub_scheduler(jobs: list[Any]) -> Any:
    job_map = {job.id: job for job in jobs}
    return SimpleNamespace(get_job=lambda job_id: job_map.get(job_id))


def test_get_next_round_time_none_scheduler() -> None:
    assert get_next_round_time(None) is None


def test_get_next_round_time_no_round_job() -> None:
    assert get_next_round_time(_stub_scheduler([])) is None


def test_get_next_round_time_returns_job_next_run() -> None:
    dt = datetime(2026, 4, 13, 18, 0, tzinfo=UTC)
    jobs = [SimpleNamespace(id=ROUND_JOB_ID, next_run_time=dt)]
    assert get_next_round_time(_stub_scheduler(jobs)) == dt


def test_get_next_round_time_paused_job_returns_none() -> None:
    jobs = [SimpleNamespace(id=ROUND_JOB_ID, next_run_time=None)]
    assert get_next_round_time(_stub_scheduler(jobs)) is None
