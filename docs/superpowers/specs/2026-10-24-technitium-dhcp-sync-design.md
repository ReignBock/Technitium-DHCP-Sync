# Technitium DHCP Sync: design

## Context

Two Technitium DNS servers each run a DHCP server. Technitium clustering syncs DNS configuration only, not DHCP. We need a
script that keeps a **secondary** DHCP server ready to take over while the **primary** is down. This is failover, not
clustering. The script runs in a Docker container on the secondary host.

The repo is empty except for a Nix flake, an Apache 2.0 license, and a stub `pyproject.toml` (name `infrastructure`,
Python `==3.13.*`). The remote is `git@github.com:ReignBock/Technitium-DHCP-Sync.git`. `gh` is not installed.

### Research result (validated against `APIDOCS.md` on the Technitium `master` branch)

- The API has **no call that creates a dynamic lease**. Lease calls are `leases/list`, `leases/remove`,
  `leases/convertToReserved`, and `leases/convertToDynamic`. All of them act on a lease that already exists on that
  server. The user's assumption holds: each primary lease becomes a **reserved lease** on the secondary.
- `scopes/set` takes the full scope configuration, including `reservedLeases` as
  `host|MAC|IP|comments` joined with `|`. It creates the scope if missing. `offerDelayTime` is in milliseconds.
- `scopes/get` returns every field that `scopes/set` accepts. `scopes/list` gives each scope's name and `enabled` flag.
- `leases/list` returns `scope`, `type` (`Dynamic` / `Reserved`), `hardwareAddress`, `address`, `hostName`, and the
  lease timestamps.
- Auth: `Authorization: Bearer <token>`. Tokens come from `/api/user/createToken`, and they do not expire.

### Decisions (confirmed with user)

| Topic | Decision |
|---|---|
| Run mode | Daemon loop, interval set in TOML. A `--once` flag exists for tests and debugging. |
| Scopes only on secondary | Deleted. The secondary mirrors the primary exactly. |
| Secondaries per instance | Exactly one. |
| Release | PR gating on all checks. A `vX.Y.Z` tag on `main` creates a GitHub Release and pushes an image to `ghcr.io`. |

## Design

### Sync semantics (one cycle)

1. Read all primary scopes (`scopes/list`, then `scopes/get` for each) and `leases/list`. **If any primary call
   fails, the cycle aborts and the secondary is left unchanged.** A down primary must never wipe the failover copy.
2. Read all secondary scopes the same way.
3. For each primary scope, build the desired secondary scope:
   - every field copied unchanged, except:
   - `offerDelayTime` = primary value + `offer_delay_increment_seconds * 1000`
   - lease time = `secondary_lease_time_minutes` (split into days, hours, and minutes)
   - `reservedLeases` = the primary's reservations, plus one reservation for each primary `Dynamic` lease in that
     scope. The key is the MAC address. A primary reservation wins over a lease with the same MAC. Lease-derived
     entries get the comment `technitium-dhcp-sync: dynamic lease`.
   - `enabled` mirrors the primary.
4. Compare desired with actual through a normalized, frozen model. Call `scopes/set` only when they differ, then
   `enable` or `disable` if the flag differs.
5. Delete every secondary scope whose name is not on the primary.

When a lease expires on the primary, it leaves the desired set, and the next `scopes/set` removes its reservation.
The sync is one-way: leases the secondary hands out during an outage never go back to the primary.

### Package layout (`src/technitium_dhcp_sync/`)

| Module | Responsibility | Depends on |
|---|---|---|
| `config.py` | Loads TOML (stdlib `tomllib`) into pydantic models. Resolves `*_env` fields from the environment. Gives clear errors. | pydantic |
| `models.py` | Frozen `Scope`, `ReservedLease`, `Lease`, and related value types. Parses from API JSON and serializes to `scopes/set` params. | none |
| `client.py` | `TechnitiumClient` over `httpx`: list/get/set/delete/enable/disable scopes, list leases. Raises `TechnitiumError` on HTTP failure or on `status != "ok"`. | httpx, models |
| `planner.py` | Pure functions: `desired_scope(...)` and `plan(primary, secondary, leases, settings) -> list[Action]`. No I/O. | models |
| `sync.py` | `run_cycle(primary_client, secondary_client, settings)`: fetch, plan, apply, log. | client, planner |
| `runner.py` | Daemon loop with an interval and a stop `Event`. SIGTERM/SIGINT stop it cleanly. One failed cycle is logged, and the loop continues. | sync |
| `__main__.py` / `cli.py` | `argparse` with `--config PATH` (default `/config/config.toml`) and `--once`. Sets up logging. Exit codes: 0 ok, 2 config error, 1 failed `--once` cycle. | all |

Runtime dependencies: `httpx`, `pydantic`. Everything else is stdlib.

### Config file

```toml
[sync]
interval_seconds = 60
offer_delay_increment_seconds = 2     # default 2
secondary_lease_time_minutes = 5      # default 5
log_level = "INFO"

[primary]
url = "https://dns1.example.com:53443"
token_env = "TECHNITIUM_PRIMARY_TOKEN"   # name of the env var, never the secret
verify_tls = true
timeout_seconds = 10

[secondary]
url = "http://127.0.0.1:5380"
token_env = "TECHNITIUM_SECONDARY_TOKEN"
```

Rule: any secret setting ends in `_env` and holds an environment variable name. Loading fails if that variable is
missing or empty.

### Known limits (documented in README)

- During an outage, the secondary can give new clients dynamic addresses that the primary does not know about. Ping
  check on the primary scope reduces conflicts after recovery. The script copies that setting and does not force it.
- Technitium binds a scope to the interface that has an address in the scope's subnet. The secondary host must have
  an address in each synced subnet.
- Renaming a scope on the primary shows up as a delete plus a create on the secondary.

