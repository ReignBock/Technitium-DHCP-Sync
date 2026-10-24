"""Tests for computing the secondary's desired state and the actions that reach it."""

from datetime import UTC, datetime
from typing import Any

import pytest

from technitium_dhcp_sync.models import Lease, LeaseType, ReservedLease, Scope
from technitium_dhcp_sync.planner import (
    LEASE_COMMENT,
    DeleteScope,
    DisableScope,
    EnableScope,
    ScopeState,
    SecondaryOverrides,
    SetScope,
    desired_scope,
    plan,
)

from .test_models import DOC_SCOPE, MINIMAL_SCOPE

EXPIRES = datetime(2026, 10, 24, 15, 0, tzinfo=UTC)
OVERRIDES = SecondaryOverrides(offer_delay_increment_ms=2000, lease_time_minutes=5)


def scope(base: dict[str, Any] = MINIMAL_SCOPE, **fields: object) -> Scope:
    return Scope.model_validate(base).model_copy(update=fields)


def dynamic(mac: str, address: str, scope_name: str = "Lab", host: str | None = None) -> Lease:
    return Lease(
        scope=scope_name,
        type=LeaseType.DYNAMIC,
        hardware_address=mac,
        address=address,
        host_name=host,
        lease_expires=EXPIRES,
    )


def desired(primary: Scope, leases: tuple[Lease, ...] = ()) -> Scope:
    return desired_scope(primary, leases, OVERRIDES)


def run_plan(
    primary: dict[str, ScopeState],
    secondary: dict[str, ScopeState],
    leases: tuple[Lease, ...] = (),
    held: frozenset[str] = frozenset(),
) -> list[object]:
    return list(
        plan(
            primary,
            secondary,
            leases,
            held=held,
            overrides=OVERRIDES,
        )
    )


# desired_scope


def test_desired_scope_copies_every_field_except_the_overrides() -> None:
    primary = scope(DOC_SCOPE, offer_delay_time=500)

    result = desired(primary)

    assert result.offer_delay_time == 2500
    assert (result.lease_time_days, result.lease_time_hours, result.lease_time_minutes) == (0, 0, 5)
    overridden = {
        "offer_delay_time",
        "lease_time_days",
        "lease_time_hours",
        "lease_time_minutes",
    }
    assert result.model_dump(exclude=overridden) == primary.model_dump(exclude=overridden)


@pytest.mark.parametrize(
    ("minutes", "expected"),
    [(5, (0, 0, 5)), (60, (0, 1, 0)), (90, (0, 1, 30)), (1440, (1, 0, 0)), (1505, (1, 1, 5))],
)
def test_lease_time_is_split_into_days_hours_and_minutes(
    minutes: int, expected: tuple[int, int, int]
) -> None:
    result = desired_scope(scope(), (), SecondaryOverrides(0, minutes))

    assert (result.lease_time_days, result.lease_time_hours, result.lease_time_minutes) == expected


def test_dynamic_leases_become_reservations() -> None:
    result = desired(scope(), (dynamic("00-00-00-00-00-0A", "10.0.0.150", host="laptop"),))

    assert result.reserved_leases == (
        ReservedLease(
            host_name="laptop",
            hardware_address="00-00-00-00-00-0A",
            address="10.0.0.150",
            comments=LEASE_COMMENT,
        ),
    )


def test_primary_reservations_are_kept_alongside_lease_reservations() -> None:
    reserved = ReservedLease(hardware_address="00-00-00-00-00-01", address="10.0.0.101")

    result = desired(
        scope(reserved_leases=(reserved,)), (dynamic("00-00-00-00-00-0A", "10.0.0.150"),)
    )

    assert [r.hardware_address for r in result.reserved_leases] == [
        "00-00-00-00-00-01",
        "00-00-00-00-00-0A",
    ]


def test_a_primary_reservation_wins_over_a_lease_with_the_same_mac() -> None:
    reserved = ReservedLease(
        hardware_address="00-00-00-00-00-01", address="10.0.0.101", comments="printer"
    )

    result = desired(
        scope(reserved_leases=(reserved,)), (dynamic("00-00-00-00-00-01", "10.0.0.150"),)
    )

    assert result.reserved_leases == (reserved,)


def test_a_lease_on_an_address_already_reserved_is_skipped() -> None:
    reserved = ReservedLease(hardware_address="00-00-00-00-00-01", address="10.0.0.150")

    result = desired(
        scope(reserved_leases=(reserved,)), (dynamic("00-00-00-00-00-0A", "10.0.0.150"),)
    )

    assert result.reserved_leases == (reserved,)


def test_reserved_type_and_other_scope_leases_are_ignored() -> None:
    leases = (
        Lease(
            scope="Lab",
            type=LeaseType.RESERVED,
            hardware_address="00-00-00-00-00-01",
            address="10.0.0.101",
            lease_expires=EXPIRES,
        ),
        dynamic("00-00-00-00-00-0B", "192.168.1.50", scope_name="Other"),
    )

    assert desired(scope(), leases).reserved_leases == ()


def test_the_same_inputs_give_an_equal_scope_whatever_the_lease_order() -> None:
    a = dynamic("00-00-00-00-00-0A", "10.0.0.150")
    b = dynamic("00-00-00-00-00-0B", "10.0.0.151")

    assert desired(scope(), (a, b)) == desired(scope(), (b, a))


# plan


def test_a_scope_missing_on_the_secondary_is_created_then_enabled() -> None:
    primary = {"Lab": ScopeState(scope(), enabled=True)}

    assert run_plan(primary, {}) == [SetScope(desired(scope())), EnableScope("Lab")]


def test_a_disabled_primary_scope_is_created_and_left_disabled() -> None:
    primary = {"Lab": ScopeState(scope(), enabled=False)}

    assert run_plan(primary, {}) == [SetScope(desired(scope()))]


def test_a_secondary_already_in_sync_needs_no_action() -> None:
    lease = dynamic("00-00-00-00-00-0A", "10.0.0.150")
    primary = {"Lab": ScopeState(scope(), enabled=True)}
    secondary = {"Lab": ScopeState(desired(scope(), (lease,)), enabled=True)}

    assert run_plan(primary, secondary, (lease,)) == []


def test_a_changed_field_updates_the_scope() -> None:
    primary = {"Lab": ScopeState(scope(router_address="10.0.0.1"), enabled=True)}
    secondary = {"Lab": ScopeState(desired(scope()), enabled=True)}

    assert run_plan(primary, secondary) == [SetScope(desired(scope(router_address="10.0.0.1")))]


def test_a_new_lease_updates_the_scope() -> None:
    lease = dynamic("00-00-00-00-00-0A", "10.0.0.150")
    primary = {"Lab": ScopeState(scope(), enabled=True)}
    secondary = {"Lab": ScopeState(desired(scope()), enabled=True)}

    assert run_plan(primary, secondary, (lease,)) == [SetScope(desired(scope(), (lease,)))]


def test_an_expired_lease_removes_its_reservation() -> None:
    lease = dynamic("00-00-00-00-00-0A", "10.0.0.150")
    primary = {"Lab": ScopeState(scope(), enabled=True)}
    secondary = {"Lab": ScopeState(desired(scope(), (lease,)), enabled=True)}

    assert run_plan(primary, secondary) == [SetScope(desired(scope()))]


@pytest.mark.parametrize(
    ("primary_enabled", "secondary_enabled", "expected"),
    [(True, False, EnableScope("Lab")), (False, True, DisableScope("Lab"))],
)
def test_enabled_drift_is_corrected_without_rewriting_the_scope(
    *, primary_enabled: bool, secondary_enabled: bool, expected: object
) -> None:
    primary = {"Lab": ScopeState(scope(), enabled=primary_enabled)}
    secondary = {"Lab": ScopeState(desired(scope()), enabled=secondary_enabled)}

    assert run_plan(primary, secondary) == [expected]


def test_a_scope_only_on_the_secondary_is_deleted_first() -> None:
    primary = {"Lab": ScopeState(scope(), enabled=True)}
    secondary = {"Old": ScopeState(desired(scope(name="Old")), enabled=True)}

    assert run_plan(primary, secondary) == [
        DeleteScope("Old"),
        SetScope(desired(scope())),
        EnableScope("Lab"),
    ]


def test_an_empty_primary_deletes_everything_on_the_secondary() -> None:
    secondary = {
        "A": ScopeState(desired(scope(name="A")), enabled=True),
        "B": ScopeState(desired(scope(name="B")), enabled=False),
    }

    assert run_plan({}, secondary) == [DeleteScope("A"), DeleteScope("B")]


def test_a_held_scope_is_disabled_even_though_the_primary_has_it_enabled() -> None:
    primary = {"Lab": ScopeState(scope(), enabled=True)}
    secondary = {"Lab": ScopeState(desired(scope()), enabled=True)}

    assert run_plan(primary, secondary, held=frozenset({"Lab"})) == [DisableScope("Lab")]


def test_a_held_scope_stays_disabled_and_is_still_kept_up_to_date() -> None:
    primary = {"Lab": ScopeState(scope(router_address="10.0.0.1"), enabled=True)}
    secondary = {"Lab": ScopeState(desired(scope()), enabled=False)}

    assert run_plan(primary, secondary, held=frozenset({"Lab"})) == [
        SetScope(desired(scope(router_address="10.0.0.1")))
    ]


def test_a_new_held_scope_is_created_but_not_enabled() -> None:
    primary = {"Lab": ScopeState(scope(), enabled=True)}

    assert run_plan(primary, {}, held=frozenset({"Lab"})) == [SetScope(desired(scope()))]
