"""An in-memory Technitium DHCP API for tests, served through ``httpx.MockTransport``."""

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qsl

import httpx

from technitium_dhcp_sync.client import TechnitiumClient

if TYPE_CHECKING:
    from collections.abc import Callable

TOKEN = "test-token"  # ruff: ignore[hardcoded-password-string]

# Fields of the records that scopes/set packs into one "|"-separated value.
_RECORD_FIELDS = {
    "staticRoutes": ("destination", "subnetMask", "router"),
    "vendorInfo": ("identifier", "information"),
    "genericOptions": ("code", "value"),
    "exclusions": ("startingAddress", "endingAddress"),
    "reservedLeases": ("hostName", "hardwareAddress", "address", "comments"),
}
_TEXT_LISTS = {
    "domainSearchList",
    "dnsServers",
    "winsServers",
    "ntpServers",
    "ntpServerDomainNames",
    "capwapAcIpAddresses",
    "tftpServerAddresses",
}
_REQUIRED_FOR_NEW_SCOPE = ("startingAddress", "endingAddress", "subnetMask")


def _parse_value(key: str, value: str) -> object:
    if key in _RECORD_FIELDS:
        names = _RECORD_FIELDS[key]
        parts = value.split("|") if value else []
        records: list[dict[str, object]] = [
            dict(zip(names, parts[i : i + len(names)], strict=True))
            for i in range(0, len(parts), len(names))
        ]
        for record in records:
            if "code" in record:
                record["code"] = int(str(record["code"]))
        return records
    if key in _TEXT_LISTS:
        return value.split(",") if value else []
    if value in {"true", "false"}:
        return value == "true"
    if value.isdigit():
        return int(value)
    return value or None


@dataclass
class FakeTechnitium:
    """State of one fake server. ``writes`` records each modifying call in order."""

    scopes: dict[str, dict[str, Any]] = field(default_factory=dict)
    enabled: dict[str, bool] = field(default_factory=dict)
    leases: list[dict[str, Any]] = field(default_factory=list)
    writes: list[tuple[str, str]] = field(default_factory=list)
    reachable: bool = True

    def add_scope(self, scope: dict[str, Any], *, enabled: bool = True) -> None:
        """Store a scope in ``scopes/get`` form."""
        self.scopes[scope["name"]] = dict(scope)
        self.enabled[scope["name"]] = enabled

    def client(self, token: str = TOKEN) -> TechnitiumClient:
        """Return a client wired to this fake, sending ``token``."""
        return TechnitiumClient(
            "http://fake.test:5380", token, transport=httpx.MockTransport(self.handle)
        )

    def handle(self, request: httpx.Request) -> httpx.Response:
        """Answer one API request.

        Returns:
            The reply a real server would send.

        Raises:
            httpx.ConnectError: The fake is set as unreachable.
        """
        if not self.reachable:
            msg = "fake server is down"
            raise httpx.ConnectError(msg, request=request)
        if request.headers.get("Authorization") != f"Bearer {TOKEN}":
            return httpx.Response(200, json={"status": "invalid-token", "errorMessage": "bad"})
        params = dict(request.url.params)
        params.update(parse_qsl(request.content.decode(), keep_blank_values=True))
        route: Callable[[dict[str, str]], object] | None = getattr(
            self, "_" + request.url.path.removeprefix("/api/dhcp/").replace("/", "_"), None
        )
        if route is None:
            return httpx.Response(404)
        try:
            response = route(params)
        except KeyError as error:
            return httpx.Response(200, json={"status": "error", "errorMessage": str(error)})
        return httpx.Response(200, json={"status": "ok", "response": response or {}})

    def _scopes_list(self, _params: dict[str, str]) -> object:
        return {
            "scopes": [
                {"name": name, "enabled": self.enabled[name], **_summary(scope)}
                for name, scope in self.scopes.items()
            ]
        }

    def _scopes_get(self, params: dict[str, str]) -> object:
        return self.scopes[params["name"]]

    def _scopes_set(self, params: dict[str, str]) -> object:
        name = params.pop("name")
        self.writes.append(("set", name))
        if name not in self.scopes:
            missing = [key for key in _REQUIRED_FOR_NEW_SCOPE if key not in params]
            if missing:
                msg = f"new scope needs {missing}"
                raise KeyError(msg)
            self.scopes[name] = {"name": name}
            self.enabled[name] = False
        self.scopes[name].update({key: _parse_value(key, v) for key, v in params.items()})
        return None

    def _scopes_enable(self, params: dict[str, str]) -> object:
        return self._set_enabled(params["name"], enabled=True)

    def _scopes_disable(self, params: dict[str, str]) -> object:
        return self._set_enabled(params["name"], enabled=False)

    def _set_enabled(self, name: str, *, enabled: bool) -> object:
        self.writes.append(("enable" if enabled else "disable", name))
        if name not in self.scopes:
            raise KeyError(name)
        self.enabled[name] = enabled
        return None

    def _scopes_delete(self, params: dict[str, str]) -> object:
        self.writes.append(("delete", params["name"]))
        del self.scopes[params["name"]]
        del self.enabled[params["name"]]
        return None

    def _leases_list(self, _params: dict[str, str]) -> object:
        return {"leases": self.leases}


def _summary(scope: dict[str, Any]) -> dict[str, Any]:
    return {key: scope[key] for key in _REQUIRED_FOR_NEW_SCOPE}
