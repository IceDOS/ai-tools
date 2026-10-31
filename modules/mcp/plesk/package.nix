{ pkgs }:

let
  inherit (pkgs) lib;

  # Pin refreshed by ./update.sh from the PyPI sdist; upstream pushes no git tags.
  source = lib.importJSON ./source.json;

  # nixpkgs FastMCP 3.x needs the MCP SDK 1.x.
  python = import ../lib/python-mcp1.nix { inherit pkgs; };

  unwrapped = python.pkgs.buildPythonApplication {
    pname = "plesk-mcp";
    inherit (source) version;
    pyproject = true;

    src = pkgs.fetchurl { inherit (source) url hash; };

    # Upstream caps uv_build below the 0.12 that nixpkgs ships.
    postPatch = ''
      substituteInPlace pyproject.toml \
        --replace-fail '"uv_build>=0.10,<0.12"' '"uv_build>=0.10"'
    '';

    build-system = [ python.pkgs.uv-build ];

    dependencies = with python.pkgs; [
      async-lru
      fastmcp
      httpx
    ];

    pythonImportsCheck = [ "plesk_mcp" ];
    doCheck = false;

    meta.mainProgram = "plesk-mcp";
  };
in
# PLESK_ENV_FILE names a KEY=value file outside the store holding PLESK_API_KEY or PLESK_PASSWORD.
pkgs.writeShellApplication {
  name = "plesk-mcp";
  text = ''
    if [ -n "''${PLESK_ENV_FILE:-}" ]; then
      file="''${PLESK_ENV_FILE/#\~/$HOME}"
      if [ ! -r "$file" ]; then
        echo "plesk-mcp: cannot read PLESK_ENV_FILE $file" >&2
        exit 1
      fi
      set -a
      # shellcheck source=/dev/null
      . "$file"
      set +a
    fi
    exec ${lib.getExe unwrapped} "$@"
  '';
  derivationArgs.passthru = { inherit unwrapped; };
}
