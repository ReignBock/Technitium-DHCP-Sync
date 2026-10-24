"""Tests for loading the TOML settings file."""

from pathlib import Path

import pytest

from technitium_dhcp_sync.config import ConfigError, Settings, load_settings

ENVIRON = {"PRIMARY_TOKEN": "primary-secret", "SECONDARY_TOKEN": "secondary-secret"}

MINIMAL = """
[primary]
url = "https://dns1.example.com:53443"
token_env = "PRIMARY_TOKEN"

[secondary]
url = "http://127.0.0.1:5380"
token_env = "SECONDARY_TOKEN"
"""


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(text, encoding="utf-8")
    return path


def test_minimal_file_uses_the_documented_defaults(tmp_path: Path) -> None:
    settings = load_settings(write(tmp_path, MINIMAL), ENVIRON)

    assert settings.sync.interval_seconds == 60
    assert settings.sync.offer_delay_increment_seconds == 2
    assert settings.sync.offer_delay_increment_ms == 2000
    assert settings.sync.secondary_lease_time_minutes == 5
    assert settings.sync.log_level == "INFO"
    assert settings.sync.down_check_interval_seconds == 10
    assert settings.sync.failback_margin_seconds == 60
    assert settings.primary.verify_tls is True
    assert settings.primary.timeout_seconds == 10


def test_tokens_are_read_from_the_named_environment_variables(tmp_path: Path) -> None:
    settings = load_settings(write(tmp_path, MINIMAL), ENVIRON)

    assert settings.primary.token.get_secret_value() == "primary-secret"
    assert settings.secondary.token.get_secret_value() == "secondary-secret"


def test_tokens_never_appear_in_the_settings_repr(tmp_path: Path) -> None:
    settings = load_settings(write(tmp_path, MINIMAL), ENVIRON)

    assert "secret" not in repr(settings)


def test_every_setting_can_be_overridden(tmp_path: Path) -> None:
    text = """
[sync]
interval_seconds = 15
offer_delay_increment_seconds = 0.5
secondary_lease_time_minutes = 2
log_level = "DEBUG"
down_check_interval_seconds = 4
failback_margin_seconds = 0

[primary]
url = "https://dns1.example.com:53443"
token_env = "PRIMARY_TOKEN"
verify_tls = false
timeout_seconds = 3

[secondary]
url = "http://127.0.0.1:5380"
token_env = "SECONDARY_TOKEN"
"""

    settings = load_settings(write(tmp_path, text), ENVIRON)

    assert settings.sync.interval_seconds == 15
    assert settings.sync.offer_delay_increment_ms == 500
    assert settings.sync.secondary_lease_time_minutes == 2
    assert settings.sync.log_level == "DEBUG"
    assert settings.sync.down_check_interval_seconds == 4
    assert settings.sync.failback_margin_seconds == 0
    assert settings.primary.verify_tls is False
    assert settings.primary.timeout_seconds == 3


def test_missing_environment_variable_names_the_variable(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="SECONDARY_TOKEN"):
        load_settings(write(tmp_path, MINIMAL), {"PRIMARY_TOKEN": "x"})


def test_empty_environment_variable_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="PRIMARY_TOKEN"):
        load_settings(write(tmp_path, MINIMAL), {**ENVIRON, "PRIMARY_TOKEN": ""})


def test_token_env_must_be_a_variable_name(tmp_path: Path) -> None:
    text = MINIMAL.replace('token_env = "PRIMARY_TOKEN"', "token_env = 5")

    with pytest.raises(ConfigError, match="name of an environment variable"):
        load_settings(write(tmp_path, text), ENVIRON)


def test_a_token_written_into_the_file_is_rejected(tmp_path: Path) -> None:
    text = MINIMAL.replace('token_env = "PRIMARY_TOKEN"', 'token = "primary-secret"')

    with pytest.raises(ConfigError, match="token"):
        load_settings(write(tmp_path, text), ENVIRON)


def test_unknown_keys_are_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="interval"):
        load_settings(write(tmp_path, MINIMAL + "[sync]\ninterval = 5\n"), ENVIRON)


@pytest.mark.parametrize(
    "sync_table",
    [
        "interval_seconds = 0",
        "offer_delay_increment_seconds = -1",
        "secondary_lease_time_minutes = 0",
        'log_level = "LOUD"',
        "down_check_interval_seconds = 0",
        "failback_margin_seconds = -1",
    ],
)
def test_out_of_range_values_are_rejected(tmp_path: Path, sync_table: str) -> None:
    with pytest.raises(ConfigError):
        load_settings(write(tmp_path, f"{MINIMAL}[sync]\n{sync_table}\n"), ENVIRON)


def test_url_must_be_http_or_https(tmp_path: Path) -> None:
    text = MINIMAL.replace("http://127.0.0.1:5380", "ftp://127.0.0.1")

    with pytest.raises(ConfigError, match="url"):
        load_settings(write(tmp_path, text), ENVIRON)


def test_missing_file_is_a_config_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=r"missing\.toml"):
        load_settings(tmp_path / "missing.toml", ENVIRON)


def test_invalid_toml_is_a_config_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=r"config\.toml"):
        load_settings(write(tmp_path, "[primary\n"), ENVIRON)


def test_environment_defaults_to_the_process_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name, value in ENVIRON.items():
        monkeypatch.setenv(name, value)

    settings = load_settings(write(tmp_path, MINIMAL))

    assert settings.primary.token.get_secret_value() == "primary-secret"


def test_settings_can_be_built_without_a_file() -> None:
    settings = Settings.model_validate(
        {
            "primary": {"url": "http://a:5380", "token_env": "PRIMARY_TOKEN"},
            "secondary": {"url": "http://b:5380", "token_env": "SECONDARY_TOKEN"},
        },
        context={"environ": ENVIRON},
    )

    assert str(settings.secondary.url) == "http://b:5380/"
