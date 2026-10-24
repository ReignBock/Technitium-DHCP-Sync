"""Load settings from a TOML file.

Secrets never live in the file. A key that ends in ``_env`` holds the name of an environment
variable, and the loader reads the secret from that variable.
"""

import os
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    HttpUrl,
    SecretStr,
    ValidationError,
    ValidationInfo,
)


class ConfigError(Exception):
    """The settings file is missing, unreadable, or invalid."""


def _read_env(name: object, info: ValidationInfo) -> SecretStr:
    if not isinstance(name, str):
        msg = "must be the name of an environment variable"
        # Pydantic turns only ValueError into a validation error; TypeError would escape.
        raise ValueError(msg)  # ruff: ignore[type-check-without-type-error]
    context = info.context or {}
    environ: Mapping[str, str] = context.get("environ", os.environ)
    value = environ.get(name)
    if not value:
        msg = f"environment variable {name} is not set or is empty"
        raise ValueError(msg)
    return SecretStr(value)


EnvSecret = Annotated[SecretStr, BeforeValidator(_read_env)]


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ServerSettings(_Section):
    """How to reach one Technitium server's HTTP API."""

    url: HttpUrl
    token: EnvSecret = Field(alias="token_env")
    verify_tls: bool = True
    timeout_seconds: float = Field(default=10, gt=0)


class SyncSettings(_Section):
    """How often to sync, how the secondary scopes differ, and how failback behaves."""

    interval_seconds: float = Field(default=60, gt=0)
    offer_delay_increment_seconds: float = Field(default=2, ge=0)
    secondary_lease_time_minutes: int = Field(default=5, ge=1)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    down_check_interval_seconds: float = Field(default=10, gt=0)
    failback_margin_seconds: float = Field(default=60, ge=0)

    @property
    def offer_delay_increment_ms(self) -> int:
        """The offer delay increment in milliseconds, the unit the API uses."""
        return round(self.offer_delay_increment_seconds * 1000)


class Settings(_Section):
    """Everything the sync needs: the two servers and the sync options."""

    sync: SyncSettings = Field(default_factory=SyncSettings)
    primary: ServerSettings
    secondary: ServerSettings


def load_settings(path: Path, environ: Mapping[str, str] | None = None) -> Settings:
    """Read and validate the settings file.

    Args:
        path: The TOML file to read.
        environ: Where to look up ``*_env`` variables. Defaults to the process environment.

    Returns:
        The validated settings, with secrets resolved.

    Raises:
        ConfigError: The file cannot be read or parsed, or a setting is invalid.
    """
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as error:
        msg = f"cannot read {path}: {error}"
        raise ConfigError(msg) from error
    try:
        return Settings.model_validate(
            data, context={"environ": os.environ if environ is None else environ}
        )
    except ValidationError as error:
        problems = "; ".join(
            f"{'.'.join(str(part) for part in problem['loc'])}: {problem['msg']}"
            for problem in error.errors(include_url=False)
        )
        msg = f"invalid settings in {path}: {problems}"
        raise ConfigError(msg) from error
