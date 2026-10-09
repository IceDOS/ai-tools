{ icedosLib, lib, ... }:

let
  # Every folder here holds one language's SKILL.md.
  available = lib.attrNames (lib.filterAttrs (_: t: t == "directory") (builtins.readDir ./.));
in
{
  options.icedos.ai-tools.code-intelligence.languages =
    let
      inherit ((lib.importTOML ./config.toml).icedos.ai-tools.code-intelligence) languages;
    in
    icedosLib.mkStrListOption { default = languages; };

  outputs.nixosModules =
    { ... }:
    [
      (
        { config, lib, ... }:

        let
          inherit (config.icedos.ai-tools.code-intelligence) languages;

          # Upstream names are bare languages (nix, python); prefix them to avoid clashes.
          skillName = lang: "code-intelligence-${lang}";
        in
        {
          assertions = [
            {
              assertion = lib.all (l: lib.elem l available) languages;
              message = "code-intelligence: languages must be among ${lib.concatStringsSep ", " available}.";
            }
          ];

          icedos.ai-tools = {
            skills = lib.genAttrs' languages (
              lang:
              lib.nameValuePair (skillName lang) (
                lib.mkDefault (
                  builtins.replaceStrings [ "\nname: ${lang}\n" ] [ "\nname: ${skillName lang}\n" ] (
                    builtins.readFile ./${lang}/SKILL.md
                  )
                )
              )
            );

            rules.code-intelligence = lib.mkDefault ''
              # Code intelligence

              Language servers are available on demand on this NixOS machine.

              - Pull any language server ad hoc with `nix-shell -p <pkg> --run "<tool> ..."`.
              - For definitions, references, hover, diagnostics or symbol search, load the skill for the code's language (${
                lib.concatMapStringsSep ", " skillName languages
              }) and follow its exact commands. Do not guess tool output.
              - For a language without a skill, use its own checker, e.g. `nix-shell -p gopls --run "gopls check ./..."`, rust-analyzer, or `lua-language-server --check ./src`.
            '';
          };
        }
      )
    ];

  meta.name = "code-intelligence";
}
