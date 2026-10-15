{ icedosLib, lib, ... }:

let
  inherit (icedosLib) mkAttrsOfOption mkStrListOption;
  defaults = (lib.importTOML ./config.toml).icedos.ai-tools.kwin-mcp;
in
{
  options.icedos.ai-tools.kwin-mcp = {
    # Skills read from the packaged source (integrations/claude-code/skills).
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
          cfg = config.icedos.ai-tools.kwin-mcp;
          package = import ./package.nix { inherit pkgs; };

          upstream = import ../lib/upstream-skills.nix {
            inherit lib;
          } "${package.src}/integrations/claude-code/skills" cfg.upstreamSkills;
        in
        {
          environment.systemPackages = [ package ];
          icedos.ai-tools.mcp-servers.kwin-mcp.command = lib.getExe package;
          icedos.ai-tools.skills = lib.mapAttrs (_: lib.mkDefault) (upstream // cfg.skills);
        }
      )
    ];

  meta = {
    name = "kwin-mcp";
    description = "MCP server that drives KDE Plasma 6 Wayland apps, in a virtual KWin session or on the live desktop";
  };
}
