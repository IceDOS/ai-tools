{ icedosLib, lib, ... }:

let
  inherit (icedosLib) mkNonEmptyStrOption mkStrListOption;
  defaults = (lib.importTOML ./config.toml).icedos.ai-tools.skills.ponytail;
in
{
  options.icedos.ai-tools.skills.ponytail = {
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
          lib,
          pkgs,
          ...
        }:

        let
          has = name: icedosLib.hasModule { inherit config repoUrl name; };
          cfg = config.icedos.ai-tools.skills.ponytail;
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
          icedos.ai-tools.skills.pool = lib.mapAttrs (_: lib.mkDefault) (
            import ../../mcp/lib/upstream-skills.nix { inherit lib; } "${src}/skills" cfg.skills
          );

          # The repo is also a Claude Code plugin and a pi package; both ship these skills themselves.
          # Claude refuses its hook files and band module through a skills-dir symlink into the store; a plugin dir resolves the link first.
          home-manager.sharedModules = [
            { programs.claude-code.settings.env.CLAUDE_CODE_PLUGIN_DIRS = "${src}"; }
          ];

          icedos.ai-tools.claude-code = lib.optionalAttrs (has "claude-code") {
            excludeSkills = cfg.skills;
          };

          icedos.ai-tools.pi-agent = lib.optionalAttrs (has "pi-agent") {
            packages = [ "${src}" ];
            skills.exclude = cfg.skills;
          };

          icedos.ai-tools.rules = lib.listToAttrs (
            map (name: {
              name = "${name}-autoload";
              value = lib.mkDefault ''
                # Autoload ${name}

                At session start, before any other step, load the `${name}` skill and keep it active for the whole session. A plugin may list it as `<plugin>:${name}`.
              '';
            }) cfg.autoload
          );
        }
      )
    ];

  meta = {
    name = "ponytail";
    description = "Ponytail lazy-senior-dev skills for every agent";
  };
}
