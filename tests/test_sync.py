"""Tests for one sync cycle against two fake servers."""

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from technitium_dhcp_sync.client import TechnitiumAuthError, TechnitiumError
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


def secondary_lease(
    expires: datetime, scope: str = "Lab", kind: str = "Reserved", address: str = "10.0.0.150"
) -> dict[str, Any]:
    return {
        "scope": scope,
        "type": kind,
        "hardwareAddress": "00-00-00-00-00-0A",
        "address": address,
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


def test_a_primary_with_no_scopes_does_not_empty_the_secondary(
    secondary: FakeTechnitium, caplog: pytest.LogCaptureFixture
) -> None:
    secondary.add_scope(MINIMAL_SCOPE)

    with caplog.at_level(logging.ERROR):
        result = sync(FakeTechnitium(), secondary)

    assert secondary.writes == []
    assert len(result.failures) == 1
    assert "allow_empty_primary" in result.failures[0]
    assert "allow_empty_primary" in caplog.text


def test_allow_empty_primary_lets_the_secondary_be_emptied(secondary: FakeTechnitium) -> None:
    secondary.add_scope(MINIMAL_SCOPE)
    settings = SyncSettings(allow_empty_primary=True)

    with FakeTechnitium().client() as p, secondary.client() as s:
        result = run_cycle(p, s, settings, now=NOW)

    assert secondary.writes == [("delete", "Lab")]
    assert result.failures == ()


def test_two_empty_servers_are_in_sync(secondary: FakeTechnitium) -> None:
    assert sync(FakeTechnitium(), secondary).failures == ()


def test_a_rejected_primary_token_is_not_a_primary_outage(
    primary: FakeTechnitium, secondary: FakeTechnitium
) -> None:
    with (
        primary.client(token="wrong") as p,  # ruff: ignore[hardcoded-password-func-arg]
        secondary.client() as s,
        pytest.raises(TechnitiumAuthError) as error,
    ):
        run_cycle(p, s, SETTINGS, now=NOW)

    assert not isinstance(error.value, PrimaryUnreachableError)
    assert secondary.writes == []


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


def test_the_result_lists_dynamic_secondary_leases_the_primary_does_not_know(
    primary: FakeTechnitium, secondary: FakeTechnitium
) -> None:
    secondary.leases.append(
        secondary_lease(NOW + timedelta(minutes=3), kind="Dynamic", address="10.0.0.120")
    )

    result = sync(primary, secondary)

    assert [lease.address for lease in result.outage_leases] == ["10.0.0.120"]


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


def test_a_rejected_primary_token_leaves_holds_in_place(
    primary: FakeTechnitium, secondary: FakeTechnitium
) -> None:
    secondary.add_scope(MINIMAL_SCOPE, enabled=False)
    secondary.leases.append(secondary_lease(NOW + timedelta(minutes=3)))
    syncer = Syncer(
        primary.client(token="wrong"),  # ruff: ignore[hardcoded-password-func-arg]
        secondary.client(),
        SETTINGS,
        clock=lambda: NOW,
    )

    with pytest.raises(TechnitiumAuthError):
        syncer.cycle()

    assert secondary.writes == []


def test_an_outage_logs_an_error_once_then_warnings_then_recovery(
    primary: FakeTechnitium, secondary: FakeTechnitium, caplog: pytest.LogCaptureFixture
) -> None:
    times = iter([NOW, NOW + timedelta(seconds=10), NOW + timedelta(seconds=95)])
    syncer = Syncer(primary.client(), secondary.client(), SETTINGS, clock=lambda: next(times))
    primary.reachable = False

    with caplog.at_level(logging.INFO):
        syncer.cycle()
        syncer.cycle()
        primary.reachable = True
        syncer.cycle()

    outage = [
        (record.levelname, record.getMessage())
        for record in caplog.records
        if "primary" in record.getMessage() and "reach" in record.getMessage()
    ]
    assert [level for level, _ in outage] == ["ERROR", "WARNING", "INFO"]
    assert "still unreachable" in outage[1][1]
    assert "0:01:35" in outage[2][1]
    assert not any(record.exc_info for record in caplog.records)


def test_each_outage_lease_is_logged_once(
    primary: FakeTechnitium, secondary: FakeTechnitium, caplog: pytest.LogCaptureFixture
) -> None:
    syncer = make_syncer(primary, secondary)
    secondary.leases.append(
        secondary_lease(NOW + timedelta(minutes=3), kind="Dynamic", address="10.0.0.120")
    )

    with caplog.at_level(logging.WARNING):
        syncer.cycle()
        syncer.cycle()
        secondary.leases.append(
            secondary_lease(NOW + timedelta(minutes=3), kind="Dynamic", address="10.0.0.121")
        )
        syncer.cycle()

    warned = [record.getMessage() for record in caplog.records if "unknown" in record.getMessage()]
    assert len(warned) == 2
    assert "10.0.0.120" in warned[0]
    assert "10.0.0.121" in warned[1]
