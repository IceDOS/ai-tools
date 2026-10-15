{ icedosLib, lib, ... }:

let
  inherit (icedosLib)
    mkAttrsOfOption
    mkIntBetweenOption
    mkStrListOption
    mkStrOption
    ;

  defaults = (lib.importTOML ./config.toml).icedos.ai-tools.signal-mcp;
in
{
  options.icedos.ai-tools.signal-mcp = {
    # Signal number (E.164) of the person the agent talks to.
    operator = mkStrOption { default = defaults.operator; };

    # Signal number the signal-cli daemon runs as. Empty uses operator.
    account = mkStrOption { default = defaults.account; };

    # Recipients the agent may message: numbers, group names or group ids.
    trustedRecipients = mkStrListOption { default = defaults.trustedRecipients; };

    # Senders whose inbound messages reach the agent.
    trustedSenders = mkStrListOption { default = defaults.trustedSenders; };

    # Port of the local signal-cli JSON-RPC daemon, started on demand.
    rpcPort = mkIntBetweenOption {
      path = "icedos.ai-tools.signal-mcp.rpcPort";
      source = ./config.toml;
      default = defaults.rpcPort;
    } 1 65535;

    skills = mkAttrsOfOption { default = defaults.skills; } lib.types.lines;
  };

  outputs.nixosModules =
    { ... }:
    [
      (
        {
          config,
          lib,
          pkgs,
          ...
        }:

        let
          cfg = config.icedos.ai-tools.signal-mcp;
          package = import ./package.nix { inherit pkgs; };
          toArgs = flag: map (value: "${flag}=${value}");
        in
        {
          assertions = [
            {
              assertion = cfg.operator != "";
              message = "icedos.ai-tools.signal-mcp.operator must be set to a Signal number";
            }
          ];

          environment.systemPackages = [
            package
            pkgs.signal-cli
          ];

          # Values go through env, since the wrapper starts the daemon before signal-mcp parses flags.
          icedos.ai-tools.mcp-servers.signal-mcp = {
            command = lib.getExe package;
            args =
              toArgs "--trusted-recipient" cfg.trustedRecipients ++ toArgs "--trusted-sender" cfg.trustedSenders;
            env = {
              SIGNAL_MCP_OPERATOR = cfg.operator;
              SIGNAL_MCP_RPC_PORT = toString cfg.rpcPort;
            }
            // lib.optionalAttrs (cfg.account != "") { SIGNAL_MCP_ACCOUNT = cfg.account; };
          };

          icedos.ai-tools.skills = lib.mapAttrs (_: lib.mkDefault) cfg.skills;
        }
      )
    ];

  meta = {
    name = "signal-mcp";
    description = "Signal MCP server over a signal-cli daemon, with trusted recipient and sender allowlists";
  };
}
