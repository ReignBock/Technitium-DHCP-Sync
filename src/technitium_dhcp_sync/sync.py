"""Run one sync cycle: read both servers, plan, and apply the plan to the secondary."""

import logging
from dataclasses import dataclass

from .client import TechnitiumClient, TechnitiumError
from .config import SyncSettings
from .planner import Action, DisableScope, EnableScope, ScopeState, SetScope, plan

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CycleResult:
    """What one cycle applied, and a message for each action that failed."""

    actions: tuple[Action, ...]
    failures: tuple[str, ...]


def read_state(client: TechnitiumClient) -> dict[str, ScopeState]:
    """Read every scope on a server with its enabled flag.

    Returns:
        The scopes keyed by name.
    """
    return {
        summary.name: ScopeState(client.get_scope(summary.name), enabled=summary.enabled)
        for summary in client.list_scopes()
    }


def run_cycle(
    primary: TechnitiumClient, secondary: TechnitiumClient, settings: SyncSettings
) -> CycleResult:
    """Make the secondary match the primary.

    All reads happen before any write. If a read fails, ``TechnitiumError`` propagates and the
    secondary stays as it was, so a primary outage never removes the failover copy. A failed
    write is logged and skips the remaining actions for that scope only.

    Returns:
        The actions applied and the failures seen.
    """
    primary_state = read_state(primary)
    leases = primary.list_leases()
    secondary_state = read_state(secondary)

    applied: list[Action] = []
    failures: list[str] = []
    failed_scopes: set[str] = set()
    for action in plan(
        primary_state,
        secondary_state,
        leases,
        offer_delay_increment_ms=settings.offer_delay_increment_ms,
        lease_time_minutes=settings.secondary_lease_time_minutes,
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
    return CycleResult(actions=tuple(applied), failures=tuple(failures))


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
