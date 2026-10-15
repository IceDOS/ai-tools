{ icedosLib, lib, ... }:

let
  inherit (icedosLib)
    mkAttrsOfOption
    mkBoolOption
    mkNonEmptyStrOption
    mkStrListOption
    ;

  defaults = (lib.importTOML ./config.toml).icedos.ai-tools.basic-memory-mcp;
in
{
  options.icedos.ai-tools.basic-memory-mcp = {
    # Config dir, relative to $HOME (BASIC_MEMORY_CONFIG_DIR).
    configDir = mkNonEmptyStrOption { default = defaults.configDir; };

    # Default notes and database dir, relative to $HOME (BASIC_MEMORY_HOME).
    dataDir = mkNonEmptyStrOption { default = defaults.dataDir; };

    # Ignore Basic Memory Cloud and always use the local database.
    forceLocal = mkBoolOption { default = defaults.forceLocal; };

    # Skills read from the release source (skills/), references inlined.
    upstreamSkills = mkStrListOption { default = defaults.upstreamSkills; };

    # Skills, name = full SKILL.md content; replaces a same-named upstream skill.
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
          cfg = config.icedos.ai-tools.basic-memory-mcp;

          # Set in the wrapper so the CLI and the MCP server agree, whoever launches them. Env wins.
          package = import ./package.nix {
            inherit pkgs;
            preExec = ''
              export BASIC_MEMORY_CONFIG_DIR="''${BASIC_MEMORY_CONFIG_DIR:-$HOME/${cfg.configDir}}"
              export BASIC_MEMORY_HOME="''${BASIC_MEMORY_HOME:-$HOME/${cfg.dataDir}}"
              ${lib.optionalString cfg.forceLocal ''export BASIC_MEMORY_FORCE_LOCAL="''${BASIC_MEMORY_FORCE_LOCAL:-true}"''}
            '';
          };

          upstream = import ../lib/upstream-skills.nix {
            inherit lib;
          } "${package.src}/skills" cfg.upstreamSkills;
        in
        {
          environment.systemPackages = [ package ];

          icedos.ai-tools.mcp-servers.basic-memory = {
            command = lib.getExe package;
            args = [ "mcp" ];
          };

          icedos.ai-tools.skills = lib.mapAttrs (_: lib.mkDefault) (upstream // cfg.skills);
        }
      )
    ];

  meta = {
    name = "basic-memory-mcp";
    description = "Basic Memory MCP server: local markdown notes with a knowledge graph, shared by every agent";
  };
}
