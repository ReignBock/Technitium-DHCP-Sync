"""End-to-end checks: real servers, a real DHCP client, and the sync run as a process."""

import pytest
from pydantic.alias_generators import to_camel

from technitium_dhcp_sync.models import Scope
from technitium_dhcp_sync.planner import LEASE_COMMENT

from .conftest import CLIENT_MAC, PRIMARY_IP, SECONDARY_IP, Stack, request_lease

pytestmark = pytest.mark.integration

LAB = {
    "name": "Lab",
    "startingAddress": "172.30.0.100",
    "endingAddress": "172.30.0.200",
    "subnetMask": "255.255.255.0",
    "routerAddress": "172.30.0.1",
    "leaseTimeDays": "0",
    "leaseTimeHours": "1",
    "leaseTimeMinutes": "0",
    "offerDelayTime": "100",
    "exclusions": "172.30.0.150|172.30.0.160",
    "dnsServers": "1.1.1.1,9.9.9.9",
}


def create_lab(stack: Stack, **overrides: str) -> None:
    stack.primary.call("scopes/set", **{**LAB, **overrides})
    stack.primary.call("scopes/enable", name="Lab")


def assert_sync_ok(stack: Stack) -> None:
    result = stack.run_sync()
    assert result.returncode == 0, result.stderr


def test_sync_copies_the_scope_with_overrides_and_the_lease(stack: Stack) -> None:
    create_lab(stack)
    lease = request_lease()
    assert lease.server == PRIMARY_IP

    assert_sync_ok(stack)

    primary = stack.primary.scope("Lab")
    secondary = stack.secondary.scope("Lab")
    assert secondary["offerDelayTime"] == 2100
    assert (
        secondary["leaseTimeDays"],
        secondary["leaseTimeHours"],
        secondary["leaseTimeMinutes"],
    ) == (0, 0, 5)
    assert secondary["reservedLeases"] == [
        {
            "hostName": "",
            "hardwareAddress": CLIENT_MAC,
            "address": lease.ip,
            "comments": LEASE_COMMENT,
        }
    ]
    overridden = {
        "offerDelayTime",
        "leaseTimeDays",
        "leaseTimeHours",
        "leaseTimeMinutes",
        "reservedLeases",
    }
    assert {k: v for k, v in secondary.items() if k not in overridden} == {
        k: v for k, v in primary.items() if k not in overridden
    }
    assert {"name": "Lab", "enabled": True}.items() <= next(
        s for s in stack.secondary.call("scopes/list")["scopes"] if s["name"] == "Lab"
    ).items()


def test_a_second_sync_is_a_no_op(stack: Stack) -> None:
    create_lab(stack)
    request_lease()
    assert_sync_ok(stack)

    result = stack.run_sync()

    assert result.returncode == 0, result.stderr
    assert "applied" not in result.stderr


def test_clearing_fields_on_the_primary_clears_them_on_the_secondary(stack: Stack) -> None:
    create_lab(stack)
    assert_sync_ok(stack)

    stack.primary.call("scopes/set", name="Lab", exclusions="", dnsServers="")
    assert_sync_ok(stack)

    secondary = stack.secondary.scope("Lab")
    assert "exclusions" not in secondary
    assert "dnsServers" not in secondary


def test_the_primary_answers_first_while_it_is_up(stack: Stack) -> None:
    create_lab(stack)
    request_lease()
    assert_sync_ok(stack)

    assert request_lease().server == PRIMARY_IP


def test_the_secondary_serves_the_same_address_while_the_primary_is_down(
    stack: Stack,
) -> None:
    create_lab(stack)
    first = request_lease()
    assert_sync_ok(stack)

    stack.primary.stop()
    failover = request_lease()

    assert failover.server == SECONDARY_IP
    assert failover.ip == first.ip
    assert failover.lease_seconds == 300


def test_a_sync_with_the_primary_down_fails_and_changes_nothing(stack: Stack) -> None:
    create_lab(stack)
    request_lease()
    assert_sync_ok(stack)
    before = stack.secondary.scope("Lab")

    stack.primary.stop()
    result = stack.run_sync()

    assert result.returncode == 1
    assert stack.secondary.scope("Lab") == before


def test_a_scope_deleted_on_the_primary_is_deleted_on_the_secondary(stack: Stack) -> None:
    create_lab(stack)
    assert_sync_ok(stack)

    stack.primary.call("scopes/delete", name="Lab")
    assert_sync_ok(stack)

    assert stack.secondary.scope_names() == set()


def test_the_scope_model_knows_every_field_the_server_returns(stack: Stack) -> None:
    create_lab(stack, staticRoutes="10.9.0.0|255.255.0.0|172.30.0.1", domainName="lab.test")

    reply = stack.primary.scope("Lab")

    known = {to_camel(name) for name in Scope.model_fields}
    assert set(reply) <= known
