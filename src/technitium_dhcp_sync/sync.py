"""Run sync cycles: read both servers, plan, apply the plan, and handle primary outages."""

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from types import TracebackType
from typing import Self

from .client import TechnitiumAuthError, TechnitiumClient, TechnitiumError
from .config import SyncSettings
from .failback import outage_leases, scope_holds, scopes_to_enable
from .models import Lease
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
    outage_leases: tuple[Lease, ...] = ()


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
    propagates and the secondary stays as it was. A rejected primary token raises
    ``TechnitiumAuthError`` instead, since the primary is up. A failed secondary read raises
    ``TechnitiumError``. A failed write is logged and skips the remaining actions for that
    scope only.

    A primary with no scopes would empty the secondary. Unless ``allow_empty_primary`` is set,
    the cycle refuses that, writes nothing, and reports a failure.

    Returns:
        The actions applied, the failures seen, the primary's enabled flags, the holds, and the
        outage leases.

    Raises:
        PrimaryUnreachableError: A read from the primary failed.
        TechnitiumAuthError: The primary rejected the API token.
    """
    try:
        primary_state = read_state(primary)
        leases = primary.list_leases()
    except TechnitiumAuthError:
        raise
    except TechnitiumError as error:
        raise PrimaryUnreachableError(str(error)) from error
    secondary_state = read_state(secondary)
    secondary_leases = secondary.list_leases()
    holds = scope_holds(secondary_leases, now=now, margin=_margin(settings))
    strays = tuple(outage_leases(secondary_leases, leases, now=now))

    if not primary_state and secondary_state and not settings.allow_empty_primary:
        message = (
            f"the primary has no scopes; refusing to delete all {len(secondary_state)} "
            "secondary scopes (set allow_empty_primary to allow this)"
        )
        logger.error("sync failed: %s", message)
        return CycleResult(actions=(), failures=(message,), holds=holds, outage_leases=strays)

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
        outage_leases=strays,
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
        self._outage_since: datetime | None = None
        self._reported_leases: set[tuple[str, str]] = set()

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
        comes after ``down_check_interval_seconds`` instead of ``interval_seconds``. The first
        failed check logs an error, later ones a warning, and the recovery logs once. A failed
        secondary read or a rejected token raises ``TechnitiumError`` for the caller to retry.

        Returns:
            Whether the cycle fully succeeded, and how long to wait before the next one.
        """
        now = self._clock()
        try:
            result = run_cycle(self._primary, self._secondary, self._settings, now=now)
        except PrimaryUnreachableError as error:
            if self._outage_since is None:
                self._outage_since = now
                # The message already names the cause; a traceback would only add noise.
                logger.error("primary unreachable: %s", error)  # ruff: ignore[error-instead-of-exception]
            else:
                logger.warning("primary still unreachable: %s", error)
            try:
                release_holds(self._secondary, self._last_primary_enabled, self._settings, now=now)
            except TechnitiumError as secondary_error:
                # The message already names the cause; a traceback would only add noise.
                logger.error("cannot end holds on the secondary: %s", secondary_error)  # ruff: ignore[error-instead-of-exception]
            return CycleOutcome(ok=False, wait_seconds=self._settings.down_check_interval_seconds)
        if self._outage_since is not None:
            logger.info("primary reachable again after %s", now - self._outage_since)
            self._outage_since = None
        self._last_primary_enabled = dict(result.primary_enabled)
        self._report_outage_leases(result.outage_leases)
        return CycleOutcome(ok=not result.failures, wait_seconds=self._settings.interval_seconds)

    def _report_outage_leases(self, leases: tuple[Lease, ...]) -> None:
        keys = {(lease.hardware_address, lease.address) for lease in leases}
        for lease in leases:
            if (lease.hardware_address, lease.address) not in self._reported_leases:
                logger.warning(
                    "%s holds %s in %s from the secondary, unknown to the primary; the primary "
                    "may hand it out again before %s unless the scope uses ping check",
                    lease.hardware_address,
                    lease.address,
                    lease.scope,
                    lease.lease_expires.isoformat(timespec="seconds"),
                )
        self._reported_leases = keys


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
