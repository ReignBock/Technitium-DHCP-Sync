"""Tests for the Technitium HTTP API client."""

from datetime import UTC, datetime

import httpx
import pytest

from technitium_dhcp_sync.client import TechnitiumAuthError, TechnitiumClient, TechnitiumError
from technitium_dhcp_sync.config import ServerSettings
from technitium_dhcp_sync.models import Lease, LeaseType, ReservedLease, Scope, ScopeSummary

from .fakes import FakeTechnitium
from .test_models import DOC_SCOPE, MINIMAL_SCOPE


@pytest.fixture
def server() -> FakeTechnitium:
    fake = FakeTechnitium()
    fake.add_scope(DOC_SCOPE, enabled=True)
    fake.leases.append({
        "scope": "Default",
        "type": "Dynamic",
        "hardwareAddress": "00-00-00-00-00-0A",
        "clientIdentifier": "1-00000000000A",
        "address": "192.168.1.13",
        "hostName": "laptop.local",
        "leaseObtained": "2026-10-24T13:13:02Z",
        "leaseExpires": "2026-10-24T14:13:02Z",
    })
    return fake


def test_list_scopes(server: FakeTechnitium) -> None:
    with server.client() as client:
        assert client.list_scopes() == [ScopeSummary(name="Default", enabled=True)]


def test_get_scope(server: FakeTechnitium) -> None:
    with server.client() as client:
        assert client.get_scope("Default") == Scope.model_validate(DOC_SCOPE)


def test_list_leases(server: FakeTechnitium) -> None:
    with server.client() as client:
        assert client.list_leases() == [
            Lease(
                scope="Default",
                type=LeaseType.DYNAMIC,
                hardware_address="00-00-00-00-00-0A",
                address="192.168.1.13",
                host_name="laptop.local",
                lease_expires=datetime(2026, 10, 24, 14, 13, 2, tzinfo=UTC),
            )
        ]


def test_set_scope_round_trips_every_field(server: FakeTechnitium) -> None:
    scope = Scope.model_validate(DOC_SCOPE).model_copy(update={"name": "Copy"})

    with server.client() as client:
        client.set_scope(scope)
        assert client.get_scope("Copy") == scope


def test_set_scope_replaces_reserved_leases(server: FakeTechnitium) -> None:
    reservation = ReservedLease(hardware_address="00-00-00-00-00-0B", address="192.168.1.20")
    scope = Scope.model_validate(DOC_SCOPE).model_copy(update={"reserved_leases": (reservation,)})

    with server.client() as client:
        client.set_scope(scope)
        assert client.get_scope("Default").reserved_leases == (reservation,)


def test_enable_disable_and_delete_scope(server: FakeTechnitium) -> None:
    with server.client() as client:
        client.set_scope(Scope.model_validate(MINIMAL_SCOPE))
        client.enable_scope("Lab")
        assert ScopeSummary(name="Lab", enabled=True) in client.list_scopes()
        client.disable_scope("Lab")
        assert ScopeSummary(name="Lab", enabled=False) in client.list_scopes()
        client.delete_scope("Lab")
        assert [s.name for s in client.list_scopes()] == ["Default"]

    assert server.writes == [
        ("set", "Lab"),
        ("enable", "Lab"),
        ("disable", "Lab"),
        ("delete", "Lab"),
    ]


def test_error_status_raises_with_the_server_message(server: FakeTechnitium) -> None:
    with server.client() as client, pytest.raises(TechnitiumError, match="Missing"):
        client.get_scope("Missing")


def test_rejected_token_raises() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "invalid-token", "errorMessage": "bad"})

    with (
        TechnitiumClient("http://x", "t", transport=httpx.MockTransport(handler)) as client,
        pytest.raises(TechnitiumAuthError, match="rejected the API token"),
    ):
        client.list_scopes()


def test_a_rejected_token_is_a_technitium_error() -> None:
    assert issubclass(TechnitiumAuthError, TechnitiumError)


def test_unreachable_server_raises(server: FakeTechnitium) -> None:
    server.reachable = False

    with server.client() as client, pytest.raises(TechnitiumError, match="fake server is down"):
        client.list_scopes()


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(500, text="boom"),
        httpx.Response(200, text="not json"),
        httpx.Response(200, json=["not", "an", "object"]),
        httpx.Response(200, json={"status": "ok"}),
    ],
    ids=["http-error", "not-json", "not-an-object", "no-response"],
)
def test_malformed_replies_raise(response: httpx.Response) -> None:
    with (
        TechnitiumClient(
            "http://x", "t", transport=httpx.MockTransport(lambda _request: response)
        ) as client,
        pytest.raises(TechnitiumError),
    ):
        client.list_scopes()


def test_reply_that_does_not_match_the_model_raises() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "ok", "response": {"scopes": [{"x": 1}]}})

    with (
        TechnitiumClient("http://x", "t", transport=httpx.MockTransport(handler)) as client,
        pytest.raises(TechnitiumError, match="unexpected reply"),
    ):
        client.list_scopes()


def test_requests_carry_the_token_and_use_the_base_url() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"status": "ok", "response": {"scopes": []}})

    with TechnitiumClient(
        "https://dns.example.com:53443/", "abc", transport=httpx.MockTransport(handler)
    ) as client:
        client.list_scopes()

    assert str(seen[0].url) == "https://dns.example.com:53443/api/dhcp/scopes/list"
    assert seen[0].headers["Authorization"] == "Bearer abc"


def test_from_settings_applies_url_token_and_timeout() -> None:
    settings = ServerSettings.model_validate(
        {"url": "http://dns.test:5380", "token_env": "T", "timeout_seconds": 3},
        context={"environ": {"T": "secret"}},
    )
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"status": "ok", "response": {"leases": []}})

    with TechnitiumClient.from_settings(settings, httpx.MockTransport(handler)) as client:
        client.list_leases()

    assert client.base_url == "http://dns.test:5380/"
    assert seen[0].headers["Authorization"] == "Bearer secret"
    assert seen[0].extensions["timeout"]["read"] == 3
