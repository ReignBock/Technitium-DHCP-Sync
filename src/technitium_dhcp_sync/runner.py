"""The daemon loop: run a sync cycle, wait, repeat until told to stop."""

import logging
import signal
import threading
from collections.abc import Callable
from types import FrameType

from .client import TechnitiumError

logger = logging.getLogger(__name__)


def run_forever(
    cycle: Callable[[], float], *, error_wait_seconds: float, stop: threading.Event
) -> None:
    """Call ``cycle`` until ``stop`` is set, waiting as long as each call returns.

    A ``TechnitiumError`` means a server was down or refused a call. The loop tries again
    after ``error_wait_seconds``. The first failure in a row logs an error, later ones a
    warning, and the next success logs once. Tracebacks appear only at DEBUG level. Any other
    exception is a bug and ends the loop.
    """
    failing = False
    while not stop.is_set():
        try:
            wait = cycle()
        except TechnitiumError as error:
            traceback = logger.isEnabledFor(logging.DEBUG)
            if failing:
                logger.warning("sync cycle failed again: %s", error, exc_info=traceback)
            else:
                # The traceback is added at DEBUG level only, through exc_info.
                logger.error(  # ruff: ignore[error-instead-of-exception]
                    "sync cycle failed; retrying every %s seconds: %s",
                    error_wait_seconds,
                    error,
                    exc_info=traceback,
                )
            failing = True
            wait = error_wait_seconds
        else:
            if failing:
                logger.info("sync cycle succeeded again")
            failing = False
        stop.wait(wait)


def install_signal_handlers(stop: threading.Event) -> None:
    """Set ``stop`` on SIGTERM or SIGINT, so ``docker stop`` ends the loop cleanly."""

    def handle(signum: int, _frame: FrameType | None) -> None:
        logger.info("received %s; stopping", signal.Signals(signum).name)
        stop.set()

    signal.signal(signal.SIGTERM, handle)
    signal.signal(signal.SIGINT, handle)
