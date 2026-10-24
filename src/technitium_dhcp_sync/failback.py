"""Decide when the secondary steps aside so its clients return to the primary.

A client renews with the server that issued its lease, so clients that moved to the secondary
during an outage stay there while it answers. Disabling the secondary scope makes their renewals
fail, and they rebind to the primary. Everything here is pure, like the planner.
"""

from collections.abc import Iterable, Mapping
from datetime import datetime, timedelta

from .models import Lease, LeaseType


def scope_holds(
    secondary_leases: Iterable[Lease], *, now: datetime, margin: timedelta
) -> dict[str, datetime]:
    """Find the secondary scopes on hold, and when each hold ends.

    A scope is on hold while any of its secondary leases, dynamic or reserved, expires within
    ``margin`` of ``now`` or later. A disabled scope renews nothing, so the expiries stay fixed
    and the hold ends on its own.

    Returns:
        The end of each hold, keyed by scope name.
    """
    holds: dict[str, datetime] = {}
    for lease in secondary_leases:
        until = lease.lease_expires + margin
        if until > now:
            holds[lease.scope] = max(until, holds.get(lease.scope, until))
    return holds


def scopes_to_enable(
    secondary_enabled: Mapping[str, bool],
    last_primary_enabled: Mapping[str, bool],
    holds: Mapping[str, datetime],
) -> list[str]:
    """List the disabled secondary scopes to enable while the primary is unreachable.

    A scope is enabled if the primary last had it enabled. With no known primary state, which
    happens when the sync starts during an outage, a scope is enabled if it is on hold, since a
    hold is the only reason the sync disables a scope the primary has enabled.

    Returns:
        The scope names, in the secondary's order.
    """
    return [
        name
        for name, enabled in secondary_enabled.items()
        if not enabled and last_primary_enabled.get(name, name in holds)
    ]


def outage_leases(
    secondary_leases: Iterable[Lease], primary_leases: Iterable[Lease], *, now: datetime
) -> list[Lease]:
    """Find active dynamic leases the secondary handed out at addresses the primary does not list.

    Every primary lease reaches the secondary as a reservation, so a dynamic secondary lease
    comes from an outage. Until it expires, the primary can hand the same address to another
    client.

    Returns:
        The leases, in the secondary's order.
    """
    known = {lease.address for lease in primary_leases}
    return [
        lease
        for lease in secondary_leases
        if lease.type is LeaseType.DYNAMIC
        and lease.lease_expires > now
        and lease.address not in known
    ]
