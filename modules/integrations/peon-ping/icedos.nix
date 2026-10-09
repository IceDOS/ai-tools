{ icedosLib, lib, ... }:

{
  inputs.peon-ping = {
    url = "github:PeonPing/peon-ping";
    inputs.nixpkgs.follows = "nixpkgs";
  };

  options.icedos.ai-tools.peon-ping.users =
    let
      inherit (lib) head importTOML;

      inherit (icedosLib)
        mkAttrsOption
        mkBoolOption
        mkFloatBetweenOption
        mkIntBetweenOption
        mkStrListOption
        mkStrOption
        mkSubmoduleAttrsOption
        mkSubmoduleListOption
        ;

      inherit ((importTOML ./config.toml).icedos.ai-tools.peon-ping.users.username)
        categories
        claudeCodeIntegration
        defaultPack
        desktopNotifications
        packs
        silentWindowSeconds
        suppressSubagentComplete
        volume
        ;

      customPackTemplate = head (importTOML ./custom-packs.toml)
        .icedos.ai-tools.peon-ping.users.username.customPacks;
    in
    mkSubmoduleAttrsOption { default = { }; } {
      defaultPack = mkStrOption { default = defaultPack; };

      volume = mkFloatBetweenOption {
        path = "icedos.ai-tools.peon-ping.users.<u>.volume";
        source = ./config.toml;
        default = volume;
      } 0.0 1.0;

      desktopNotifications = mkBoolOption { default = desktopNotifications; };
      claudeCodeIntegration = mkBoolOption { default = claudeCodeIntegration; };
      suppressSubagentComplete = mkBoolOption { default = suppressSubagentComplete; };

      silentWindowSeconds = mkIntBetweenOption {
        path = "icedos.ai-tools.peon-ping.users.<u>.silentWindowSeconds";
        source = ./config.toml;
        default = silentWindowSeconds;
      } 0 3600;

      # Per-category sound switches. Keys (all default on except task.acknowledge):
      #   session.start  task.acknowledge  task.complete  task.error
      #   input.required  resource.limit  user.spam
      # task.complete covers both turn-end and idle/stuck; input.required covers
      # permission prompts and elicitation questions.
      categories = mkAttrsOption { default = categories; };

      packs = mkStrListOption { default = packs; };

      customPacks = mkSubmoduleListOption { default = [ ]; } {
        name = mkStrOption { default = customPackTemplate.name; };
        owner = mkStrOption { default = customPackTemplate.owner; };
        repo = mkStrOption { default = customPackTemplate.repo; };
        rev = mkStrOption { default = customPackTemplate.rev; };
        hash = mkStrOption { default = customPackTemplate.hash; };
      };
    };

  # Skills for this module (name = SKILL.md), loaded by every agent via icedos.ai-tools.skills.
  options.icedos.ai-tools.peon-ping.skills = icedosLib.mkAttrsOfOption {
    default = (lib.importTOML ./config.toml).icedos.ai-tools.peon-ping.skills;
  } lib.types.lines;

  outputs.nixosModules =
    { inputs, ... }:
    [
      (
        {
          config,
          lib,
          pkgs,
          ...
        }:

        let
          inherit (lib) filter optionalAttrs;
          peonUsers = config.icedos.ai-tools.peon-ping.users;
          peonPkg = inputs.peon-ping.packages.${pkgs.stdenv.hostPlatform.system}.default;

          renderCustomPack = cp: {
            inherit (cp) name;
            src = pkgs.fetchFromGitHub {
              inherit (cp)
                owner
                repo
                rev
                hash
                ;
            };
          };

          renderInstallPacks =
            u: u.packs ++ (map renderCustomPack (filter (cp: cp.name != "") u.customPacks));

          renderPeonSettings =
            u:
            {
              default_pack = u.defaultPack;
              volume = u.volume;
              desktop_notifications = u.desktopNotifications;
              suppress_subagent_complete = u.suppressSubagentComplete;
              silent_window_seconds = u.silentWindowSeconds;
            }
            // optionalAttrs (u.categories != { }) { categories = u.categories; };
        in
        {
          # Skills for this module, added to the shared icedos.ai-tools.skills pool.
          icedos.ai-tools.skills = lib.mapAttrs (_: lib.mkDefault) config.icedos.ai-tools.peon-ping.skills;

          icedos.ai-tools.peon-ping.users = icedosLib.users.genDefaults {
            inherit (config.icedos) users;
          };

          home-manager.sharedModules = [
            inputs.peon-ping.homeManagerModules.default
            (
              {
                config,
                lib,
                pkgs,
                ...
              }:

              let
                peonUserCfg = peonUsers.${config.home.username} or null;
              in
              lib.mkIf (peonUserCfg != null) {
                programs.peon-ping = {
                  enable = true;
                  package = peonPkg;
                  claudeCodeIntegration = peonUserCfg.claudeCodeIntegration;
                  settings = renderPeonSettings peonUserCfg;
                  installPacks = renderInstallPacks peonUserCfg;
                };

                # Upstream's claudeCodeIntegration merge writes ~/.claude/settings.json,
                # but hm's programs.claude-code materialises it as a read-only store
                # symlink — replace it with a writable copy before the merge.
                home.activation.icedosPeonSettingsWritable = lib.mkIf peonUserCfg.claudeCodeIntegration (
                  lib.hm.dag.entryBetween [ "peonPingClaudeCodeHooks" ] [ "linkGeneration" ] ''
                    if [ -e "$HOME/.claude/settings.json" ] || [ -L "$HOME/.claude/settings.json" ]; then
                      run ${pkgs.coreutils}/bin/install -m644 "$HOME/.claude/settings.json" "$HOME/.claude/settings.json.new"
                      run ${pkgs.coreutils}/bin/mv -f "$HOME/.claude/settings.json.new" "$HOME/.claude/settings.json"
                    fi
                  ''
                );
              }
            )
          ];

          icedos.system.tips.list = [
            "peon-ping plays configured sounds when your AI assistant needs you."
          ];
        }
      )
    ];

  meta.name = "peon-ping";
}
