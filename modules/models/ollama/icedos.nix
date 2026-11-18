{ lib, icedosLib, ... }:

{
  options.icedos.ai-tools.ollama =
    let
      inherit (lib) importTOML;

      inherit (icedosLib)
        mkAttrsOfOption
        mkBoolOption
        mkStrOption
        mkNumberOption
        mkStrListOption
        ;

      inherit ((importTOML ./config.toml).icedos.ai-tools.ollama)
        vulkan
        host
        port
        loadModels
        skills
        ;
    in
    {
      vulkan = mkBoolOption { default = vulkan; };
      host = mkStrOption { default = host; };
      port = mkNumberOption { default = port; };
      loadModels = mkStrListOption { default = loadModels; };

      # Skills for this module (name = SKILL.md), loaded by every agent via icedos.ai-tools.skills.
      skills = mkAttrsOfOption { default = skills; } lib.types.lines;
    };

  outputs.nixosModules =
    { ... }:
    [
      (
        {
          config,
          pkgs,
          lib,
          ...
        }:

        let
          inherit (config.icedos.ai-tools.ollama)
            vulkan
            host
            port
            loadModels
            ;
        in
        {
          # Skills for this module, added to the shared icedos.ai-tools.skills pool.
          icedos.ai-tools.skills = lib.mapAttrs (_: lib.mkDefault) config.icedos.ai-tools.ollama.skills;

          services.ollama = {
            enable = true;
            package = if vulkan then pkgs.ollama-vulkan else pkgs.ollama;
            inherit host port loadModels;
          };

          icedos.system.tips.list = [
            "ollama run <model> chats with an AI model that runs on your own machine."
          ]
          ++ lib.optionals (loadModels != [ ]) [
            "The AI models you listed in config.toml download themselves on rebuild."
          ];
        }
      )
    ];

  meta.name = "ollama";
}
