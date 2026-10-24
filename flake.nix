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
            # From nixpkgs, not PyPI: the PyPI binary needs nix-ld. Mypy stays in uv because
            # the nixpkgs package puts its own site-packages on PYTHONPATH.
            ruff
          ];

          shellHook = ''
            export UV_PYTHON_PREFERENCE="managed"

            uv -q sync
            source ./.venv/bin/activate

            pinned_ruff=$(sed -n 's/^lint = \["ruff==\(.*\)"\]$/\1/p' pyproject.toml)
            if [ "$(ruff --version)" != "ruff $pinned_ruff" ]; then
              echo "warning: $(ruff --version) differs from the CI pin ruff==$pinned_ruff in pyproject.toml" >&2
            fi
          '';
        };
      }
    );
}
