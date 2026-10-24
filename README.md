# Technitium DHCP Sync

Copies DHCP scopes and leases from a primary Technitium DNS Server to a secondary one. While the primary is
down, the secondary hands out the same addresses. This is failover, not clustering: changes flow one way,
from the primary to the secondary.

## How it works

Every `interval_seconds`, the sync reads every scope and lease on the primary. Then it makes the secondary
match:

- Each primary scope is copied field for field, with three exceptions:
  - **Offer delay:** the primary's value plus `offer_delay_increment_seconds`. While both servers are up,
    the primary answers first.
  - **Lease time:** `secondary_lease_time_minutes` (default 5). This bounds how long a client stays on
    the secondary once failback starts (see below).
  - **Reservations:** the primary's reservations, plus one reservation for each dynamic lease on the
    primary. The Technitium API has no call that creates a dynamic lease, so a reservation is the only
    way to give a client the same address on the secondary. These entries carry the comment
    `technitium-dhcp-sync: dynamic lease`.
- Each scope's enabled state matches the primary, except while the scope is on hold (see below).
- A scope on the secondary that the primary does not have is deleted.

The sync writes to the secondary only when something differs. If any read from the primary fails, the
cycle stops before it writes anything, so a primary outage never removes the failover copy.

## Failback

A short lease alone does not bring clients back to the primary. A client renews directly with the server
that issued its lease, and the secondary keeps approving those renewals while it runs. So once the primary
is back, the sync moves clients off the secondary:

1. Each cycle reads the secondary's leases. A scope is **on hold** while any of its leases, dynamic or
   reserved, has not passed its expiry plus `failback_margin_seconds` (default 60).
1. A scope on hold is disabled on the secondary. Its clients' renewals go unanswered, so each client
   broadcasts, and the primary confirms the same address. This happens within one secondary lease time.
   Clients keep their address throughout.
1. A disabled scope renews nothing, so its lease expiries stay fixed. The hold ends on its own after the
   last one passes, and the scope is enabled again.

If the primary becomes unreachable, the sync enables every held scope at once, cancelling the hold. It
then checks the primary every `down_check_interval_seconds` (default 10) until a cycle succeeds, and goes
back to `interval_seconds` after that.

Holds are computed from the secondary's leases, so a restart does not end one early. The primary's enabled
flags from the last successful cycle are kept in memory only. After a restart during an outage, the sync
enables the scopes that are on hold.

## Known limits

- During an outage, the secondary can give new clients addresses the primary does not know about. When the
  primary returns, it may offer one of those addresses to another client. Turning on ping check in the
  primary scope reduces this risk. The sync copies that setting from the primary and does not change it.
- Technitium attaches a scope to the network interface that has an address in the scope's subnet. The
  secondary host needs an address in every subnet it serves.
- Renaming a scope on the primary deletes the old scope on the secondary and creates the new one.
- "Primary reachable" means the primary's API answers. If the API answers but clients cannot reach the
  primary's DHCP service, for example because of a broken relay or VLAN, holds still apply, and clients on
  the secondary lose their leases.
- The sync copies only the scope fields it knows. An integration test checks a real server's reply for
  fields it does not know. Tested with Technitium DNS Server 15.6.0.

## Running it

Create an API token on each server (Administration, Sessions, Create Token). The token's user needs
DHCP Server view permission on the primary and view, modify, and delete permission on the secondary.

Write a settings file based on [`config.example.toml`](config.example.toml). Secrets never go in the file.
Each `*_env` key names the environment variable that holds the value.

Run the container on the secondary host:

```yaml
services:
  technitium-dhcp-sync:
    image: ghcr.io/reignbock/technitium-dhcp-sync:latest
    restart: unless-stopped
    # Host networking lets the sync reach a secondary API bound to 127.0.0.1.
    network_mode: host
    environment:
      TECHNITIUM_PRIMARY_TOKEN: ${TECHNITIUM_PRIMARY_TOKEN}
      TECHNITIUM_SECONDARY_TOKEN: ${TECHNITIUM_SECONDARY_TOKEN}
    volumes:
      - ./config.toml:/config/config.toml:ro
```

The container runs as uid 10001, so the settings file must be readable by that user.

Command-line options:

| Option          | Effect                                                   |
| --------------- | -------------------------------------------------------- |
| `--config PATH` | Settings file. Default: `/config/config.toml`.           |
| `--once`        | Run one sync cycle and exit, instead of looping forever. |

Exit codes: `0` success, `1` a `--once` cycle failed, `2` the settings file is invalid.

## Development

The Nix dev shell (`nix develop`, or direnv with `use flake`) provides uv and gh, then installs the locked
dependencies into `.venv`.

```sh
uv run ruff check              # lint (all rules, preview mode)
uv run ruff format --check     # formatting
uv run mypy                    # strict type checks
uv run pytest                  # unit tests; fails below 100% branch coverage
uv run pytest -m integration --no-cov   # real servers in docker compose
```

The integration tests start two Technitium containers and an Alpine DHCP client on a private bridge
network. Set `SYNC_IMAGE` to an image tag to run each sync in that image instead of from the checkout.

## Releases

Pull requests into `main` must pass lint, type checks, unit tests, and integration tests. To release, set
`project.version` in `pyproject.toml`, merge, then push a matching tag such as `v0.1.0` on `main`. The
release workflow runs every check again, pushes the image to `ghcr.io/reignbock/technitium-dhcp-sync`, and
creates a GitHub release.

## License

Apache License 2.0. See [LICENSE](LICENSE).
