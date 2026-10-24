"""Checks of the shipped container image. They run only when ``SYNC_IMAGE`` names it."""

import os
import subprocess
import time
from collections.abc import Iterator

import pytest

from .conftest import Stack

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("SYNC_IMAGE"), reason="SYNC_IMAGE is not set"),
]

CONTAINER = "technitium-dhcp-sync-it-daemon"

# Short intervals, so the heartbeat goes stale within seconds of the primary stopping.
FAST_SYNC = """
[sync]
interval_seconds = 1
down_check_interval_seconds = 1
health_max_age_seconds = 3
"""


def docker(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["docker", *args], check=check, capture_output=True, text=True)


def health(timeout: float, want: str) -> str:
    deadline = time.monotonic() + timeout
    status = ""
    while time.monotonic() < deadline:
        status = docker("inspect", "--format", "{{.State.Health.Status}}", CONTAINER).stdout
        if status.strip() == want:
            break
        time.sleep(1)
    return status.strip()


@pytest.fixture
def daemon(stack: Stack) -> Iterator[None]:
    base = stack.config_path.read_text(encoding="utf-8").replace("[sync]\n", "")
    config = stack.config_path.with_name("fast.toml")
    config.write_text(FAST_SYNC + base, encoding="utf-8")
    config.chmod(0o644)
    docker("rm", "-f", CONTAINER, check=False)
    docker(
        "run", "-d", "--name", CONTAINER, "--network", "host", "--health-interval", "1s",
        "-e", f"PRIMARY_TOKEN={stack.primary.token}",
        "-e", f"SECONDARY_TOKEN={stack.secondary.token}",
        "-v", f"{config.parent}:/config:ro",
        os.environ["SYNC_IMAGE"], "--config", "/config/fast.toml",
    )  # fmt: skip
    try:
        yield
    finally:
        docker("rm", "-f", CONTAINER, check=False)


@pytest.mark.usefixtures("daemon")
def test_the_container_is_healthy_while_syncing_and_unhealthy_once_the_primary_stops(
    stack: Stack,
) -> None:
    assert health(30, "healthy") == "healthy"

    stack.primary.stop()

    assert health(30, "unhealthy") == "unhealthy"
