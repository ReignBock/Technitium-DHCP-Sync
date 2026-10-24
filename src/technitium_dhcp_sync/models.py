"""Typed views of Technitium DHCP API payloads.

Each model parses the camelCase JSON that the API returns. ``Scope.to_set_params`` turns a scope
back into the flat parameters that ``/api/dhcp/scopes/set`` accepts.
"""

import re
from collections.abc import Iterable
from enum import StrEnum
from typing import Annotated

from pydantic import AfterValidator, AwareDatetime, BaseModel, BeforeValidator, ConfigDict
from pydantic.alias_generators import to_camel

_MAC_DIGITS = re.compile(r"[0-9A-F]{12}")
_MAC_SEPARATORS = re.compile(r"[-:.]")


def _empty_to_none(value: object) -> object:
    return None if isinstance(value, str) and not value else value


def _none_to_empty(value: object) -> object:
    return () if value is None else value


def _normalize_mac(value: str) -> str:
    digits = _MAC_SEPARATORS.sub("", value).upper()
    if not _MAC_DIGITS.fullmatch(digits):
        msg = f"not a MAC address: {value!r}"
        raise ValueError(msg)
    return "-".join(digits[i : i + 2] for i in range(0, 12, 2))


OptionalText = Annotated[str | None, BeforeValidator(_empty_to_none)]
TextList = Annotated[tuple[str, ...], BeforeValidator(_none_to_empty)]
MacAddress = Annotated[str, AfterValidator(_normalize_mac)]


class ApiModel(BaseModel):
    """Base for API payloads: camelCase aliases, immutable, unknown fields ignored."""

    model_config = ConfigDict(
        alias_generator=to_camel,
        validate_by_name=True,
        validate_by_alias=True,
        frozen=True,
        extra="ignore",
    )


class StaticRoute(ApiModel):
    """A classless static route handed to clients (option 121)."""

    destination: str
    subnet_mask: str
    router: str


class VendorInfo(ApiModel):
    """Vendor-specific information for a vendor class identifier."""

    identifier: str
    information: str


class GenericOption(ApiModel):
    """A raw DHCP option given as a hex string."""

    code: int
    value: str


class Exclusion(ApiModel):
    """An address range the server never assigns dynamically."""

    starting_address: str
    ending_address: str


class ReservedLease(ApiModel):
    """A fixed address for one MAC address."""

    host_name: OptionalText = None
    hardware_address: MacAddress
    address: str
    comments: OptionalText = None


def _sort_reserved(value: tuple[ReservedLease, ...]) -> tuple[ReservedLease, ...]:
    return tuple(sorted(value, key=lambda lease: lease.hardware_address))


class Scope(ApiModel):
    """The full configuration of one DHCP scope, as returned by ``scopes/get``."""

    name: str
    starting_address: str
    ending_address: str
    subnet_mask: str
    lease_time_days: int = 1
    lease_time_hours: int = 0
    lease_time_minutes: int = 0
    offer_delay_time: int = 0
    ping_check_enabled: bool = False
    ping_check_timeout: int = 1000
    ping_check_retries: int = 2
    domain_name: OptionalText = None
    domain_search_list: TextList = ()
    dns_updates: bool = True
    dns_overwrite_for_dynamic_lease: bool = False
    dns_ttl: int = 900
    server_address: OptionalText = None
    server_host_name: OptionalText = None
    boot_file_name: OptionalText = None
    router_address: OptionalText = None
    use_this_dns_server: bool = False
    dns_servers: TextList = ()
    wins_servers: TextList = ()
    ntp_servers: TextList = ()
    ntp_server_domain_names: TextList = ()
    static_routes: Annotated[tuple[StaticRoute, ...], BeforeValidator(_none_to_empty)] = ()
    vendor_info: Annotated[tuple[VendorInfo, ...], BeforeValidator(_none_to_empty)] = ()
    capwap_ac_ip_addresses: TextList = ()
    tftp_server_addresses: TextList = ()
    generic_options: Annotated[tuple[GenericOption, ...], BeforeValidator(_none_to_empty)] = ()
    exclusions: Annotated[tuple[Exclusion, ...], BeforeValidator(_none_to_empty)] = ()
    reserved_leases: Annotated[
        tuple[ReservedLease, ...],
        BeforeValidator(_none_to_empty),
        AfterValidator(_sort_reserved),
    ] = ()
    allow_only_reserved_leases: bool = False
    block_locally_administered_mac_addresses: bool = False
    ignore_client_identifier_option: bool = False

    def to_set_params(self) -> dict[str, str]:
        """Build the ``scopes/set`` parameters that recreate this scope exactly.

        Empty lists and missing text become empty strings, so the server clears those fields.
        A value that contains ``|``, the API's list separator, raises ``ValueError``.

        Returns:
            The parameters keyed by their API names.
        """
        params: dict[str, str] = {}
        for field_name, value in self:
            key = to_camel(field_name)
            match value:
                case bool():
                    params[key] = "true" if value else "false"
                case int():
                    params[key] = str(value)
                case str() | None:
                    params[key] = value or ""
                case _:
                    params[key] = _join_list(key, value)
        return params


def _join_list(key: str, items: tuple[object, ...]) -> str:
    """Join text lists with commas and lists of records with pipes, as the API expects.

    Returns:
        The joined parameter value.
    """
    records = [item for item in items if isinstance(item, ApiModel)]
    if not records:
        return ",".join(str(item) for item in items)
    return "|".join(
        part for record in records for part in _pipe_parts(key, (value for _, value in record))
    )


def _pipe_parts(key: str, values: Iterable[object]) -> list[str]:
    parts = ["" if value is None else str(value) for value in values]
    if any("|" in part for part in parts):
        msg = f"{key} value contains '|', which the API cannot carry: {parts!r}"
        raise ValueError(msg)
    return parts


class ScopeSummary(ApiModel):
    """One entry of ``scopes/list``."""

    name: str
    enabled: bool


class LeaseType(StrEnum):
    """How the server allocated a lease."""

    DYNAMIC = "Dynamic"
    RESERVED = "Reserved"


class Lease(ApiModel):
    """One entry of ``leases/list``."""

    scope: str
    type: LeaseType
    hardware_address: MacAddress
    address: str
    host_name: OptionalText = None
    lease_expires: AwareDatetime
