{
  description = "Technitium DHCP Sync development environment";

  inputs = {
    nixpkgs.url = "github:nixos/nixpkgs/nixos-unstable";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs =
    {
      self,
      nixpkgs,
      flake-utils,
    }:
    flake-utils.lib.eachDefaultSystem (
      system:
      let
        pkgs = import nixpkgs { inherit system; };
      in
      {
        devShells.default = pkgs.mkShell {
          buildInputs = with pkgs; [
            uv
            (mdformat.withPlugins (p: [ p.mdformat-gfm ]))
            gh
          ];

          shellHook = ''
            export UV_PYTHON_PREFERENCE="managed"

            uv -q sync
            source ./.venv/bin/activate

            # The PyPI ruff binary needs nix-ld. Where it cannot run, link the nixpkgs build in
            # its place, so `ruff` and `uv run ruff` both work. uv reinstalls the PyPI binary when
            # the locked version changes, and this check runs again on the next shell entry.
            if ! ./.venv/bin/ruff --version >/dev/null 2>&1; then
              ln -sf ${pkgs.ruff}/bin/ruff ./.venv/bin/ruff
              echo "PyPI ruff cannot run here; using $(ruff --version) from nixpkgs" >&2
            fi
          '';
        };
      }
    );
}
