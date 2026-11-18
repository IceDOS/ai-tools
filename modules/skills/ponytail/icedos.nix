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
