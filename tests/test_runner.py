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


def stop_after(calls: int, stop: threading.Event, *, fail: bool = False) -> Callable[[], None]:
    count = 0

    def cycle() -> None:
        nonlocal count
        count += 1
        if count >= calls:
            stop.set()
        if fail:
            msg = f"down on call {count}"
            raise TechnitiumError(msg)

    return cycle


def test_runs_cycles_until_stopped() -> None:
    stop = threading.Event()
    calls: list[int] = []
    cycle = stop_after(3, stop)

    def counted() -> None:
        calls.append(1)
        cycle()

    run_forever(counted, interval_seconds=0, stop=stop)

    assert len(calls) == 3


def test_a_failed_cycle_is_logged_and_the_loop_continues(
    caplog: pytest.LogCaptureFixture,
) -> None:
    stop = threading.Event()

    with caplog.at_level(logging.ERROR):
        run_forever(stop_after(2, stop, fail=True), interval_seconds=0, stop=stop)

    assert "down on call 1" in caplog.text
    assert "down on call 2" in caplog.text


def test_an_unexpected_error_stops_the_loop() -> None:
    def cycle() -> None:
        msg = "bug"
        raise RuntimeError(msg)

    with pytest.raises(RuntimeError, match="bug"):
        run_forever(cycle, interval_seconds=0, stop=threading.Event())


def test_does_nothing_when_already_stopped() -> None:
    stop = threading.Event()
    stop.set()
    calls: list[int] = []

    run_forever(lambda: calls.append(1), interval_seconds=0, stop=stop)

    assert calls == []


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
