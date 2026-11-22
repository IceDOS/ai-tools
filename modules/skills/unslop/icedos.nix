{ icedosLib, lib, ... }:

let
  inherit (icedosLib) mkNonEmptyStrOption mkStrListOption;
  defaults = (lib.importTOML ./config.toml).icedos.ai-tools.skills.unslop;
  source = lib.importJSON ./source.json;
in
{
  options.icedos.ai-tools.skills.unslop = {
    owner = mkNonEmptyStrOption { default = defaults.owner; };
    repo = mkNonEmptyStrOption { default = defaults.repo; };
    rev = mkNonEmptyStrOption { default = source.rev; };
    hash = mkNonEmptyStrOption { default = source.hash; };
    skills = mkStrListOption { default = defaults.skills; };
    autoload = mkStrListOption { default = defaults.autoload; };
  };

  outputs.nixosModules =
    { repoUrl, ... }:
    [
      (
        {
          config,
          lib,
          pkgs,
          ...
        }:

        let
          has = name: icedosLib.hasModule { inherit config repoUrl name; };
          cfg = config.icedos.ai-tools.skills.unslop;
          src = pkgs.fetchFromGitHub {
            inherit (cfg)
              owner
              repo
              rev
              hash
              ;
          };
        in
        {
          # The repo is also a Claude Code plugin that ships these skills itself, unslop-file's scripts included.
          home-manager.sharedModules = [ { programs.claude-code.plugins.unslop = src; } ];

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

            per-agent.claude-code = lib.mkIf (has "claude-code") {
              excludeSkills = cfg.skills;
            };
          };
        }
      )
    ];

  meta = {
    name = "unslop";
    description = "Unslop skills that strip AI writing patterns, for every agent";
  };
}
