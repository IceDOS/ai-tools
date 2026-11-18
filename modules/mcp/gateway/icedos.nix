{ icedosLib, lib, ... }:

let
  inherit (icedosLib)
    mkIntBetweenOption
    mkNonEmptyStrOption
    ;

  defaults = (lib.importTOML ./config.toml).icedos.ai-tools.mcp-gateway;
in
{
  options.icedos.ai-tools.mcp-gateway = {
    # Hostname the gateway URLs use; networking.hosts points it at 127.0.0.1.
    host = mkNonEmptyStrOption { default = defaults.host; };

    # Port the gateway listens on.
    port = mkIntBetweenOption {
      path = "icedos.ai-tools.mcp-gateway.port";
      source = ./config.toml;
      default = defaults.port;
    } 1 65535;

    # Seconds without requests before an idle server process is stopped.
    idleTimeoutSec = mkIntBetweenOption {
      path = "icedos.ai-tools.mcp-gateway.idleTimeoutSec";
      source = ./config.toml;
      default = defaults.idleTimeoutSec;
    } 1 86400;
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
          cfg = config.icedos.ai-tools.mcp-gateway;
          modes = config.icedos.ai-tools.mcp-gateway-modes;
          package = import ./package.nix { inherit pkgs; };

          servers = config.icedos.ai-tools.mcp-servers;

          # Remote url entries have no process to run, so they are never routed.
          # A server without a mode keeps the stdio default and stays unrouted.
          routed = lib.filterAttrs (
            name: server: (modes.${name} or "stdio") != "stdio" && server ? command
          ) servers;

          # A mode for a server nobody registers would be dropped without a word,
          # so a typo (a module name such as `rea-mcp` for `rea`, say) fails here.
          unknownModeServers = lib.subtractLists (lib.attrNames servers) (lib.attrNames modes);

          # A url entry has no command to spawn; only stdio can apply to it.
          unroutableModes = lib.filter (name: modes.${name} != "stdio" && !(servers.${name} ? command)) (
            lib.intersectLists (lib.attrNames servers) (lib.attrNames modes)
          );

          # Only what the daemon spawns with; its own env is inherited as-is.
          configFile = pkgs.writeText "icedos-mcp-gateway.json" (
            builtins.toJSON {
              # Bind loopback only; host above is just the name agents dial.
              host = "127.0.0.1";
              inherit (cfg) port idleTimeoutSec;
              servers = lib.mapAttrs (
                name: server:
                {
                  mode = modes.${name};
                }
                // lib.filterAttrs (
                  key: _:
                  lib.elem key [
                    "command"
                    "args"
                    "env"
                  ]
                ) server
              ) routed;
            }
          );
        in
        {
          assertions = [
            {
              assertion = unknownModeServers == [ ];
              message = ''
                icedos.ai-tools.mcp-gateway-modes names unknown MCP servers: ${lib.concatStringsSep ", " unknownModeServers}.
                Known servers: ${lib.concatStringsSep ", " (lib.attrNames servers)}.
                Only what icedos.ai-tools.mcp-servers registers is routed; use the
                server name, not the module name.
              '';
            }
            {
              assertion = unroutableModes == [ ];
              message = ''
                icedos.ai-tools.mcp-gateway-modes routes url servers, which have no
                process to run: ${
                  lib.concatStringsSep ", " (map (name: "${name} = ${modes.${name}}") unroutableModes)
                }.
                Remote url servers are already HTTP; remove the entry or set it to stdio.
              '';
            }
          ];

          # Agents dial the alias, the daemon itself never leaves loopback.
          networking.hosts."127.0.0.1" = [ cfg.host ];

          environment.systemPackages = [ package ];

          # default.nix swaps these URLs in for the server entries it publishes.
          icedos.ai-tools.mcp-http-endpoints = lib.mapAttrs (
            name: _: "http://${cfg.host}:${toString cfg.port}/${name}"
          ) routed;

          home-manager.sharedModules = [
            (
              { lib, ... }:
              {
                # Per user, since the daemon runs servers with that user's credentials.
                systemd.user.services.icedos-mcp-gateway = {
                  Unit = {
                    Description = "MCP gateway for local MCP servers";
                    PartOf = [ "graphical-session.target" ];
                    After = [ "graphical-session.target" ];
                  };

                  Service = {
                    ExecStart = "${lib.getExe package} --config ${configFile}";
                    Restart = "on-failure";
                    RestartSec = 5;
                  };

                  Install.WantedBy = [ "graphical-session.target" ];
                };
              }
            )
          ];
        }
      )
    ];

  meta = {
    name = "mcp-gateway";
    description = "One local HTTP gateway in front of the shared MCP servers, so agents stop starting a copy each";
  };
}
