"""A small client for the Technitium DHCP HTTP API."""

from types import TracebackType
from typing import Self

import httpx
from pydantic import TypeAdapter, ValidationError

from .config import ServerSettings
from .models import Lease, Scope, ScopeSummary

_SCOPE_LIST = TypeAdapter(list[ScopeSummary])
_LEASE_LIST = TypeAdapter(list[Lease])


class TechnitiumError(Exception):
    """A call failed: the server was unreachable, refused the call, or replied in a bad shape."""


class TechnitiumClient:
    """Calls one Technitium server's DHCP API.

    Reads use GET. Writes use POST with form data, so large reservation lists do not hit URL
    length limits. Every failure raises ``TechnitiumError``.
    """

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        timeout: float = 10,
        verify_tls: bool = True,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        """Create a client for the server at ``base_url`` that authenticates with ``token``."""
        self._http = httpx.Client(
            base_url=base_url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout,
            verify=verify_tls,
            transport=transport,
        )

    @classmethod
    def from_settings(
        cls, settings: ServerSettings, transport: httpx.BaseTransport | None = None
    ) -> Self:
        """Create a client from one server's settings.

        Returns:
            A client for that server.
        """
        return cls(
            str(settings.url),
            settings.token.get_secret_value(),
            timeout=settings.timeout_seconds,
            verify_tls=settings.verify_tls,
            transport=transport,
        )

    @property
    def base_url(self) -> str:
        """The server's base URL, for log messages."""
        return str(self._http.base_url)

    def __enter__(self) -> Self:
        """Return the client itself.

        Returns:
            This client.
        """
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close the underlying HTTP connections."""
        self._http.close()

    def list_scopes(self) -> list[ScopeSummary]:
        """List every scope with its enabled flag.

        Returns:
            One summary per scope.
        """
        body = self._call("GET", "scopes/list")
        return self._parse(_SCOPE_LIST, body.get("scopes"), "scopes/list")

    def get_scope(self, name: str) -> Scope:
        """Fetch the full configuration of one scope.

        Returns:
            The scope.
        """
        body = self._call("GET", "scopes/get", {"name": name})
        return self._parse(TypeAdapter(Scope), body, "scopes/get")

    def list_leases(self) -> list[Lease]:
        """List every lease, dynamic and reserved, across all scopes.

        Returns:
            One entry per lease.
        """
        body = self._call("GET", "leases/list")
        return self._parse(_LEASE_LIST, body.get("leases"), "leases/list")

    def set_scope(self, scope: Scope) -> None:
        """Create the scope, or overwrite every field of an existing scope with that name."""
        self._call("POST", "scopes/set", scope.to_set_params())

    def enable_scope(self, name: str) -> None:
        """Let the scope hand out leases."""
        self._call("POST", "scopes/enable", {"name": name})

    def disable_scope(self, name: str) -> None:
        """Stop the scope from handing out leases."""
        self._call("POST", "scopes/disable", {"name": name})

    def delete_scope(self, name: str) -> None:
        """Delete the scope permanently."""
        self._call("POST", "scopes/delete", {"name": name})

    def _call(
        self, method: str, endpoint: str, params: dict[str, str] | None = None
    ) -> dict[str, object]:
        where = f"{self.base_url} {endpoint}"
        payload = self._send(method, f"/api/dhcp/{endpoint}", params, where)
        if not isinstance(payload, dict):
            msg = f"{where}: reply is not a JSON object"
            raise TechnitiumError(msg)
        status = payload.get("status")
        if status != "ok":
            msg = f"{where}: {status}: {payload.get('errorMessage')}"
            raise TechnitiumError(msg)
        body = payload.get("response")
        if not isinstance(body, dict):
            msg = f"{where}: reply has no response object"
            raise TechnitiumError(msg)
        return body

    def _send(self, method: str, path: str, params: dict[str, str] | None, where: str) -> object:
        try:
            if method == "GET":
                response = self._http.get(path, params=params)
            else:
                response = self._http.post(path, data=params)
            response.raise_for_status()
            return response.json()
        except httpx.HTTPError as error:
            msg = f"{where}: {error}"
            raise TechnitiumError(msg) from error
        except ValueError as error:
            msg = f"{where}: reply is not JSON"
            raise TechnitiumError(msg) from error

    @staticmethod
    def _parse[T](adapter: TypeAdapter[T], data: object, endpoint: str) -> T:
        try:
            return adapter.validate_python(data)
        except ValidationError as error:
            msg = f"unexpected reply from {endpoint}: {error}"
            raise TechnitiumError(msg) from error
