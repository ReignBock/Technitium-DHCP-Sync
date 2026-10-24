"""Tests for secondary scope holds and for re-enabling scopes while the primary is down."""

from datetime import UTC, datetime, timedelta

from technitium_dhcp_sync.failback import scope_holds, scopes_to_enable
from technitium_dhcp_sync.models import Lease, LeaseType

NOW = datetime(2026, 10, 24, 14, 0, tzinfo=UTC)
MARGIN = timedelta(seconds=60)


def lease(scope: str, expires: datetime, kind: LeaseType = LeaseType.DYNAMIC) -> Lease:
    return Lease(
        scope=scope,
        type=kind,
        hardware_address="00-00-00-00-00-0A",
        address="10.0.0.150",
        lease_expires=expires,
    )


# scope_holds


def test_no_leases_means_no_holds() -> None:
    assert scope_holds([], now=NOW, margin=MARGIN) == {}


def test_an_active_lease_holds_its_scope_until_expiry_plus_margin() -> None:
    expires = NOW + timedelta(minutes=3)

    assert scope_holds([lease("Lab", expires)], now=NOW, margin=MARGIN) == {"Lab": expires + MARGIN}


def test_reserved_leases_hold_their_scope_too() -> None:
    expires = NOW + timedelta(minutes=3)

    holds = scope_holds([lease("Lab", expires, LeaseType.RESERVED)], now=NOW, margin=MARGIN)

    assert holds == {"Lab": expires + MARGIN}


def test_the_hold_lasts_until_the_latest_lease_in_the_scope() -> None:
    early, late = NOW + timedelta(minutes=1), NOW + timedelta(minutes=4)

    holds = scope_holds([lease("Lab", late), lease("Lab", early)], now=NOW, margin=MARGIN)

    assert holds == {"Lab": late + MARGIN}


def test_an_expired_lease_holds_only_within_the_margin() -> None:
    inside = NOW - timedelta(seconds=30)
    outside = NOW - timedelta(seconds=60)

    holds = scope_holds([lease("A", inside), lease("B", outside)], now=NOW, margin=MARGIN)

    assert holds == {"A": inside + MARGIN}


def test_each_scope_gets_its_own_hold() -> None:
    a, b = NOW + timedelta(minutes=1), NOW + timedelta(minutes=2)

    holds = scope_holds([lease("A", a), lease("B", b)], now=NOW, margin=MARGIN)

    assert holds == {"A": a + MARGIN, "B": b + MARGIN}


# scopes_to_enable


def test_a_disabled_scope_the_primary_had_enabled_is_enabled() -> None:
    assert scopes_to_enable({"Lab": False}, {"Lab": True}, holds={}) == ["Lab"]


def test_a_scope_the_primary_had_disabled_stays_disabled_even_if_held() -> None:
    assert scopes_to_enable({"Lab": False}, {"Lab": False}, holds={"Lab": NOW}) == []


def test_an_enabled_scope_needs_nothing() -> None:
    assert scopes_to_enable({"Lab": True}, {"Lab": True}, holds={}) == []


def test_without_a_known_primary_state_held_scopes_are_enabled() -> None:
    secondary = {"Held": False, "Off": False}

    assert scopes_to_enable(secondary, {}, holds={"Held": NOW}) == ["Held"]
