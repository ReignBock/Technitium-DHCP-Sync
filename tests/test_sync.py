"""Tests for one sync cycle against two fake servers."""

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from technitium_dhcp_sync.client import TechnitiumError
from technitium_dhcp_sync.config import SyncSettings
from technitium_dhcp_sync.models import Scope
from technitium_dhcp_sync.planner import LEASE_COMMENT, DeleteScope, EnableScope, SetScope
from technitium_dhcp_sync.sync import (
    CycleResult,
    PrimaryUnreachableError,
    Syncer,
    release_holds,
    run_cycle,
)

from .fakes import FakeTechnitium
from .test_models import DOC_SCOPE, MINIMAL_SCOPE

SETTINGS = SyncSettings()
NOW = datetime(2026, 10, 24, 14, 0, tzinfo=UTC)

LEASE = {
    "scope": "Lab",
    "type": "Dynamic",
    "hardwareAddress": "00-00-00-00-00-0A",
    "address": "10.0.0.150",
    "hostName": "laptop",
    "leaseExpires": "2026-10-24T15:00:00Z",
}


@pytest.fixture
def primary() -> FakeTechnitium:
    fake = FakeTechnitium()
    fake.add_scope(MINIMAL_SCOPE, enabled=True)
    fake.leases.append(LEASE)
    return fake


@pytest.fixture
def secondary() -> FakeTechnitium:
    return FakeTechnitium()


def sync(primary: FakeTechnitium, secondary: FakeTechnitium) -> CycleResult:
    with primary.client() as p, secondary.client() as s:
        return run_cycle(p, s, SETTINGS, now=NOW)


def secondary_lease(expires: datetime, scope: str = "Lab") -> dict[str, Any]:
    return {
        "scope": scope,
        "type": "Reserved",
        "hardwareAddress": "00-00-00-00-00-0A",
        "address": "10.0.0.150",
        "leaseExpires": expires.isoformat(),
    }


def test_first_cycle_copies_the_scope_and_its_leases(
    primary: FakeTechnitium, secondary: FakeTechnitium
) -> None:
    sync(primary, secondary)

    copy = Scope.model_validate(secondary.scopes["Lab"])
    assert secondary.enabled == {"Lab": True}
    assert copy.offer_delay_time == 2000
    assert copy.lease_time_minutes == 5
    assert [(r.address, r.comments) for r in copy.reserved_leases] == [
        ("10.0.0.150", LEASE_COMMENT)
    ]


def test_a_second_cycle_writes_nothing(primary: FakeTechnitium, secondary: FakeTechnitium) -> None:
    sync(primary, secondary)
    secondary.writes.clear()

    result = sync(primary, secondary)

    assert secondary.writes == []
    assert result.actions == ()
    assert result.failures == ()


def test_the_result_lists_the_applied_actions(
    primary: FakeTechnitium, secondary: FakeTechnitium
) -> None:
    secondary.add_scope({**MINIMAL_SCOPE, "name": "Old"})

    result = sync(primary, secondary)

    assert [type(action) for action in result.actions] == [
        DeleteScope,
        SetScope,
        EnableScope,
    ]


def test_disabling_a_primary_scope_disables_its_copy(
    primary: FakeTechnitium, secondary: FakeTechnitium
) -> None:
    sync(primary, secondary)
    primary.enabled["Lab"] = False
    secondary.writes.clear()

    sync(primary, secondary)

    assert secondary.writes == [("disable", "Lab")]
    assert secondary.enabled == {"Lab": False}


@pytest.mark.parametrize("broken", ["scopes", "leases"])
def test_an_unreachable_primary_leaves_the_secondary_untouched(
    primary: FakeTechnitium, secondary: FakeTechnitium, broken: str
) -> None:
    secondary.add_scope({**MINIMAL_SCOPE, "name": "Keep"})
    if broken == "scopes":
        primary.reachable = False
    else:
        primary.leases.append({"bad": "lease"})

    with pytest.raises(TechnitiumError):
        sync(primary, secondary)

    assert secondary.writes == []
    assert list(secondary.scopes) == ["Keep"]


def test_an_unreachable_primary_raises_primary_unreachable(
    primary: FakeTechnitium, secondary: FakeTechnitium
) -> None:
    primary.reachable = False

    with pytest.raises(PrimaryUnreachableError):
        sync(primary, secondary)


def test_an_unreachable_secondary_is_not_a_primary_outage(
    primary: FakeTechnitium, secondary: FakeTechnitium
) -> None:
    secondary.reachable = False

    with pytest.raises(TechnitiumError) as error:
        sync(primary, secondary)

    assert not isinstance(error.value, PrimaryUnreachableError)


def test_one_failed_action_does_not_stop_the_others(
    primary: FakeTechnitium, secondary: FakeTechnitium, caplog: pytest.LogCaptureFixture
) -> None:
    primary.add_scope(
        {
            **DOC_SCOPE,
            "reservedLeases": [
                {
                    "hardwareAddress": "00-00-00-00-00-01",
                    "address": "192.168.1.9",
                    "comments": "a|b",
                }
            ],
        },
        enabled=True,
    )

    with caplog.at_level(logging.ERROR):
        result = sync(primary, secondary)

    assert secondary.writes == [("set", "Lab"), ("enable", "Lab")]
    assert "Lab" in secondary.scopes
    assert "Default" not in secondary.scopes
    assert len(result.failures) == 1
    assert "Default" in result.failures[0]
    assert "Default" in caplog.text


# Holds


def test_an_active_secondary_lease_puts_its_scope_on_hold(
    primary: FakeTechnitium, secondary: FakeTechnitium, caplog: pytest.LogCaptureFixture
) -> None:
    sync(primary, secondary)
    expires = NOW + timedelta(minutes=3)
    secondary.leases.append(secondary_lease(expires))
    secondary.writes.clear()

    with caplog.at_level(logging.INFO):
        result = sync(primary, secondary)

    assert secondary.writes == [("disable", "Lab")]
    assert result.holds == {"Lab": expires + timedelta(seconds=60)}
    assert "Lab" in caplog.text
    assert "hold" in caplog.text


def test_a_hold_ends_once_the_last_lease_is_past_the_margin(
    primary: FakeTechnitium, secondary: FakeTechnitium
) -> None:
    sync(primary, secondary)
    secondary.leases.append(secondary_lease(NOW - timedelta(seconds=61)))
    secondary.enabled["Lab"] = False
    secondary.writes.clear()

    result = sync(primary, secondary)

    assert secondary.writes == [("enable", "Lab")]
    assert result.holds == {}


def test_the_result_reports_the_primary_enabled_flags(
    primary: FakeTechnitium, secondary: FakeTechnitium
) -> None:
    primary.add_scope({**MINIMAL_SCOPE, "name": "Off"}, enabled=False)

    assert sync(primary, secondary).primary_enabled == {"Lab": True, "Off": False}


# release_holds


def test_release_holds_enables_scopes_the_primary_had_enabled(
    secondary: FakeTechnitium,
) -> None:
    secondary.add_scope(MINIMAL_SCOPE, enabled=False)
    secondary.add_scope({**MINIMAL_SCOPE, "name": "Off"}, enabled=False)

    with secondary.client() as client:
        enabled = release_holds(client, {"Lab": True, "Off": False}, SETTINGS, now=NOW)

    assert enabled == ["Lab"]
    assert secondary.writes == [("enable", "Lab")]


def test_release_holds_falls_back_to_holds_without_a_known_primary_state(
    secondary: FakeTechnitium,
) -> None:
    secondary.add_scope(MINIMAL_SCOPE, enabled=False)
    secondary.add_scope({**MINIMAL_SCOPE, "name": "Off"}, enabled=False)
    secondary.leases.append(secondary_lease(NOW + timedelta(minutes=2)))

    with secondary.client() as client:
        assert release_holds(client, {}, SETTINGS, now=NOW) == ["Lab"]


# Syncer


def make_syncer(primary: FakeTechnitium, secondary: FakeTechnitium) -> Syncer:
    return Syncer(primary.client(), secondary.client(), SETTINGS, clock=lambda: NOW)


def test_a_successful_cycle_waits_the_normal_interval(
    primary: FakeTechnitium, secondary: FakeTechnitium
) -> None:
    outcome = make_syncer(primary, secondary).cycle()

    assert outcome.ok
    assert outcome.wait_seconds == SETTINGS.interval_seconds


def test_a_failed_action_is_not_ok_but_waits_the_normal_interval(
    primary: FakeTechnitium, secondary: FakeTechnitium
) -> None:
    primary.add_scope({
        **MINIMAL_SCOPE,
        "name": "Bad",
        "reservedLeases": [
            {"hardwareAddress": "00-00-00-00-00-01", "address": "10.0.0.9", "comments": "a|b"}
        ],
    })

    outcome = make_syncer(primary, secondary).cycle()

    assert not outcome.ok
    assert outcome.wait_seconds == SETTINGS.interval_seconds


def test_a_primary_outage_ends_holds_and_checks_again_sooner(
    primary: FakeTechnitium, secondary: FakeTechnitium, caplog: pytest.LogCaptureFixture
) -> None:
    syncer = make_syncer(primary, secondary)
    secondary.leases.append(secondary_lease(NOW + timedelta(minutes=3)))
    syncer.cycle()
    assert secondary.enabled == {"Lab": False}

    primary.reachable = False
    with caplog.at_level(logging.WARNING):
        outcome = syncer.cycle()

    assert not outcome.ok
    assert outcome.wait_seconds == SETTINGS.down_check_interval_seconds
    assert secondary.enabled == {"Lab": True}
    assert "primary unreachable" in caplog.text


def test_an_outage_with_the_secondary_also_down_still_checks_sooner(
    primary: FakeTechnitium, secondary: FakeTechnitium, caplog: pytest.LogCaptureFixture
) -> None:
    primary.reachable = False
    secondary.reachable = False

    with caplog.at_level(logging.ERROR):
        outcome = make_syncer(primary, secondary).cycle()

    assert outcome.wait_seconds == SETTINGS.down_check_interval_seconds
    assert "cannot end holds" in caplog.text
