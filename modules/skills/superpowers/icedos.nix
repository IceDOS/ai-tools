{ icedosLib, lib, ... }:

let
  inherit (icedosLib) mkNonEmptyStrOption mkStrListOption;
  defaults = (lib.importTOML ./config.toml).icedos.ai-tools.skills.superpowers;
in
{
  options.icedos.ai-tools.skills.superpowers = {
    owner = mkNonEmptyStrOption { default = defaults.owner; };
    repo = mkNonEmptyStrOption { default = defaults.repo; };
    rev = mkNonEmptyStrOption { default = defaults.rev; };
    hash = mkNonEmptyStrOption { default = defaults.hash; };
    skills = mkStrListOption { default = defaults.skills; };
    autoload = mkStrListOption { default = defaults.autoload; };
  };

  outputs.nixosModules =
    { repoUrl, ... }:
    [
      (
        {
          config,
          options,
          lib,
          pkgs,
          ...
        }:

        let
          _file = "icedos-superpowers";
          has = name: icedosLib.hasModule { inherit config repoUrl name; };
          cfg = config.icedos.ai-tools.skills.superpowers;
          src = pkgs.fetchFromGitHub {
            inherit (cfg)
              owner
              repo
              rev
              hash
              ;
          };

          # Checks if claude-plugins-official is already defined in claude-code user marketplaces.
          hasOfficialMarketplace =
            u:
            let
              checkItem = m: (m.name or "") == "claude-plugins-official";
              checkList = l: builtins.isList l && builtins.any checkItem l;
              checkVal =
                v:
                if v ? content then
                  checkList v.content
                else if builtins.isList v then
                  checkList v
                else
                  false;
              otherDefs = lib.filter (
                d: d.file != "icedos-superpowers"
              ) options.icedos.ai-tools.claude-code.users.definitionsWithLocations;
            in
            builtins.any (
              d:
              let
                uVal = d.value.${u} or null;
              in
              uVal != null && uVal ? marketplaces && checkVal uVal.marketplaces
            ) otherDefs;

          officialMarketplace = {
            name = "claude-plugins-official";
            source = "fetchFromGitHub";
            owner = "anthropics";
            repo = "claude-plugins-official";
            rev = "main";
            hash = "sha256-g4ilhs1xWRX7Z3m8TaXbPG9gG/pXMuY18dbejmXF99A=";
          };
        in
        {
          inherit _file;

          # Claude Code gets superpowers via its official marketplace plugin.
          home-manager.sharedModules = lib.optionals (has "claude-code") [
            {
              programs.claude-code.settings.enabledPlugins."superpowers@claude-plugins-official" =
                lib.mkDefault true;
            }
          ];

          # Keys of absent agent modules must be omitted, not set to {}.
          icedos.ai-tools = {
            skills.pool = lib.mapAttrs (_: lib.mkDefault) (
              import ../../mcp/lib/upstream-skills.nix { inherit lib; } "${src}/skills" cfg.skills
            );

            rules = lib.listToAttrs (
              map (name: {
                name = "${name}-autoload";
                value = lib.mkDefault ''
                  # Autoload ${name}

                  At session start, before any other step, load the `${name}` skill and keep it active for the whole session. A plugin may list it as `<plugin>:${name}`.
                '';
              }) cfg.autoload
            );

            plugins.pool.superpowers = src;

            per-agent = {
              claude-code = lib.mkIf (has "claude-code") {
                excludeSkills = cfg.skills;
              };
              pi-agent = lib.mkIf (has "pi-agent") {
                globalPlugins = [ "superpowers" ];
                excludeSkills = cfg.skills;
              };
              opencode = lib.mkIf (has "opencode") {
                globalPlugins = [ "superpowers" ];
                excludeSkills = cfg.skills;
              };
            };
          }
          // lib.optionalAttrs (has "claude-code") {
            claude-code.users = lib.genAttrs (builtins.attrNames config.icedos.users) (
              u:
              lib.optionalAttrs (!hasOfficialMarketplace u) {
                marketplaces = [ officialMarketplace ];
              }
            );
          };
        }
      )
    ];

  meta = {
    name = "superpowers";
    description = "Superpowers agent workflow skills for every agent";
  };
}
