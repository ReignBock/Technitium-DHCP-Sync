"""Tests for the daemon loop and its signal handling."""

import logging
import signal
import threading
from collections.abc import Callable
from typing import TYPE_CHECKING

import pytest

from technitium_dhcp_sync.client import TechnitiumError
from technitium_dhcp_sync.runner import install_signal_handlers, run_forever

if TYPE_CHECKING:
    from types import FrameType


def stop_after(calls: int, stop: threading.Event, *, fail: bool = False) -> Callable[[], float]:
    count = 0

    def cycle() -> float:
        nonlocal count
        count += 1
        if count >= calls:
            stop.set()
        if fail:
            msg = f"down on call {count}"
            raise TechnitiumError(msg)
        return 0

    return cycle


class RecordingEvent(threading.Event):
    """A stop event that records each wait instead of sleeping."""

    def __init__(self) -> None:
        """Start with no waits recorded."""
        super().__init__()
        self.waits: list[float | None] = []

    def wait(self, timeout: float | None = None) -> bool:
        """Record the timeout and return at once."""
        self.waits.append(timeout)
        return self.is_set()


def test_runs_cycles_until_stopped() -> None:
    stop = threading.Event()
    calls: list[int] = []
    cycle = stop_after(3, stop)

    def counted() -> float:
        calls.append(1)
        return cycle()

    run_forever(counted, error_wait_seconds=0, stop=stop)

    assert len(calls) == 3


def test_a_failed_cycle_is_logged_and_the_loop_continues(
    caplog: pytest.LogCaptureFixture,
) -> None:
    stop = threading.Event()

    with caplog.at_level(logging.WARNING):
        run_forever(stop_after(2, stop, fail=True), error_wait_seconds=0, stop=stop)

    assert "down on call 1" in caplog.text
    assert "down on call 2" in caplog.text


def test_failures_log_an_error_once_then_warnings_then_recovery(
    caplog: pytest.LogCaptureFixture,
) -> None:
    stop = threading.Event()
    results: list[bool] = [False, False, True, True]

    def cycle() -> float:
        ok = results.pop(0)
        if not results:
            stop.set()
        if not ok:
            msg = "secondary down"
            raise TechnitiumError(msg)
        return 0

    with caplog.at_level(logging.INFO):
        run_forever(cycle, error_wait_seconds=0, stop=stop)

    assert [record.levelname for record in caplog.records] == ["ERROR", "WARNING", "INFO"]
    assert not any(record.exc_info for record in caplog.records)


def test_debug_logging_adds_the_traceback(caplog: pytest.LogCaptureFixture) -> None:
    stop = threading.Event()

    with caplog.at_level(logging.DEBUG):
        run_forever(stop_after(1, stop, fail=True), error_wait_seconds=0, stop=stop)

    assert caplog.records[0].exc_info


def test_an_unexpected_error_stops_the_loop() -> None:
    def cycle() -> float:
        msg = "bug"
        raise RuntimeError(msg)

    with pytest.raises(RuntimeError, match="bug"):
        run_forever(cycle, error_wait_seconds=0, stop=threading.Event())


def test_does_nothing_when_already_stopped() -> None:
    stop = threading.Event()
    stop.set()
    calls: list[int] = []

    run_forever(lambda: float(len(calls)), error_wait_seconds=0, stop=stop)

    assert calls == []


def test_waits_as_long_as_the_cycle_asks() -> None:
    stop = RecordingEvent()
    waits = [60.0, 10.0]

    def cycle() -> float:
        wait = waits[len(stop.waits)]
        if len(stop.waits) == len(waits) - 1:
            stop.set()
        return wait

    run_forever(cycle, error_wait_seconds=99, stop=stop)

    assert stop.waits == [60.0, 10.0]


def test_waits_the_error_interval_after_a_failed_cycle() -> None:
    stop = RecordingEvent()

    run_forever(stop_after(1, stop, fail=True), error_wait_seconds=99, stop=stop)

    assert stop.waits == [99]


@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGINT])
def test_sigterm_and_sigint_set_the_stop_event(
    monkeypatch: pytest.MonkeyPatch, signum: signal.Signals
) -> None:
    handlers: dict[int, Callable[[int, FrameType | None], object]] = {}
    monkeypatch.setattr(signal, "signal", handlers.__setitem__)
    stop = threading.Event()

    install_signal_handlers(stop)
    handlers[signum](signum, None)

    assert stop.is_set()
