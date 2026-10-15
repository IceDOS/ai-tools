{ icedosLib, lib, ... }:

let
  inherit (icedosLib) mkAttrsOfOption mkNonEmptyStrOption mkStrListOption;
  defaults = (lib.importTOML ./config.toml).icedos.ai-tools.context7-mcp;
in
{
  options.icedos.ai-tools.context7-mcp = {
    # Remote Context7 MCP endpoint.
    url = mkNonEmptyStrOption { default = defaults.url; };

    # Skills read from the nixpkgs context7-mcp source (skills/).
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
          cfg = config.icedos.ai-tools.context7-mcp;

          # nixpkgs' context7-mcp source, so the skill follows its version.
          upstream = import ../lib/upstream-skills.nix {
            inherit lib;
          } "${pkgs.context7-mcp.src}/skills" cfg.upstreamSkills;
        in
        {
          icedos.ai-tools.mcp-servers.context7.url = cfg.url;
          icedos.ai-tools.skills = lib.mapAttrs (_: lib.mkDefault) (upstream // cfg.skills);
        }
      )
    ];

  meta = {
    name = "context7-mcp";
    description = "Remote Context7 MCP server for current library and framework documentation";
  };
}
