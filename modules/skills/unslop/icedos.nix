{ icedosLib, lib, ... }:

let
  inherit (icedosLib) mkNonEmptyStrOption mkStrListOption;
  defaults = (lib.importTOML ./config.toml).icedos.ai-tools.skills.unslop;
in
{
  options.icedos.ai-tools.skills.unslop = {
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
    name = "unslop";
    description = "Unslop skills that strip AI writing patterns, for every agent";
  };
}
