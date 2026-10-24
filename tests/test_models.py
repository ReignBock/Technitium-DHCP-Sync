"""Tests for parsing Technitium API payloads and building scopes/set parameters."""

from typing import Any

import pytest
from pydantic import ValidationError

from technitium_dhcp_sync.models import (
    Exclusion,
    GenericOption,
    Lease,
    LeaseType,
    ReservedLease,
    Scope,
    ScopeSummary,
    StaticRoute,
    VendorInfo,
)

# The "Get DHCP Scope" response from the Technitium API docs.
DOC_SCOPE: dict[str, Any] = {
    "name": "Default",
    "startingAddress": "192.168.1.1",
    "endingAddress": "192.168.1.254",
    "subnetMask": "255.255.255.0",
    "leaseTimeDays": 7,
    "leaseTimeHours": 0,
    "leaseTimeMinutes": 0,
    "offerDelayTime": 0,
    "pingCheckEnabled": False,
    "pingCheckTimeout": 1000,
    "pingCheckRetries": 2,
    "domainName": "local",
    "domainSearchList": ["home.arpa", "lan"],
    "dnsUpdates": True,
    "dnsOverwriteForDynamicLease": False,
    "dnsTtl": 900,
    "serverAddress": "192.168.1.1",
    "serverHostName": "tftp-server-1",
    "bootFileName": "boot.bin",
    "routerAddress": "192.168.1.1",
    "useThisDnsServer": False,
    "dnsServers": ["192.168.1.5"],
    "winsServers": ["192.168.1.5"],
    "ntpServers": ["192.168.1.5"],
    "ntpServerDomainNames": ["pool.ntp.org"],
    "staticRoutes": [
        {"destination": "172.16.0.0", "subnetMask": "255.255.255.0", "router": "192.168.1.2"}
    ],
    "vendorInfo": [
        {
            "identifier": 'substring(vendor-class-identifier,0,9)=="PXEClient"',
            "information": "06:01:03",
        }
    ],
    "capwapAcIpAddresses": ["192.168.1.2"],
    "tftpServerAddresses": ["192.168.1.5", "192.168.1.6"],
    "genericOptions": [{"code": 150, "value": "C0:A8:01:01"}],
    "exclusions": [{"startingAddress": "192.168.1.1", "endingAddress": "192.168.1.10"}],
    "reservedLeases": [
        {
            "hostName": None,
            "hardwareAddress": "00-00-00-00-00-00",
            "address": "192.168.1.10",
            "comments": "comments",
        }
    ],
    "allowOnlyReservedLeases": False,
    "blockLocallyAdministeredMacAddresses": True,
    "ignoreClientIdentifierOption": True,
}

MINIMAL_SCOPE: dict[str, Any] = {
    "name": "Lab",
    "startingAddress": "10.0.0.100",
    "endingAddress": "10.0.0.200",
    "subnetMask": "255.255.255.0",
}


def test_scope_parses_the_documented_response() -> None:
    scope = Scope.model_validate(DOC_SCOPE)

    assert scope.name == "Default"
    assert scope.lease_time_days == 7
    assert scope.domain_search_list == ("home.arpa", "lan")
    assert scope.static_routes == (
        StaticRoute(destination="172.16.0.0", subnet_mask="255.255.255.0", router="192.168.1.2"),
    )
    assert scope.vendor_info == (
        VendorInfo(
            identifier='substring(vendor-class-identifier,0,9)=="PXEClient"',
            information="06:01:03",
        ),
    )
    assert scope.generic_options == (GenericOption(code=150, value="C0:A8:01:01"),)
    assert scope.exclusions == (
        Exclusion(starting_address="192.168.1.1", ending_address="192.168.1.10"),
    )
    assert scope.reserved_leases == (
        ReservedLease(
            host_name=None,
            hardware_address="00-00-00-00-00-00",
            address="192.168.1.10",
            comments="comments",
        ),
    )


def test_scope_fills_missing_fields_with_defaults() -> None:
    scope = Scope.model_validate(MINIMAL_SCOPE)

    assert scope.lease_time_days == 1
    assert scope.lease_time_hours == 0
    assert scope.lease_time_minutes == 0
    assert scope.offer_delay_time == 0
    assert scope.domain_name is None
    assert scope.dns_servers == ()
    assert scope.reserved_leases == ()


def test_scope_ignores_fields_it_does_not_know() -> None:
    scope = Scope.model_validate({**MINIMAL_SCOPE, "someFutureField": 1})

    assert scope == Scope.model_validate(MINIMAL_SCOPE)


def test_empty_strings_and_nulls_read_as_missing() -> None:
    scope = Scope.model_validate({
        **MINIMAL_SCOPE,
        "domainName": "",
        "serverHostName": None,
        "dnsServers": None,
    })

    assert scope.domain_name is None
    assert scope.server_host_name is None
    assert scope.dns_servers == ()


def test_scope_rejects_a_wrong_type() -> None:
    with pytest.raises(ValidationError):
        Scope.model_validate({**MINIMAL_SCOPE, "leaseTimeDays": "seven"})


def test_scope_is_immutable() -> None:
    scope = Scope.model_validate(MINIMAL_SCOPE)

    with pytest.raises(ValidationError):
        scope.name = "Other"  # type: ignore[misc]


@pytest.mark.parametrize(
    "mac", ["aa:bb:cc:dd:ee:ff", "AA-BB-CC-DD-EE-FF", "aabbccddeeff", "aa-bb-cc-dd-ee-ff"]
)
def test_hardware_address_is_normalized(mac: str) -> None:
    lease = ReservedLease(hardware_address=mac, address="10.0.0.5")

    assert lease.hardware_address == "AA-BB-CC-DD-EE-FF"


@pytest.mark.parametrize("mac", ["aa:bb:cc", "zz:bb:cc:dd:ee:ff", ""])
def test_hardware_address_rejects_invalid_values(mac: str) -> None:
    with pytest.raises(ValidationError, match="MAC address"):
        ReservedLease(hardware_address=mac, address="10.0.0.5")


def test_reserved_leases_are_sorted_by_hardware_address() -> None:
    scope = Scope.model_validate({
        **MINIMAL_SCOPE,
        "reservedLeases": [
            {"hardwareAddress": "00-00-00-00-00-02", "address": "10.0.0.2"},
            {"hardwareAddress": "00-00-00-00-00-01", "address": "10.0.0.1"},
        ],
    })

    assert [r.hardware_address for r in scope.reserved_leases] == [
        "00-00-00-00-00-01",
        "00-00-00-00-00-02",
    ]


def test_to_set_params_covers_every_field() -> None:
    params = Scope.model_validate(DOC_SCOPE).to_set_params()

    assert params == {
        "name": "Default",
        "startingAddress": "192.168.1.1",
        "endingAddress": "192.168.1.254",
        "subnetMask": "255.255.255.0",
        "leaseTimeDays": "7",
        "leaseTimeHours": "0",
        "leaseTimeMinutes": "0",
        "offerDelayTime": "0",
        "pingCheckEnabled": "false",
        "pingCheckTimeout": "1000",
        "pingCheckRetries": "2",
        "domainName": "local",
        "domainSearchList": "home.arpa,lan",
        "dnsUpdates": "true",
        "dnsOverwriteForDynamicLease": "false",
        "dnsTtl": "900",
        "serverAddress": "192.168.1.1",
        "serverHostName": "tftp-server-1",
        "bootFileName": "boot.bin",
        "routerAddress": "192.168.1.1",
        "useThisDnsServer": "false",
        "dnsServers": "192.168.1.5",
        "winsServers": "192.168.1.5",
        "ntpServers": "192.168.1.5",
        "ntpServerDomainNames": "pool.ntp.org",
        "staticRoutes": "172.16.0.0|255.255.255.0|192.168.1.2",
        "vendorInfo": 'substring(vendor-class-identifier,0,9)=="PXEClient"|06:01:03',
        "capwapAcIpAddresses": "192.168.1.2",
        "tftpServerAddresses": "192.168.1.5,192.168.1.6",
        "genericOptions": "150|C0:A8:01:01",
        "exclusions": "192.168.1.1|192.168.1.10",
        "reservedLeases": "|00-00-00-00-00-00|192.168.1.10|comments",
        "allowOnlyReservedLeases": "false",
        "blockLocallyAdministeredMacAddresses": "true",
        "ignoreClientIdentifierOption": "true",
    }


def test_to_set_params_sends_empty_values_to_clear_fields() -> None:
    params = Scope.model_validate(MINIMAL_SCOPE).to_set_params()

    cleared = ("domainName", "dnsServers", "staticRoutes", "reservedLeases")
    assert {key: params[key] for key in cleared} == dict.fromkeys(cleared, "")


def test_to_set_params_joins_several_entries_with_pipes() -> None:
    scope = Scope.model_validate({
        **MINIMAL_SCOPE,
        "exclusions": [
            {"startingAddress": "10.0.0.100", "endingAddress": "10.0.0.110"},
            {"startingAddress": "10.0.0.150", "endingAddress": "10.0.0.160"},
        ],
        "reservedLeases": [
            {"hostName": "a", "hardwareAddress": "00-00-00-00-00-01", "address": "10.0.0.1"},
            {
                "hostName": "b",
                "hardwareAddress": "00-00-00-00-00-02",
                "address": "10.0.0.2",
                "comments": "note",
            },
        ],
    })

    params = scope.to_set_params()

    assert params["exclusions"] == "10.0.0.100|10.0.0.110|10.0.0.150|10.0.0.160"
    assert (
        params["reservedLeases"]
        == "a|00-00-00-00-00-01|10.0.0.1||b|00-00-00-00-00-02|10.0.0.2|note"
    )


def test_to_set_params_rejects_a_pipe_inside_a_value() -> None:
    scope = Scope.model_validate({
        **MINIMAL_SCOPE,
        "reservedLeases": [
            {"hardwareAddress": "00-00-00-00-00-01", "address": "10.0.0.1", "comments": "a|b"}
        ],
    })

    with pytest.raises(ValueError, match=r"reservedLeases.*'\|'"):
        scope.to_set_params()


def test_scope_summary_parses_a_list_entry() -> None:
    summary = ScopeSummary.model_validate({
        "name": "Default",
        "enabled": True,
        "startingAddress": "192.168.1.1",
        "endingAddress": "192.168.1.254",
        "subnetMask": "255.255.255.0",
        "networkAddress": "192.168.1.0",
        "broadcastAddress": "192.168.1.255",
    })

    assert summary == ScopeSummary(name="Default", enabled=True)


def test_lease_parses_a_list_entry() -> None:
    lease = Lease.model_validate({
        "scope": "Default",
        "type": "Dynamic",
        "hardwareAddress": "00-00-00-00-00-0a",
        "clientIdentifier": "1-00000000000a",
        "address": "192.168.1.13",
        "hostName": None,
        "leaseObtained": "06/15/2020 16:41:46",
        "leaseExpires": "09/25/2020 12:39:54",
    })

    assert lease == Lease(
        scope="Default",
        type=LeaseType.DYNAMIC,
        hardware_address="00-00-00-00-00-0A",
        address="192.168.1.13",
        host_name=None,
    )
