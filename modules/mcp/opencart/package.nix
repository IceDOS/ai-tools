{ pkgs }:

let
  inherit (pkgs) lib;

  # Pin refreshed by ./update.sh; upstream main runs ahead of its tags.
  source = lib.importJSON ./source.json;

  unwrapped = pkgs.python3Packages.buildPythonApplication {
    pname = "opencart-mcp";
    inherit (source) version;
    pyproject = true;

    src = pkgs.fetchFromGitHub {
      owner = "chrisbray85";
      repo = "opencart-mcp";
      inherit (source) rev hash;
    };

    build-system = [ pkgs.python3Packages.setuptools ];

    dependencies = with pkgs.python3Packages; [
      fastmcp
      paramiko
      pymysql
      python-dotenv
    ];

    pythonImportsCheck = [ "opencart_mcp" ];
    doCheck = false;

    meta.mainProgram = "opencart-mcp";
  };
in
# OPENCART_ENV_FILE names a KEY=value file outside the store, e.g. with OPENCART_DB_PASS.
pkgs.writeShellApplication {
  name = "opencart-mcp";
  text = ''
    if [ -n "''${OPENCART_ENV_FILE:-}" ]; then
      file="''${OPENCART_ENV_FILE/#\~/$HOME}"
      if [ ! -r "$file" ]; then
        echo "opencart-mcp: cannot read OPENCART_ENV_FILE $file" >&2
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
