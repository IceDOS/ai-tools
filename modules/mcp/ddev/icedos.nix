{ icedosLib, lib, ... }:

let
  inherit (icedosLib) mkAttrsOfOption mkIntBetweenOption;
  defaults = (lib.importTOML ./config.toml).icedos.ai-tools.ddev-mcp;
in
{
  options.icedos.ai-tools.ddev-mcp = {
    # Tool-call timeout in milliseconds for agents that honour it.
    timeout = mkIntBetweenOption {
      path = "icedos.ai-tools.ddev-mcp.timeout";
      source = ./config.toml;
      default = defaults.timeout;
    } 1000 86400000;

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
          cfg = config.icedos.ai-tools.ddev-mcp;
          package = import ./package.nix { inherit pkgs; };
        in
        {
          environment.systemPackages = [ package ];

          icedos.ai-tools.mcp-servers.ddev = {
            command = lib.getExe package;
            inherit (cfg) timeout;
          };

          icedos.ai-tools.skills = lib.mapAttrs (_: lib.mkDefault) cfg.skills;
        }
      )
    ];

  meta = {
    name = "ddev-mcp";
    description = "DDEV MCP server that runs ddev commands for local web projects";
  };
}
