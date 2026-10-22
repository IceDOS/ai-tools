{ icedosLib, lib, ... }:

let
  inherit (icedosLib)
    mkAttrsOfOption
    mkBoolOption
    mkIntBetweenOption
    mkNonEmptyStrOption
    mkStrOption
    mkSubmoduleAttrsOption
    ;

  defaults = (lib.importTOML ./config.toml).icedos.ai-tools.plesk-mcp;
in
{
  options.icedos.ai-tools.plesk-mcp = {
    # Plesk servers, one MCP server named "plesk-<name>" each.
    servers = mkSubmoduleAttrsOption { default = defaults.servers; } {
      # Base URL, port included.
      host = mkNonEmptyStrOption { };

      # KEY=value file with the credentials; "" uses ~/.config/secrets/plesk-<name>.env.
      envFile = mkStrOption { default = ""; };

      # Skip TLS verification and allow http://.
      insecure = mkBoolOption { default = false; };

      # API timeout in seconds.
      timeout = mkIntBetweenOption {
        path = "icedos.ai-tools.plesk-mcp.servers.<name>.timeout";
        source = ./config.toml;
        default = 300;
      } 1 86400;
    };

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
          cfg = config.icedos.ai-tools.plesk-mcp;
          package = import ./package.nix { inherit pkgs; };
        in
        {
          environment.systemPackages = [ package ];

          # Credentials stay out of the store; the wrapper sources PLESK_ENV_FILE at start.
          icedos.ai-tools.mcp-servers = lib.mapAttrs' (
            name: server:
            lib.nameValuePair "plesk-${name}" {
              command = lib.getExe package;
              args = [
                "--timeout"
                (toString server.timeout)
              ]
              ++ lib.optional server.insecure "--insecure";
              env = {
                PLESK_HOST = server.host;
                PLESK_ENV_FILE =
                  if server.envFile != "" then server.envFile else "~/.config/secrets/plesk-${name}.env";
              };
            }
          ) cfg.servers;

          icedos.ai-tools.skills = lib.mapAttrs (_: lib.mkDefault) cfg.skills;
        }
      )
    ];

  meta = {
    name = "plesk-mcp";
    description = "Plesk MCP server, one entry per configured Plesk host, credentials read from env files";
  };
}
