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

    A ``TechnitiumError`` means a server was down or refused a call. It is logged, and the
    loop tries again after ``error_wait_seconds``. Any other exception is a bug and ends the
    loop.
    """
    while not stop.is_set():
        try:
            wait = cycle()
        except TechnitiumError:
            logger.exception("sync cycle failed; retrying in %s seconds", error_wait_seconds)
            wait = error_wait_seconds
        stop.wait(wait)


def install_signal_handlers(stop: threading.Event) -> None:
    """Set ``stop`` on SIGTERM or SIGINT, so ``docker stop`` ends the loop cleanly."""

    def handle(signum: int, _frame: FrameType | None) -> None:
        logger.info("received %s; stopping", signal.Signals(signum).name)
        stop.set()

    signal.signal(signal.SIGTERM, handle)
    signal.signal(signal.SIGINT, handle)
