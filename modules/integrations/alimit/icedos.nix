{ icedosLib, lib, ... }:

{
  options.icedos.ai-tools.alimit.users =
    let
      inherit (lib) importTOML;

      inherit (icedosLib)
        mkBoolOption
        mkEnumOption
        mkIntBetweenOption
        mkNumberOption
        mkStrListOption
        mkStrOption
        mkSubmoduleAttrsOption
        ;

      inherit ((importTOML ./config.toml).icedos.ai-tools.alimit.users.username)
        alertTimeout
        alertTransient
        alertUrgency
        alerts
        interval
        keybind
        providers
        statusLine
        widget
        ;
    in
    mkSubmoduleAttrsOption { default = { }; } {
      interval = mkNumberOption { default = interval; };
      alerts = mkBoolOption { default = alerts; };
      widget = mkBoolOption { default = widget; };

      # alimit's usage bar in Claude Code's status line, prime-agent's footer and opencode's sidebar.
      statusLine = mkBoolOption { default = statusLine; };

      # Providers the timer polls: claude, opencode-go, ollama, openrouter.
      providers = mkStrListOption { default = providers; };

      # Plasma never expires critical urgency: alertUrgency = "critical" + alertTimeout = 0 is sticky.
      alertUrgency =
        mkEnumOption
          {
            path = "icedos.ai-tools.alimit.users.username.alertUrgency";
            source = ./config.toml;
            default = alertUrgency;
          }
          [
            "critical"
            "low"
            "normal"
          ];

      # Seconds on screen; 0 means "until dismissed".
      alertTimeout = mkIntBetweenOption {
        path = "icedos.ai-tools.alimit.users.username.alertTimeout";
        source = ./config.toml;
        default = alertTimeout;
      } 0 300;

      # Popup only, skip the notification history.
      alertTransient = mkBoolOption { default = alertTransient; };

      # Zed keybind to spawn the alimit dashboard.
      keybind = mkStrOption { default = keybind; };
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
          inherit (lib)
            any
            attrValues
            mkDefault
            mkIf
            optional
            optionalAttrs
            replaceStrings
            ;

          alimitUsers = config.icedos.ai-tools.alimit.users;
          alimitPkg = pkgs.callPackage ./package.nix { };
          alimit = lib.getExe alimitPkg;
          pollable = [
            "claude"
            "opencode-go"
            "ollama"
            "openrouter"
          ];

          hasPrimeAgent = icedosLib.hasModule {
            inherit config repoUrl;
            name = "prime-agent";
          };

          # cost-footer already shows the opencode-go plan rows.
          primeAgentSkip = optional (
            hasPrimeAgent && config.icedos.ai-tools.prime-agent.extensions.meters.cost
          ) "opencode-go";

          primeAgentExtension =
            replaceStrings [ "@alimit@" "@skip@" ] [ alimit (builtins.toJSON primeAgentSkip) ]
              (builtins.readFile ./prime-agent.ts);

          opencodePlugin = pkgs.writeText "alimit-opencode.tsx" (
            replaceStrings [ "@alimit@" ] [ alimit ] (builtins.readFile ./opencode.tsx)
          );

          # KDE Plasma 6 applet rendering `status --json` (store path baked in via
          # @alimit@). It only reads the DB the timer and status lines fill; no network.
          alimitPlasmoid = pkgs.stdenvNoCC.mkDerivation {
            pname = "alimit-plasmoid";
            version = "0.2.0";
            src = ./plasmoid;
            dontConfigure = true;
            dontBuild = true;
            installPhase = ''
              runHook preInstall
              dst="$out/share/plasma/plasmoids/org.icedos.alimit"
              mkdir -p "$dst"
              cp -r ./* "$dst/"
              substituteInPlace "$dst/contents/ui/main.qml" \
                --replace-fail '@alimit@' '${alimit}'
              runHook postInstall
            '';
            meta.description = "alimit KDE Plasma 6 widget";
          };
        in
        {
          icedos.ai-tools = {
            alimit.users = icedosLib.users.genDefaults {
              inherit (config.icedos) users;
            };
          }
          // optionalAttrs hasPrimeAgent {
            prime-agent.extensions.inline.alimit = mkIf (any (u: u.statusLine) (attrValues alimitUsers)) (
              mkDefault primeAgentExtension
            );
          };

          icedos.system.toolset.commands = mkIf (alimitUsers != { }) [
            {
              command = "ai";
              help = "AI tooling";

              commands = [
                {
                  command = "limits";
                  bin = alimit;
                  help = "live AI usage-limit dashboard (every agent and provider, burn rate)";
                }
              ];
            }
          ];

          home-manager.sharedModules = [
            (
              { config, lib, ... }:

              let
                userCfg = alimitUsers.${config.home.username} or null;
              in
              mkIf (userCfg != null) {
                assertions = [
                  {
                    # The Claude usage endpoint answers a faster poller with a persistent 429.
                    assertion = userCfg.interval >= 180;
                    message =
                      "alimit: interval for ${config.home.username} is ${toString userCfg.interval}s;"
                      + " it must be ≥ 180 (the /api/oauth/usage rate-limit floor).";
                  }
                  {
                    assertion = lib.all (p: lib.elem p pollable) userCfg.providers;
                    message =
                      "alimit: providers for ${config.home.username} must be among "
                      + lib.concatStringsSep ", " pollable
                      + " (antigravity is recorded by its own status line).";
                  }
                ];

                # plasmashell finds plasmoids via XDG_DATA_DIRS, which home.packages populates.
                # Add via "Add Widgets", or pin "org.icedos.alimit" in icedos.desktop.kde.panel.widgets.
                home.packages = [ alimitPkg ] ++ optional userCfg.widget alimitPlasmoid;

                # Status lines record samples while agents run; this timer covers idle periods.
                systemd.user.services.alimit = {
                  Unit.Description = "alimit: fetch AI usage limits once";

                  Service = {
                    Type = "oneshot";
                    Environment = [ "ALIMIT_PROVIDERS=${lib.concatStringsSep "," userCfg.providers}" ];
                    ExecStart =
                      "${alimit} poll"
                      + (
                        if !userCfg.alerts then
                          " --no-alerts"
                        else
                          " --alert-urgency ${userCfg.alertUrgency}"
                          + " --alert-timeout ${toString userCfg.alertTimeout}"
                          + lib.optionalString userCfg.alertTransient " --alert-transient"
                      );
                  };
                };

                systemd.user.timers.alimit = {
                  Unit.Description = "alimit: AI usage-limit poll timer";

                  Timer = {
                    OnStartupSec = "30s";
                    OnUnitActiveSec = "${toString userCfg.interval}s";
                    AccuracySec = "1s";
                  };

                  Install.WantedBy = [ "timers.target" ];
                };

                programs.claude-code.settings.statusLine = mkIf userCfg.statusLine {
                  type = "command";
                  command = "${alimit} statusline";
                  padding = 0;
                  # Redraw between model responses so timer samples and reset countdowns show up.
                  refreshInterval = 60;
                };

                programs.opencode.tui.plugin = mkIf userCfg.statusLine [ "${opencodePlugin}" ];

                # Live usage view as a Zed task (bottom terminal dock), when Zed is in use.
                programs.zed-editor.userTasks = mkIf (config.programs.zed-editor.enable or false) [
                  {
                    label = "alimit";
                    command = "icedos";
                    args = [
                      "ai"
                      "limits"
                    ];
                    use_new_terminal = false;
                    allow_concurrent_runs = false;
                    reveal = "always";
                    reveal_target = "dock";
                    hide = "never";
                  }
                ];

                programs.zed-editor.userKeymaps = mkIf (config.programs.zed-editor.enable or false) [
                  {
                    context = "Workspace";
                    bindings = {
                      ${userCfg.keybind} = [
                        "task::Spawn"
                        {
                          task_name = "alimit";
                        }
                      ];
                    };
                  }
                ];
              }
            )
          ];
        }
      )
    ];

  meta.name = "alimit";
}
