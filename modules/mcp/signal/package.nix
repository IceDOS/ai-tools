{ pkgs }:

let
  python = pkgs.python3;

  # Pin refreshed by ./update.sh. Upstream main runs ahead of its tags, so it tracks main.
  source = pkgs.lib.importJSON ./source.json;

  signal-mcp = python.pkgs.buildPythonApplication {
    pname = "signal-mcp";
    inherit (source) version;
    pyproject = true;

    src = pkgs.fetchFromGitHub {
      owner = "joestump";
      repo = "signal-mcp";
      inherit (source) rev hash;
    };

    build-system = [ python.pkgs.hatchling ];

    dependencies = with python.pkgs; [
      anyio
      fastmcp
      mcp
    ];

    pythonImportsCheck = [ "signal_mcp" ];
    doCheck = false;
  };
in
# Starts a signal-cli JSON-RPC daemon for the account when none listens yet, then serves MCP over stdio.
(pkgs.writeShellApplication {
  name = "signal-mcp-stdio";
  runtimeInputs = [ pkgs.signal-cli ];
  text = ''
    host="''${SIGNAL_MCP_RPC_HOST:-127.0.0.1}"
    port="''${SIGNAL_MCP_RPC_PORT:-7583}"
    account="''${SIGNAL_MCP_ACCOUNT:-''${SIGNAL_MCP_OPERATOR:-}}"

    listening() {
      (exec 3<>"/dev/tcp/$host/$port") 2>/dev/null
    }

    if ! listening; then
      if [ -z "$account" ]; then
        echo "signal-mcp-stdio: set SIGNAL_MCP_ACCOUNT or SIGNAL_MCP_OPERATOR" >&2
        exit 1
      fi

      signal-cli -a "$account" daemon --tcp "$host:$port" --receive-mode on-start --no-receive-stdout &

      for _ in $(seq 1 50); do
        listening && break
        sleep 0.2
      done
    fi

    exec ${signal-mcp}/bin/signal-mcp --transport stdio "$@"
  '';
}).overrideAttrs
  (old: {
    passthru = (old.passthru or { }) // {
      unwrapped = signal-mcp;
    };
  })
