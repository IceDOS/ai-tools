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
          inherit (lib)
            any
            attrNames
            listToAttrs
            mkIf
            ;

          inherit (config.icedos) users;

          inherit (config.icedos.ai-tools.ollama)
            vulkan
            host
            port
            loadModels
            ;

          baseURL = "http://${host}:${toString port}/v1";
        in
        {
          # Skills for this module, added to the shared icedos.ai-tools.skills pool.
          icedos.ai-tools.skills = lib.mapAttrs (_: lib.mkDefault) config.icedos.ai-tools.ollama.skills;

          services.ollama = {
            enable = true;
            package = if vulkan then pkgs.ollama-vulkan else pkgs.ollama;
            inherit host port loadModels;
          };

          # Expose the local ollama endpoint to opencode when it is enabled.
          home-manager.sharedModules =
            mkIf (any (user: config.home-manager.users.${user}.programs.opencode.enable) (attrNames users))
              [
                {
                  # @ai-sdk/openai-compatible is fetched from npm on first use
                  # (normal user-runtime network, not a build-time dependency).
                  programs.opencode.settings.provider.ollama = {
                    npm = "@ai-sdk/openai-compatible";
                    name = "Ollama (local)";
                    options.baseURL = baseURL;

                    models = listToAttrs (
                      map (m: {
                        name = m;
                        value.name = m;
                      }) loadModels
                    );
                  };
                }
              ];

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
