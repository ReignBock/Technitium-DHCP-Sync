"""End-to-end checks: real servers, a real DHCP client, and the sync run as a process."""

import pytest
from pydantic.alias_generators import to_camel

from technitium_dhcp_sync.models import Scope
from technitium_dhcp_sync.planner import LEASE_COMMENT

from .conftest import (
    CLIENT_MAC,
    PRIMARY_IP,
    SECONDARY_IP,
    BackgroundClient,
    Stack,
    request_lease,
)

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
    "dnsUpdates": "true",
    "dnsOverwriteForDynamicLease": "true",
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
    assert (primary["dnsUpdates"], primary["dnsOverwriteForDynamicLease"]) == (True, True)
    assert (secondary["dnsUpdates"], secondary["dnsOverwriteForDynamicLease"]) == (False, False)
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
        "dnsUpdates",
        "dnsOverwriteForDynamicLease",
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
    stack.primary.call(
        "scopes/set",
        name="Other",
        startingAddress="10.99.0.100",
        endingAddress="10.99.0.200",
        subnetMask="255.255.255.0",
    )
    assert_sync_ok(stack)

    stack.primary.call("scopes/delete", name="Lab")
    assert_sync_ok(stack)

    assert stack.secondary.scope_names() == {"Other"}


def test_a_primary_with_no_scopes_does_not_empty_the_secondary(stack: Stack) -> None:
    create_lab(stack)
    assert_sync_ok(stack)

    stack.primary.call("scopes/delete", name="Lab")
    result = stack.run_sync()

    assert result.returncode == 1
    assert "allow_empty_primary" in result.stderr
    assert stack.secondary.scope_names() == {"Lab"}


def test_a_rejected_primary_token_is_reported_as_such(stack: Stack) -> None:
    create_lab(stack)

    result = stack.run_sync(primary_token="not-a-real-token")  # ruff: ignore[hardcoded-password-func-arg]

    assert result.returncode == 1
    assert "rejected the API token" in result.stderr
    assert stack.secondary.scope_names() == set()


def test_the_scope_model_knows_every_field_the_server_returns(stack: Stack) -> None:
    create_lab(stack, staticRoutes="10.9.0.0|255.255.0.0|172.30.0.1", domainName="lab.test")

    reply = stack.primary.scope("Lab")

    known = {to_camel(name) for name in Scope.model_fields}
    assert set(reply) <= known


def scope_enabled(stack: Stack, name: str) -> bool:
    scopes = stack.secondary.call("scopes/list")["scopes"]
    return next(scope["enabled"] is True for scope in scopes if scope["name"] == name)


def fail_over(stack: Stack, client: BackgroundClient) -> int:
    """Put the client on the secondary during an outage, then bring the primary back.

    Returns:
        The number of client events so far.
    """
    create_lab(stack)
    assert_sync_ok(stack)
    stack.primary.stop()
    client.start()
    assert client.wait_for_event(0).lease.server == SECONDARY_IP
    stack.primary.start()
    return len(client.events())


def test_clients_on_the_secondary_return_to_the_primary_after_a_sync(
    stack: Stack, client: BackgroundClient
) -> None:
    seen = fail_over(stack, client)

    # Without a hold, a renewal goes back to the secondary, which approves it.
    client.renew()
    sticky = client.wait_for_event(seen)
    assert (sticky.event, sticky.lease.server) == ("renew", SECONDARY_IP)

    assert_sync_ok(stack)
    assert not scope_enabled(stack, "Lab")

    client.renew()
    moved = client.wait_for_event(seen + 1)
    assert moved.lease.server == PRIMARY_IP
    assert moved.lease.ip == sticky.lease.ip


def test_a_primary_outage_during_a_hold_enables_the_secondary_at_once(
    stack: Stack, client: BackgroundClient
) -> None:
    fail_over(stack, client)
    assert_sync_ok(stack)
    assert not scope_enabled(stack, "Lab")

    stack.primary.stop()
    result = stack.run_sync()

    assert result.returncode == 1
    assert "enabled Lab on the secondary" in result.stderr
    assert scope_enabled(stack, "Lab")
