"""Tests for the heartbeat file behind the container health check."""

import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from technitium_dhcp_sync.health import heartbeat_problem, write_heartbeat

NOW = datetime(2026, 10, 24, 14, 0, tzinfo=UTC)


def test_a_heartbeat_before_its_deadline_is_healthy(tmp_path: Path) -> None:
    path = tmp_path / "heartbeat"

    write_heartbeat(path, NOW + timedelta(seconds=1))

    assert heartbeat_problem(path, now=NOW) is None


def test_a_heartbeat_past_its_deadline_is_unhealthy(tmp_path: Path) -> None:
    path = tmp_path / "heartbeat"

    write_heartbeat(path, NOW)

    problem = heartbeat_problem(path, now=NOW)
    assert problem is not None
    assert "2026-10-24T14:00:00+00:00" in problem


def test_a_missing_heartbeat_is_unhealthy(tmp_path: Path) -> None:
    problem = heartbeat_problem(tmp_path / "heartbeat", now=NOW)

    assert problem is not None
    assert "no healthy cycle" in problem


@pytest.mark.parametrize("content", ["garbage", "2026-10-24T14:00:00"])
def test_an_unreadable_heartbeat_is_unhealthy(tmp_path: Path, content: str) -> None:
    path = tmp_path / "heartbeat"
    path.write_text(content, encoding="utf-8")

    problem = heartbeat_problem(path, now=NOW)

    assert problem is not None
    assert "cannot read" in problem


def test_a_new_heartbeat_replaces_the_old_one(tmp_path: Path) -> None:
    path = tmp_path / "heartbeat"
    write_heartbeat(path, NOW)

    write_heartbeat(path, NOW + timedelta(minutes=5))

    assert heartbeat_problem(path, now=NOW + timedelta(minutes=4)) is None
    assert [entry.name for entry in tmp_path.iterdir()] == ["heartbeat"]


def test_a_failed_write_is_logged_not_raised(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "missing-dir" / "heartbeat"

    with caplog.at_level(logging.ERROR):
        write_heartbeat(path, NOW)

    assert "missing-dir" in caplog.text
