{
  description = "Ansible development environemnt";

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
            ruff
            mypy
          ];

          shellHook = ''
            export UV_PYTHON_PREFERENCE="managed"

            uv -q sync
            source ./env/.venv/bin/activate
          '';
        };
      }
    );
}
