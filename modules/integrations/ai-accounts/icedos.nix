{ icedosLib, lib, ... }:

{
  options.icedos.ai-tools.ai-accounts =
    let
      inherit (icedosLib)
        mkAttrsOption
        mkEnumOption
        mkIntBetweenOption
        mkStrListOption
        mkStrOption
        mkSubmoduleAttrsOption
        ;

      toml = (lib.importTOML ./config.toml).icedos.ai-tools.ai-accounts;
      user = toml.users.username;
      account = user.agents.agentname.accounts.accountname;
    in
    {
      antigravity.busTalk = mkStrListOption { default = toml.antigravity.busTalk; };

      users = mkSubmoduleAttrsOption { default = { }; } {
        select =
          mkEnumOption
            {
              path = "icedos.ai-tools.ai-accounts.users.username.select";
              source = ./config.toml;
              default = user.select;
            }
            [
              "auto"
              "manual"
            ];

        threshold = mkIntBetweenOption {
          path = "icedos.ai-tools.ai-accounts.users.username.threshold";
          source = ./config.toml;
          default = user.threshold;
        } 1 100;

        agents = mkSubmoduleAttrsOption { default = { }; } {
          accounts = mkSubmoduleAttrsOption { default = { }; } {
            mode =
              mkEnumOption
                {
                  path = "icedos.ai-tools.ai-accounts.users.username.agents.agentname.accounts.accountname.mode";
                  source = ./config.toml;
                  default = account.mode;
                }
                [
                  "isolated"
                  "shared"
                ];

            label = mkStrOption { default = account.label; };

            # Keys merged over the agent's own settings file for this account only.
            settings = mkAttrsOption { default = account.settings or { }; };
          };
        };
      };
    };

  outputs.nixosModules =
    { repoUrl, ... }:
    [
      (
        {
          config,
          icedosLib,
          lib,
          pkgs,
          ...
        }:

        let
          cfg = config.icedos.ai-tools.ai-accounts;
          aiAccountPkg = pkgs.callPackage ./package.nix { };
          alimitPkg = pkgs.callPackage ../alimit/package.nix { };
          agents = [
            "claude"
            "prime-agent"
            "opencode"
            "antigravity"
            "ollama"
          ];
          validName = n: n != "default" && builtins.match "[a-z0-9][a-z0-9_-]*" n != null;
        in
        {
          icedos.ai-tools.ai-accounts.users = icedosLib.users.genDefaults {
            inherit (config.icedos) users;
          };

          home-manager.sharedModules = [
            (
              { config, lib, ... }:

              let
                userCfg = cfg.users.${config.home.username} or null;
                home = config.home.homeDirectory;
                realDirs = {
                  claude = "${home}/.claude";
                  prime-agent = "${config.xdg.configHome}/prime-agent";
                  opencode = "${config.xdg.dataHome}/opencode";
                  antigravity = home;
                  ollama = "";
                };
                bins = {
                  claude = lib.getExe pkgs.claude-code;
                  prime-agent = lib.getExe pkgs.prime-agent;
                  opencode = lib.getExe pkgs.opencode;
                  antigravity = "${pkgs.antigravity-cli}/bin/agy";
                  ollama = "";
                };
                accountNames = lib.concatMap (a: lib.attrNames a.accounts) (lib.attrValues userCfg.agents);
              in
              lib.mkIf (userCfg != null) {
                assertions = [
                  {
                    assertion = lib.all (a: lib.elem a agents) (lib.attrNames userCfg.agents);
                    message = "ai-accounts: agents must be among ${lib.concatStringsSep ", " agents}.";
                  }
                  {
                    assertion = lib.all validName accountNames;
                    message = "ai-accounts: account names match [a-z0-9][a-z0-9_-]* and are not \"default\".";
                  }
                ];

                home.packages = [ aiAccountPkg ];

                xdg.configFile."ai-accounts/config.json".text = builtins.toJSON {
                  inherit (userCfg) select threshold;
                  busTalk = cfg.antigravity.busTalk;
                  alimit = lib.getExe alimitPkg;
                  agents = lib.mapAttrs (name: a: {
                    realDir = realDirs.${name} or "";
                    bin = bins.${name} or "";
                    settings = config.icedos.managedSettings.${name} or { };
                    accounts = lib.mapAttrs (_: acc: { inherit (acc) mode label settings; }) a.accounts;
                  }) userCfg.agents;
                };
              }
            )
          ];
        }
      )
    ];

  meta.name = "ai-accounts";
}
