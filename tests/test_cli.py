"""Tests for the command-line entry point."""

import runpy
import sys
import threading
from collections.abc import Callable
from pathlib import Path

import pytest

from technitium_dhcp_sync import cli
from technitium_dhcp_sync.client import TechnitiumClient
from technitium_dhcp_sync.config import ServerSettings

from .fakes import TOKEN, FakeTechnitium
from .test_models import MINIMAL_SCOPE

CONFIG = """
[sync]
interval_seconds = 30
log_level = "DEBUG"

[primary]
url = "http://primary.test:5380"
token_env = "PRIMARY_TOKEN"

[secondary]
url = "http://secondary.test:5380"
token_env = "SECONDARY_TOKEN"
"""


@pytest.fixture
def servers(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, FakeTechnitium]:
    # Point the CLI at two fakes, keyed by host name, and write a config for them.
    fakes = {"primary.test": FakeTechnitium(), "secondary.test": FakeTechnitium()}
    fakes["primary.test"].add_scope(MINIMAL_SCOPE, enabled=True)

    def from_settings(settings: ServerSettings) -> TechnitiumClient:
        assert settings.url.host is not None
        return fakes[settings.url.host].client()

    monkeypatch.setattr(TechnitiumClient, "from_settings", from_settings)
    monkeypatch.setenv("PRIMARY_TOKEN", TOKEN)
    monkeypatch.setenv("SECONDARY_TOKEN", TOKEN)
    (tmp_path / "config.toml").write_text(CONFIG, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    return fakes


def test_once_syncs_and_exits_zero(servers: dict[str, FakeTechnitium]) -> None:
    assert cli.main(["--config", "config.toml", "--once"]) == 0

    assert "Lab" in servers["secondary.test"].scopes


def test_once_exits_one_when_the_primary_is_down(servers: dict[str, FakeTechnitium]) -> None:
    servers["primary.test"].reachable = False

    assert cli.main(["--config", "config.toml", "--once"]) == 1


def test_once_exits_one_when_the_secondary_is_down(servers: dict[str, FakeTechnitium]) -> None:
    servers["secondary.test"].reachable = False

    assert cli.main(["--config", "config.toml", "--once"]) == 1


def test_once_exits_one_when_an_action_fails(servers: dict[str, FakeTechnitium]) -> None:
    servers["primary.test"].add_scope({
        **MINIMAL_SCOPE,
        "name": "Bad",
        "reservedLeases": [
            {"hardwareAddress": "00-00-00-00-00-01", "address": "10.0.0.9", "comments": "a|b"}
        ],
    })

    assert cli.main(["--config", "config.toml", "--once"]) == 1


def test_bad_config_exits_two_with_the_reason(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["--config", "/nonexistent/config.toml", "--once"]) == 2

    assert "/nonexistent/config.toml" in capsys.readouterr().err


def test_daemon_mode_loops_with_the_configured_interval(
    servers: dict[str, FakeTechnitium], monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, object] = {}

    def fake_run_forever(
        cycle: Callable[[], float], *, error_wait_seconds: float, stop: threading.Event
    ) -> None:
        seen["error_wait"] = error_wait_seconds
        seen["stop"] = stop
        seen["wait"] = cycle()

    installed: list[threading.Event] = []
    monkeypatch.setattr(cli, "run_forever", fake_run_forever)
    monkeypatch.setattr(cli, "install_signal_handlers", installed.append)

    assert cli.main(["--config", "config.toml"]) == 0

    assert seen["error_wait"] == 30
    assert seen["wait"] == 30
    assert installed == [seen["stop"]]
    assert "Lab" in servers["secondary.test"].scopes


def test_config_path_defaults_to_the_container_location() -> None:
    assert cli.parse_args([]).config == Path("/config/config.toml")


def test_python_dash_m_runs_main(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["technitium-dhcp-sync", "--config", "/nonexistent"])

    with pytest.raises(SystemExit) as exit_info:
        runpy.run_module("technitium_dhcp_sync", run_name="__main__")

    assert exit_info.value.code == 2
