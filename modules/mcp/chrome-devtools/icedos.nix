{ icedosLib, lib, ... }:

let
  inherit (icedosLib)
    mkAttrsOfOption
    mkBoolOption
    mkIntBetweenOption
    mkStrListOption
    mkStrOption
    ;

  defaults = (lib.importTOML ./config.toml).icedos.ai-tools.chrome-devtools-mcp;
in
{
  options.icedos.ai-tools.chrome-devtools-mcp = {
    # Browser binary the server drives. Empty uses nixpkgs chromium.
    executablePath = mkStrOption { default = defaults.executablePath; };

    # Run the browser with a temporary profile that is deleted on exit.
    isolated = mkBoolOption { default = defaults.isolated; };

    # Send Google usage statistics.
    usageStatistics = mkBoolOption { default = defaults.usageStatistics; };

    # Tool-call timeout in milliseconds for agents that honour it.
    timeout = mkIntBetweenOption {
      path = "icedos.ai-tools.chrome-devtools-mcp.timeout";
      source = ./config.toml;
      default = defaults.timeout;
    } 1000 86400000;

    # Extra chrome-devtools-mcp arguments.
    extraArgs = mkStrListOption { default = defaults.extraArgs; };

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
          cfg = config.icedos.ai-tools.chrome-devtools-mcp;
          package = import ./package.nix { inherit pkgs; };

          executablePath = if cfg.executablePath != "" then cfg.executablePath else lib.getExe pkgs.chromium;

          upstream = import ../lib/upstream-skills.nix {
            inherit lib;
          } "${package.src}/skills" cfg.upstreamSkills;
        in
        {
          environment.systemPackages = [ package ];

          icedos.ai-tools.mcp-servers.chrome-devtools = {
            command = lib.getExe package;
            args = [
              "--executable-path=${executablePath}"
              "--isolated=${lib.boolToString cfg.isolated}"
            ]
            ++ lib.optional (!cfg.usageStatistics) "--no-usage-statistics"
            ++ cfg.extraArgs;
            inherit (cfg) timeout;
          };

          icedos.ai-tools.skills = lib.mapAttrs (_: lib.mkDefault) (upstream // cfg.skills);
        }
      )
    ];

  meta = {
    name = "chrome-devtools-mcp";
    description = "Chrome DevTools MCP server that drives and inspects a Chromium browser";
  };
}
