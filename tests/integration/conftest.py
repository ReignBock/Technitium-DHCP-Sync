"""Fixtures that run the compose stack and drive the servers, the client, and the sync."""

import os
import re
import subprocess
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest

COMPOSE_FILE = Path(__file__).with_name("compose.yml")
PASSWORD = "integration-password"  # ruff: ignore[hardcoded-password-string]
CLIENT_MAC = "00-16-3E-1E-00-99"
PRIMARY_IP = "172.30.0.10"
SECONDARY_IP = "172.30.0.11"
_LEASE_LINE = re.compile(r"ip=(?P<ip>\S+) lease=(?P<lease>\d+) serverid=(?P<server>\S+)")


def compose(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", "compose", "-f", str(COMPOSE_FILE), *args],
        check=check,
        capture_output=True,
        text=True,
    )


@dataclass
class Server:
    """One Technitium container, reached through its published API port."""

    service: str
    url: str
    token: str = ""

    def wait_until_up(self, timeout: float = 120) -> None:
        """Poll the login endpoint until the API answers."""
        deadline = time.monotonic() + timeout
        while True:
            try:
                reply = httpx.get(
                    f"{self.url}/api/user/login", params={"user": "admin", "pass": PASSWORD}
                ).json()
            except (httpx.HTTPError, ValueError):
                reply = {}
            if reply.get("status") == "ok":
                return
            if time.monotonic() > deadline:
                msg = f"{self.service} API did not come up"
                raise TimeoutError(msg)
            time.sleep(1)

    def create_token(self) -> None:
        """Create a non-expiring API token for this server."""
        # API tokens persist on disk, so they survive a container stop and start.
        reply = httpx.get(
            f"{self.url}/api/user/createToken",
            params={"user": "admin", "pass": PASSWORD, "tokenName": "integration"},
        ).json()
        self.token = reply["token"]

    def call(self, endpoint: str, **params: str) -> dict[str, Any]:
        """Call a DHCP endpoint and return its response object."""
        reply = httpx.post(
            f"{self.url}/api/dhcp/{endpoint}",
            data=params,
            headers={"Authorization": f"Bearer {self.token}"},
        ).json()
        assert reply["status"] == "ok", reply
        response: dict[str, Any] = reply["response"]
        return response

    def scope(self, name: str) -> dict[str, Any]:
        """Return the raw ``scopes/get`` reply for one scope."""
        return self.call("scopes/get", name=name)

    def scope_names(self) -> set[str]:
        """Return the names of all scopes on this server."""
        return {scope["name"] for scope in self.call("scopes/list")["scopes"]}

    def delete_all_scopes(self) -> None:
        """Delete every scope, including the default one a fresh install has."""
        for name in self.scope_names():
            self.call("scopes/delete", name=name)

    def stop(self) -> None:
        """Stop the container."""
        compose("stop", self.service)

    def start(self) -> None:
        """Start the container and wait for its API."""
        compose("start", self.service)
        self.wait_until_up()


@dataclass(frozen=True)
class LeaseReply:
    """What the client was offered and accepted."""

    ip: str
    lease_seconds: int
    server: str


@dataclass
class Stack:
    """The running compose project."""

    primary: Server
    secondary: Server
    config_path: Path

    def run_sync(self) -> subprocess.CompletedProcess[str]:
        """Run one sync cycle as a separate process.

        With ``SYNC_IMAGE`` set, the cycle runs in that container image, so CI tests the image
        it ships. Otherwise it runs ``python -m technitium_dhcp_sync`` from this checkout.
        """
        tokens = {"PRIMARY_TOKEN": self.primary.token, "SECONDARY_TOKEN": self.secondary.token}
        image = os.environ.get("SYNC_IMAGE")
        if image:
            command = [
                "docker", "run", "--rm", "--network", "host",
                "-e", "PRIMARY_TOKEN", "-e", "SECONDARY_TOKEN",
                "-v", f"{self.config_path.parent}:/config:ro",
                image, "--config", f"/config/{self.config_path.name}", "--once",
            ]  # fmt: skip
        else:
            command = [
                sys.executable, "-m", "technitium_dhcp_sync",
                "--config", str(self.config_path), "--once",
            ]  # fmt: skip
        return subprocess.run(
            command, check=False, capture_output=True, text=True, env={**os.environ, **tokens}
        )


def request_lease() -> LeaseReply:
    """Run one DHCP exchange from the client container and report the lease."""
    result = compose(
        "exec", "-T", "client",
        "udhcpc", "-i", "eth0", "-n", "-q", "-f", "-t", "5", "-T", "2",
        "-s", "/usr/local/bin/print-lease",
    )  # fmt: skip
    match = _LEASE_LINE.search(result.stdout)
    assert match is not None, result.stdout + result.stderr
    return LeaseReply(match["ip"], int(match["lease"]), match["server"])


@pytest.fixture(scope="session")
def running_stack(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Stack]:
    compose("up", "-d", "--force-recreate", "--renew-anon-volumes")
    try:
        primary = Server("primary", "http://127.0.0.1:15380")
        secondary = Server("secondary", "http://127.0.0.1:25380")
        for server in (primary, secondary):
            server.wait_until_up()
            server.create_token()
        config_path = tmp_path_factory.mktemp("config") / "config.toml"
        config_path.write_text(
            f"""
[sync]
log_level = "DEBUG"

[primary]
url = "{primary.url}"
token_env = "PRIMARY_TOKEN"

[secondary]
url = "{secondary.url}"
token_env = "SECONDARY_TOKEN"
""",
            encoding="utf-8",
        )
        # The image runs as its own user. The file holds no secrets, so let anyone read it.
        config_path.parent.chmod(0o755)
        config_path.chmod(0o644)
        yield Stack(primary, secondary, config_path)
    finally:
        compose("down", "-v", check=False)


@pytest.fixture
def stack(running_stack: Stack) -> Iterator[Stack]:
    # Give each test both servers up and no scopes on either.
    for server in (running_stack.primary, running_stack.secondary):
        server.start()
        server.delete_all_scopes()
    yield running_stack
    running_stack.primary.start()
