"""The heartbeat file behind the container health check.

After each healthy cycle, the daemon writes a deadline to the file. The health check passes
while that deadline lies in the future. It reads only the file, so it needs no settings.
"""

import logging
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

# /tmp is the one path the container user can write. Only this process uses the file.
DEFAULT_HEARTBEAT_FILE = Path("/tmp/technitium-dhcp-sync.heartbeat")  # ruff: ignore[hardcoded-temp-file]


def write_heartbeat(path: Path, deadline: datetime) -> None:
    """Record that the sync is healthy until ``deadline``.

    The file is replaced in one step, so the health check never reads half a write. A failed
    write is logged, not raised: the sync itself still works, and the health check reports
    the stale file.
    """
    partial = path.with_name(f"{path.name}.partial")
    try:
        partial.write_text(deadline.isoformat(), encoding="utf-8")
        partial.replace(path)
    except OSError as error:
        # The message already names the cause; a traceback would only add noise.
        logger.error("cannot write heartbeat file: %s", error)  # ruff: ignore[error-instead-of-exception]


def heartbeat_problem(path: Path, *, now: datetime) -> str | None:
    """Explain why the heartbeat in ``path`` is not healthy at ``now``.

    Returns:
        ``None`` while the deadline lies in the future, otherwise the reason.
    """
    try:
        deadline = datetime.fromisoformat(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return f"no healthy cycle yet: {path} does not exist"
    except (OSError, ValueError) as error:
        return f"cannot read {path}: {error}"
    if deadline.tzinfo is None:
        return f"cannot read {path}: the deadline has no time zone"
    if deadline <= now:
        return f"no healthy cycle since the deadline {deadline.isoformat()} passed"
    return None
