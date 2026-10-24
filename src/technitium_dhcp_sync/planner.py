"""Work out what the secondary should look like and which calls get it there.

Everything here is pure: no I/O, so the sync rules can be tested in isolation.
"""

from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass

from .models import Lease, LeaseType, ReservedLease, Scope

LEASE_COMMENT = "technitium-dhcp-sync: dynamic lease"

_MINUTES_PER_HOUR = 60
_MINUTES_PER_DAY = 24 * _MINUTES_PER_HOUR


@dataclass(frozen=True)
class ScopeState:
    """A scope's configuration plus its enabled flag, which ``scopes/get`` does not return."""

    scope: Scope
    enabled: bool


@dataclass(frozen=True)
class SetScope:
    """Create the scope on the secondary, or overwrite it."""

    scope: Scope


@dataclass(frozen=True)
class EnableScope:
    """Enable the named scope on the secondary."""

    name: str


@dataclass(frozen=True)
class DisableScope:
    """Disable the named scope on the secondary."""

    name: str


@dataclass(frozen=True)
class DeleteScope:
    """Delete the named scope from the secondary."""

    name: str


type Action = SetScope | EnableScope | DisableScope | DeleteScope


def desired_scope(
    primary: Scope,
    leases: Iterable[Lease],
    *,
    offer_delay_increment_ms: int,
    lease_time_minutes: int,
) -> Scope:
    """Build the secondary's copy of a primary scope.

    The copy matches the primary except for three fields. The offer delay grows by
    ``offer_delay_increment_ms``, so the primary answers first while it is up. The lease time is
    ``lease_time_minutes``, so clients return to the primary soon after it recovers. Each dynamic
    lease in the scope becomes a reservation, since the API cannot create dynamic leases.

    Returns:
        The scope the secondary should hold.
    """
    days, rest = divmod(lease_time_minutes, _MINUTES_PER_DAY)
    hours, minutes = divmod(rest, _MINUTES_PER_HOUR)
    return primary.model_copy(
        update={
            "offer_delay_time": primary.offer_delay_time + offer_delay_increment_ms,
            "lease_time_days": days,
            "lease_time_hours": hours,
            "lease_time_minutes": minutes,
            "reserved_leases": _reservations(primary, leases),
        }
    )


def _reservations(primary: Scope, leases: Iterable[Lease]) -> tuple[ReservedLease, ...]:
    reserved = list(primary.reserved_leases)
    taken_macs = {lease.hardware_address for lease in reserved}
    taken_addresses = {lease.address for lease in reserved}
    for lease in leases:
        if (
            lease.scope != primary.name
            or lease.type is not LeaseType.DYNAMIC
            or lease.hardware_address in taken_macs
            or lease.address in taken_addresses
        ):
            continue
        reserved.append(
            ReservedLease(
                host_name=lease.host_name,
                hardware_address=lease.hardware_address,
                address=lease.address,
                comments=LEASE_COMMENT,
            )
        )
        taken_macs.add(lease.hardware_address)
        taken_addresses.add(lease.address)
    return tuple(sorted(reserved, key=lambda lease: lease.hardware_address))


def plan(
    primary: Mapping[str, ScopeState],
    secondary: Mapping[str, ScopeState],
    leases: Iterable[Lease],
    *,
    offer_delay_increment_ms: int,
    lease_time_minutes: int,
) -> Iterator[Action]:
    """List the calls that make the secondary match the primary.

    Deletes come first, so a renamed scope never overlaps its old copy. Then each primary scope
    is written only if it differs, and its enabled flag is corrected only if it differs.

    Yields:
        The actions, in the order to apply them.
    """
    leases = tuple(leases)
    for name in secondary:
        if name not in primary:
            yield DeleteScope(name)
    for name, state in primary.items():
        target = desired_scope(
            state.scope,
            leases,
            offer_delay_increment_ms=offer_delay_increment_ms,
            lease_time_minutes=lease_time_minutes,
        )
        current = secondary.get(name)
        if current is None or current.scope != target:
            yield SetScope(target)
        current_enabled = current is not None and current.enabled
        if state.enabled and not current_enabled:
            yield EnableScope(name)
        elif current_enabled and not state.enabled:
            yield DisableScope(name)
