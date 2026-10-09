{ icedosLib, lib, ... }:

{
  options.icedos.ai-tools =
    let
      inherit (icedosLib) mkAttrsOfOption;
      inherit ((lib.importTOML ./config.toml).icedos.ai-tools) rules skills;
    in
    {
      # Rules every agent loads: name = markdown.
      rules = mkAttrsOfOption { default = rules; } lib.types.lines;

      # Skills every agent loads: name = full SKILL.md content.
      skills = mkAttrsOfOption { default = skills; } lib.types.lines;
    };

  outputs.nixosModules = { ... }: [ ];

  meta.name = "default";
}
