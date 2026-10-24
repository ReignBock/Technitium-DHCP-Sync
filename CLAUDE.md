# Technitium DHCP Sync

One-way sync of DHCP scopes and leases from a primary Technitium server to a secondary, for failover.
Design: `docs/superpowers/specs/2026-10-24-technitium-dhcp-sync-design.md`.

## Checks

Run all of these before committing. CI runs the same commands.

```sh
uv run ruff check && uv run ruff format --check && uv run mypy && uv run pytest
uv run pytest -m integration --no-cov    # needs Docker
```

- Work test first. Unit coverage must stay at 100% branch coverage.
- Ruff 0.17 in preview mode wants rule names, not codes, in config and suppressions. Suppress with
  `# ruff: ignore[rule-name]`, not `# noqa`, and give the reason on the line above.
- Pydantic validators must raise `ValueError`. A `TypeError` escapes as a crash.

## Layout

`models` (API payloads) -> `client` (HTTP) -> `planner` (pure diff) -> `sync` (one cycle) -> `runner`
(loop) -> `cli`. `config` loads the TOML. `tests/fakes.py` is an in-memory Technitium API for unit tests.

## Technitium API facts (confirmed on 15.6.0)

- No call creates a dynamic lease. Reservations are the only way to pin an address.
- `scopes/set` accepts POST form data. An empty value clears a field.
- `scopes/get` omits empty fields instead of returning null. A reservation with no host name comes back
  with `hostName: ""`.
- A fresh install has a disabled `Default` scope (192.168.1.0/24).
- Locally administered MACs (0x02 bit set) can be blocked by `blockLocallyAdministeredMacAddresses`.

## Environment

- The dev machine runs rootless Docker. Files mounted into containers must be world-readable.
- The dev shell falls back to nixpkgs ruff only when the PyPI binary cannot run (no nix-ld).

## Glossary

- **primary**: the Technitium server that owns the DHCP configuration.
- **secondary**: the server that receives the copy and serves clients while the primary is down.
- **scope**: one DHCP address pool and its options.
- **lease**: an address the primary handed out dynamically.
- **reservation**: a fixed MAC-to-address entry in a scope. Every primary lease becomes one on the secondary.
