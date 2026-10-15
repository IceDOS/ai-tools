{ icedosLib, lib, ... }:

let
  inherit (icedosLib) mkAttrsOfOption mkIntBetweenOption mkStrListOption;
  defaults = (lib.importTOML ./config.toml).icedos.ai-tools.rea-mcp;
in
{
  options.icedos.ai-tools.rea-mcp = {
    # Tool-call timeout in milliseconds for agents that honour it.
    timeout = mkIntBetweenOption {
      path = "icedos.ai-tools.rea-mcp.timeout";
      source = ./config.toml;
      default = defaults.timeout;
    } 1000 86400000;

    # Skills read from the packaged npm release (skills/), references inlined.
    upstreamSkills = mkStrListOption { default = defaults.upstreamSkills; };

    # Extra skills, name = full SKILL.md content; replaces a same-named upstream skill.
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
          cfg = config.icedos.ai-tools.rea-mcp;
          package = import ./package.nix { inherit pkgs; };

          upstream = import ../lib/upstream-skills.nix {
            inherit lib;
          } "${package.src}/skills" cfg.upstreamSkills;
        in
        {
          environment.systemPackages = [ package ];

          icedos.ai-tools.mcp-servers.rea = {
            command = lib.getExe package;
            args = [ "mcp" ];
            inherit (cfg) timeout;
          };

          icedos.ai-tools.skills = lib.mapAttrs (_: lib.mkDefault) (upstream // cfg.skills);
        }
      )
    ];

  meta = {
    name = "rea-mcp";
    description = "rea reverse-engineering MCP server with Ghidra, rizin and binutils";
  };
}
