"""Command-line entry point.

Exit codes: 0 success, 1 a ``--once`` cycle failed, 2 the settings file is invalid.
"""

import argparse
import logging
import sys
import threading
from collections.abc import Sequence
from pathlib import Path

from .client import TechnitiumClient, TechnitiumError
from .config import ConfigError, load_settings
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
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """Run the sync, once or as a daemon.

    Returns:
        The process exit code.
    """
    args = parse_args(argv)
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

        logger.info("syncing %s to %s", primary.base_url, secondary.base_url)
        stop = threading.Event()
        install_signal_handlers(stop)
        run_forever(
            lambda: syncer.cycle().wait_seconds,
            error_wait_seconds=settings.sync.interval_seconds,
            stop=stop,
        )
    return EXIT_OK
