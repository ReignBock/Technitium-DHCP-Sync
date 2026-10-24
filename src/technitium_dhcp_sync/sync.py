"""Run sync cycles: read both servers, plan, apply the plan, and handle primary outages."""

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from types import TracebackType
from typing import Self

from .client import TechnitiumClient, TechnitiumError
from .config import SyncSettings
from .failback import scope_holds, scopes_to_enable
from .planner import (
    Action,
    DisableScope,
    EnableScope,
    ScopeState,
    SecondaryOverrides,
    SetScope,
    plan,
)

logger = logging.getLogger(__name__)


class PrimaryUnreachableError(TechnitiumError):
    """A read from the primary failed, so the cycle stopped before any write."""


@dataclass(frozen=True)
class CycleResult:
    """What one cycle applied, what failed, and what it learned about both servers."""

    actions: tuple[Action, ...]
    failures: tuple[str, ...]
    primary_enabled: Mapping[str, bool] = field(default_factory=dict)
    holds: Mapping[str, datetime] = field(default_factory=dict)


@dataclass(frozen=True)
class CycleOutcome:
    """Whether a cycle fully succeeded, and how long to wait before the next one."""

    ok: bool
    wait_seconds: float


def read_state(client: TechnitiumClient) -> dict[str, ScopeState]:
    """Read every scope on a server with its enabled flag.

    Returns:
        The scopes keyed by name.
    """
    return {
        summary.name: ScopeState(client.get_scope(summary.name), enabled=summary.enabled)
        for summary in client.list_scopes()
    }


def _margin(settings: SyncSettings) -> timedelta:
    return timedelta(seconds=settings.failback_margin_seconds)


def run_cycle(
    primary: TechnitiumClient,
    secondary: TechnitiumClient,
    settings: SyncSettings,
    *,
    now: datetime,
) -> CycleResult:
    """Make the secondary match the primary, holding off scopes whose clients must move back.

    All reads happen before any write. If a primary read fails, ``PrimaryUnreachableError``
    propagates and the secondary stays as it was. A failed secondary read raises
    ``TechnitiumError``. A failed write is logged and skips the remaining actions for that
    scope only.

    Returns:
        The actions applied, the failures seen, the primary's enabled flags, and the holds.

    Raises:
        PrimaryUnreachableError: A read from the primary failed.
    """
    try:
        primary_state = read_state(primary)
        leases = primary.list_leases()
    except TechnitiumError as error:
        raise PrimaryUnreachableError(str(error)) from error
    secondary_state = read_state(secondary)
    holds = scope_holds(secondary.list_leases(), now=now, margin=_margin(settings))

    for name, until in holds.items():
        current = secondary_state.get(name)
        if current is not None and current.enabled:
            logger.info(
                "%s has clients on the secondary; on hold until %s so they return to the primary",
                name,
                until.isoformat(timespec="seconds"),
            )

    applied: list[Action] = []
    failures: list[str] = []
    failed_scopes: set[str] = set()
    for action in plan(
        primary_state,
        secondary_state,
        leases,
        held=holds.keys(),
        overrides=SecondaryOverrides(
            offer_delay_increment_ms=settings.offer_delay_increment_ms,
            lease_time_minutes=settings.secondary_lease_time_minutes,
        ),
    ):
        name = _scope_name(action)
        if name in failed_scopes:
            continue
        try:
            _apply(secondary, action)
        except (TechnitiumError, ValueError) as error:
            message = f"{type(action).__name__} {name}: {error}"
            # The message already names the cause; a traceback would only add noise.
            logger.error("sync failed: %s", message)  # ruff: ignore[error-instead-of-exception]
            failures.append(message)
            failed_scopes.add(name)
            continue
        logger.info("applied %s %s", type(action).__name__, name)
        applied.append(action)

    if not applied and not failures:
        logger.debug("secondary already matches the primary")
    return CycleResult(
        actions=tuple(applied),
        failures=tuple(failures),
        primary_enabled={name: state.enabled for name, state in primary_state.items()},
        holds=holds,
    )


def release_holds(
    secondary: TechnitiumClient,
    last_primary_enabled: Mapping[str, bool],
    settings: SyncSettings,
    *,
    now: datetime,
) -> list[str]:
    """Enable the disabled secondary scopes that should serve while the primary is down.

    Returns:
        The names of the scopes enabled.
    """
    secondary_enabled = {summary.name: summary.enabled for summary in secondary.list_scopes()}
    holds = scope_holds(secondary.list_leases(), now=now, margin=_margin(settings))
    names = scopes_to_enable(secondary_enabled, last_primary_enabled, holds)
    for name in names:
        secondary.enable_scope(name)
        logger.warning("primary unreachable; enabled %s on the secondary", name)
    return names


def _utc_now() -> datetime:
    return datetime.now(UTC)


class Syncer:
    """Runs cycles and remembers the primary's enabled flags between them.

    The flags decide which scopes to enable when the primary becomes unreachable. They live in
    memory only: after a restart during an outage, held scopes are enabled instead.
    """

    def __init__(
        self,
        primary: TechnitiumClient,
        secondary: TechnitiumClient,
        settings: SyncSettings,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        """Sync from ``primary`` to ``secondary``, reading the time from ``clock``."""
        self._primary = primary
        self._secondary = secondary
        self._settings = settings
        self._clock = clock
        self._last_primary_enabled: dict[str, bool] = {}

    def __enter__(self) -> Self:
        """Return the syncer itself.

        Returns:
            This syncer.
        """
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close both clients."""
        self._primary.__exit__(exc_type, exc, traceback)
        self._secondary.__exit__(exc_type, exc, traceback)

    def cycle(self) -> CycleOutcome:
        """Run one cycle.

        While the primary is unreachable, held scopes are enabled at once, and the next check
        comes after ``down_check_interval_seconds`` instead of ``interval_seconds``. A failed
        secondary read raises ``TechnitiumError`` for the caller to retry.

        Returns:
            Whether the cycle fully succeeded, and how long to wait before the next one.
        """
        now = self._clock()
        try:
            result = run_cycle(self._primary, self._secondary, self._settings, now=now)
        except PrimaryUnreachableError as error:
            logger.warning("primary unreachable: %s", error)
            try:
                release_holds(self._secondary, self._last_primary_enabled, self._settings, now=now)
            except TechnitiumError:
                logger.exception("cannot end holds on the secondary")
            return CycleOutcome(ok=False, wait_seconds=self._settings.down_check_interval_seconds)
        self._last_primary_enabled = dict(result.primary_enabled)
        return CycleOutcome(ok=not result.failures, wait_seconds=self._settings.interval_seconds)


def _scope_name(action: Action) -> str:
    return action.scope.name if isinstance(action, SetScope) else action.name


def _apply(client: TechnitiumClient, action: Action) -> None:
    match action:
        case SetScope(scope=scope):
            client.set_scope(scope)
        case EnableScope(name=name):
            client.enable_scope(name)
        case DisableScope(name=name):
            client.disable_scope(name)
        case _:
            client.delete_scope(action.name)
