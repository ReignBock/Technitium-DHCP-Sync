"""Tests for one sync cycle against two fake servers."""

import logging

import pytest

from technitium_dhcp_sync.client import TechnitiumError
from technitium_dhcp_sync.config import SyncSettings
from technitium_dhcp_sync.models import Scope
from technitium_dhcp_sync.planner import LEASE_COMMENT, DeleteScope, EnableScope, SetScope
from technitium_dhcp_sync.sync import CycleResult, run_cycle

from .fakes import FakeTechnitium
from .test_models import DOC_SCOPE, MINIMAL_SCOPE

SETTINGS = SyncSettings()

LEASE = {
    "scope": "Lab",
    "type": "Dynamic",
    "hardwareAddress": "00-00-00-00-00-0A",
    "address": "10.0.0.150",
    "hostName": "laptop",
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
        return run_cycle(p, s, SETTINGS)


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
    assert result == CycleResult(actions=(), failures=())


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


def test_an_unreachable_secondary_raises(
    primary: FakeTechnitium, secondary: FakeTechnitium
) -> None:
    secondary.reachable = False

    with pytest.raises(TechnitiumError):
        sync(primary, secondary)


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
