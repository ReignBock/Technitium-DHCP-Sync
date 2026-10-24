"""Command-line entry point.

Exit codes: 0 success, 1 a ``--once`` cycle failed or ``--healthcheck`` found the sync
unhealthy, 2 the settings file is invalid.
"""

import argparse
import logging
import sys
import threading
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .client import TechnitiumClient, TechnitiumError
from .config import ConfigError, load_settings
from .health import DEFAULT_HEARTBEAT_FILE, heartbeat_problem, write_heartbeat
from .runner import install_signal_handlers, run_forever
from .sync import Syncer

logger = logging.getLogger(__name__)

EXIT_OK = 0
EXIT_CYCLE_FAILED = 1
EXIT_BAD_CONFIG = 2


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    """Parse the command line.

    Returns:
        The parsed arguments.
    """
    parser = argparse.ArgumentParser(
        prog="technitium-dhcp-sync",
        description="Mirror Technitium DHCP scopes and leases from a primary to a secondary.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("/config/config.toml"),
        help="settings file (default: %(default)s)",
    )
    parser.add_argument("--once", action="store_true", help="run one sync cycle, then exit")
    parser.add_argument(
        "--healthcheck",
        action="store_true",
        help="exit 0 if the daemon had a healthy cycle recently, 1 if not",
    )
    parser.add_argument(
        "--heartbeat-file",
        type=Path,
        default=DEFAULT_HEARTBEAT_FILE,
        help="where the daemon records healthy cycles (default: %(default)s)",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """Run the sync, once or as a daemon.

    Returns:
        The process exit code.
    """
    args = parse_args(argv)
    if args.healthcheck:
        problem = heartbeat_problem(args.heartbeat_file, now=datetime.now(UTC))
        if problem is None:
            return EXIT_OK
        sys.stderr.write(f"technitium-dhcp-sync: unhealthy: {problem}\n")
        return EXIT_CYCLE_FAILED
    try:
        settings = load_settings(args.config)
    except ConfigError as error:
        sys.stderr.write(f"technitium-dhcp-sync: {error}\n")
        return EXIT_BAD_CONFIG

    logging.basicConfig(
        level=settings.sync.log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        force=True,
    )
    primary = TechnitiumClient.from_settings(settings.primary)
    secondary = TechnitiumClient.from_settings(settings.secondary)
    with Syncer(primary, secondary, settings.sync) as syncer:
        if args.once:
            try:
                outcome = syncer.cycle()
            except TechnitiumError:
                logger.exception("sync cycle failed")
                return EXIT_CYCLE_FAILED
            return EXIT_OK if outcome.ok else EXIT_CYCLE_FAILED

        max_age = timedelta(seconds=settings.sync.health_max_age_seconds)

        def cycle() -> float:
            outcome = syncer.cycle()
            if outcome.ok:
                write_heartbeat(args.heartbeat_file, datetime.now(UTC) + max_age)
            return outcome.wait_seconds

        logger.info("syncing %s to %s", primary.base_url, secondary.base_url)
        stop = threading.Event()
        install_signal_handlers(stop)
        run_forever(cycle, error_wait_seconds=settings.sync.interval_seconds, stop=stop)
    return EXIT_OK
