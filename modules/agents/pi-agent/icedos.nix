{ icedosLib, lib, ... }:

{
  options.icedos.ai-tools.pi-agent =
    let
      inherit (lib) importTOML types;

      inherit (icedosLib)
        mkAttrsOfOption
        mkAttrsOption
        mkBoolOption
        mkIntBetweenOption
        mkNumberOption
        mkStrListOption
        mkStrOption
        mkSubmoduleAttrsOption
        mkSubmoduleListOption
        ;

      defaults = (importTOML ./config.toml).icedos.ai-tools.pi-agent;
    in
    {
      desktopEntry = mkBoolOption { default = defaults.desktopEntry; };
      includeInIcedosGc = mkBoolOption { default = defaults.includeInIcedosGc; };

      sessionRetentionDays = mkIntBetweenOption {
        path = "icedos.ai-tools.pi-agent.sessionRetentionDays";
        source = ./config.toml;
        default = defaults.sessionRetentionDays;
      } 1 3650;

      default = {
        provider = mkStrOption { default = defaults.default.provider; };
        model = mkStrOption { default = defaults.default.model; };
      };

      rules = mkAttrsOfOption { default = defaults.rules; } types.lines;

      # name = value, exported to pi's environment via the wrapper (e.g. OPENCODE_API_KEY).
      environmentVariables = mkAttrsOption { default = { }; };

      packages = mkStrListOption { default = defaults.packages; };

      skills = {
        dirs = mkStrListOption { default = defaults.skills.dirs; };
        extra-builtin = mkAttrsOfOption { default = defaults.skills.extra-builtin; } types.str;
        codeReview = mkBoolOption { default = defaults.skills.codeReview; };
      };

      settings = {
        dataDir = mkStrOption { default = defaults.settings.dataDir; };

        mcpCallTimeout = mkIntBetweenOption {
          path = "icedos.ai-tools.pi-agent.settings.mcpCallTimeout";
          source = ./config.toml;
          default = defaults.settings.mcpCallTimeout;
        } 1 86400;

        providers = mkSubmoduleAttrsOption { default = defaults.settings.providers; } (
          import ./lib/providers-options.nix { inherit lib; }
        );

        extra = mkAttrsOption { default = defaults.settings.extra; };
      };

      extensions =
        let
          e = defaults.extensions;
          c = e.context-language-models;
          p = e.meters.power;
        in
        {
          builtin = mkStrListOption { default = e.builtin; };
          inline = mkAttrsOfOption { default = e.inline; } types.str;

          context-window-cap = {
            enable = mkBoolOption { default = e.context-window-cap.enable; };

            percent = mkIntBetweenOption {
              path = "icedos.ai-tools.pi-agent.extensions.context-window-cap.percent";
              source = ./config.toml;
              default = e.context-window-cap.percent;
            } 1 100;
          };

          opencodeFree = mkBoolOption { default = e.opencodeFree; };

          vpn-rotate = {
            enable = mkBoolOption { default = e.vpn-rotate.enable; };
            providers = mkStrListOption { default = e.vpn-rotate.providers; };
            countries = mkStrListOption { default = e.vpn-rotate.countries; };

            cooldownSeconds = mkIntBetweenOption {
              path = "icedos.ai-tools.pi-agent.extensions.vpn-rotate.cooldownSeconds";
              source = ./config.toml;
              default = e.vpn-rotate.cooldownSeconds;
            } 0 86400;

            maxPerHour = mkIntBetweenOption {
              path = "icedos.ai-tools.pi-agent.extensions.vpn-rotate.maxPerHour";
              source = ./config.toml;
              default = e.vpn-rotate.maxPerHour;
            } 1 60;
          };

          ghost-text = {
            enable = mkBoolOption { default = e.ghost-text.enable; };
            version = mkStrOption { default = e.ghost-text.version; };
          };

          session-name = {
            enable = mkBoolOption { default = e.session-name.enable; };
            version = mkStrOption { default = e.session-name.version; };
          };

          context-language-models = {
            enable = mkBoolOption { default = c.enable; };
            version = mkStrOption { default = c.version; };
            budget = mkStrOption { default = c.budget; };
            reserve = mkStrOption { default = c.reserve; };
            remindAt = mkStrOption { default = c.remindAt; };
            observationCap = mkStrOption { default = c.observationCap; };
            steering = mkStrOption { default = c.steering; };
            overflow = mkStrOption { default = c.overflow; };
            estimateFactor = mkStrOption { default = c.estimateFactor; };
            oneToolPerTurn = mkBoolOption { default = c.oneToolPerTurn; };
            sizeTrailer = mkBoolOption { default = c.sizeTrailer; };
            nativeCompaction = mkStrOption { default = c.nativeCompaction; };
            compactPrompt = mkStrOption { default = c.compactPrompt; };
          };

          recursive-language-models = {
            enable = mkBoolOption { default = e.recursive-language-models.enable; };
            version = mkStrOption { default = e.recursive-language-models.version; };

            maxDepth = mkIntBetweenOption {
              path = "icedos.ai-tools.pi-agent.extensions.recursive-language-models.maxDepth";
              source = ./config.toml;
              default = e.recursive-language-models.maxDepth;
            } 0 8;
          };

          meters = {
            cost = mkBoolOption { default = e.meters.cost; };
            speed = mkBoolOption { default = e.meters.speed; };

            power = {
              enable = mkBoolOption { default = p.enable; };
              card = mkStrOption { default = p.card; };
              rateKwh = mkNumberOption { default = p.rateKwh; };
              idleWatts = mkNumberOption { default = p.idleWatts; };
              currency = mkStrOption { default = p.currency; };
              providers = mkStrListOption { default = p.providers; };
              windows = mkStrListOption { default = p.windows; };
            };
          };
        };

      hooks.rateLimit = mkSubmoduleListOption { default = defaults.hooks.rateLimit; } {
        command = mkStrListOption { };
        providers = mkStrListOption { };
        errorKinds = mkStrListOption { default = [ "rate_limit" ]; };

        timeoutMs = mkIntBetweenOption {
          path = "icedos.ai-tools.pi-agent.hooks.rateLimit.timeoutMs";
          source = ./config.toml;
          default = 0;
        } 0 3600000;
      };

      users = mkSubmoduleAttrsOption { default = { }; } { };
    };

  outputs.nixosModules =
    { repoUrl, ... }:
    [
      (
        { config, pkgs, ... }:

        let
          cfg = config.icedos.ai-tools.pi-agent;

          # Shared entries first, so a same-named pi-agent entry wins.
          rules =
            config.icedos.ai-tools.rules
            // lib.optionalAttrs vpn.enable {
              vpn-rotate = "When a provider rate-limits a request, the agent may rotate the ProtonVPN server and retry on its own. Do not run protonvpn connect or disconnect yourself.";
            }
            // cfg.rules;
          # The bundled review drives rlm() children, so it needs pi-rlm.
          skills =
            lib.optionalAttrs (rlm.enable && cfg.skills.codeReview) {
              code-review = builtins.readFile ./skills/code-review/SKILL.md;
            }
            // config.icedos.ai-tools.skills
            // cfg.skills.extra-builtin;

          skillName = import ./lib/skill-name.nix { inherit lib; };
          invalidSkillNames = skillName.invalidSkillNames (lib.attrNames skills);

          skillsDir = pkgs.linkFarm "pi-agent-skills" (
            lib.mapAttrsToList (name: content: {
              name = "${name}/SKILL.md";
              path = pkgs.writeText "${name}-SKILL.md" content;
            }) skills
          );

          dataDirLib = import ./lib/data-dir.nix { inherit lib; };
          mcpJson = import ./lib/mcp-json.nix { inherit lib; };

          hasAccounts = import ../../integrations/ai-accounts/lib/has-accounts.nix { inherit lib; } config;
          wrapAccounts = import ../../integrations/ai-accounts/lib/wrap.nix { inherit lib pkgs; };
          aiAccountsOn = icedosLib.hasModule {
            inherit config repoUrl;
            name = "ai-accounts";
          };

          providers = lib.mapAttrs (
            _: p:
            let
              attrs =
                lib.filterAttrs (_: v: v != null) { inherit (p) api apiKey baseUrl; }
                // lib.optionalAttrs (p.headers != { }) { inherit (p) headers; };
              overrides = lib.mapAttrs (_: o: lib.filterAttrs (_: v: v != null) o) p.modelOverrides;
              models = map (m: lib.filterAttrs (_: v: v != null) m) p.models;
            in
            attrs
            // lib.optionalAttrs (overrides != { }) { modelOverrides = overrides; }
            // lib.optionalAttrs (models != [ ]) { inherit models; }
          ) cfg.settings.providers;

          ext = cfg.extensions;
          extLib = import ./lib/extensions.nix { inherit lib; };

          # pathExists on the package forces it at eval time, as 0.9.8 did.
          examplesDir = "${pkgs.pi-coding-agent}/lib/node_modules/pi-monorepo/examples/extensions";
          missingBuiltins = extLib.missingBuiltins {
            inherit examplesDir;
            names = ext.builtin;
            exists = builtins.pathExists;
          };
          builtinPaths = lib.optionals (missingBuiltins == [ ]) (
            map (
              name:
              extLib.resolveBuiltin {
                inherit examplesDir name;
                exists = builtins.pathExists;
              }
            ) ext.builtin
          );

          invalidInline = extLib.invalidInlineNames (lib.attrNames ext.inline);
          invalidWindows = extLib.invalidWindows ext.meters.power.windows;

          # Markers sit inside TS string literals, so values need JS escaping.
          jsEscape = lib.replaceStrings [ "\\" "\"" "\n" "\r" ] [ "\\\\" "\\\"" "\\n" "\\r" ];

          # Copies a .ts file or dir, fills @key@ markers and fails on any marker left.
          substituteTs =
            name: src: vars:
            pkgs.runCommand name { } ''
              cp -r ${src} $out
              chmod -R u+w $out
              find $out -type f -name '*.ts' | while read -r f; do
                substituteInPlace "$f" ${
                  lib.concatStringsSep " " (
                    lib.mapAttrsToList (
                      k: v: "--replace-quiet ${lib.escapeShellArg "@${k}@"} ${lib.escapeShellArg (jsEscape v)}"
                    ) vars
                  )
                }
              done
              if ${pkgs.gnugrep}/bin/grep -rqE '@[a-zA-Z_][0-9A-Za-z_-]*@' $out; then
                echo "unsubstituted placeholder left in ${name}" >&2
                exit 1
              fi
            '';

          hasVigor = icedosLib.hasModule {
            inherit config;
            url = "github:icedos/kde";
            name = "vigor";
          };

          # Vigor's tariff fills what the meter leaves unset. Flat, since vigor applies its bands itself while running.
          power =
            ext.meters.power
            // lib.optionalAttrs hasVigor (
              let
                own = ext.meters.power;
                vigor = config.icedos.desktop.kde.vigor;
              in
              {
                currency = if own.currency != "" then own.currency else vigor.currency;
                idleWatts = if own.idleWatts != 0 then own.idleWatts else vigor.gpuIdleWatts;
                windows = if own.windows != [ ] then own.windows else vigor.windows;
                rateKwh =
                  if own.rateKwh != 0 then own.rateKwh else (vigor.price + vigor.surcharge) * (1 + vigor.vat / 100.0);
              }
            );
          clm = ext.context-language-models;
          rlm = ext.recursive-language-models;

          # Option key under extensions = npm package.
          optionPackages = lib.filter (p: ext.${p.key}.enable) [
            {
              key = "ghost-text";
              npm = "@apothic-ai/pi-ghost-text-x";
            }
            {
              key = "session-name";
              npm = "pi-session-name";
            }
            {
              key = "context-language-models";
              npm = "@lolipopshock/pi-clm";
            }
            {
              key = "recursive-language-models";
              npm = "@florianguyonnet/pi-rlm";
            }
          ];

          pkgSource = name: version: "npm:${name}" + lib.optionalString (version != "") "@${version}";
          piPackages = cfg.packages ++ map (p: pkgSource p.npm ext.${p.key}.version) optionPackages;

          versionPattern = "[0-9]+\\.[0-9]+\\.[0-9]+(-[0-9A-Za-z.-]+)?";
          badVersions = lib.filter (x: x.value != "" && builtins.match versionPattern x.value == null) (
            map (p: {
              value = ext.${p.key}.version;
              path = "icedos.ai-tools.pi-agent.extensions.${p.key}.version";
            }) optionPackages
          );

          vpn = ext.vpn-rotate;

          vpnRotateScript = pkgs.writers.writePython3Bin "pi-vpn-rotate-unwrapped" {
            flakeIgnore = [ "E501" ];
          } (builtins.readFile ./lib/pi_vpn_rotate.py);

          vpnRotate = pkgs.runCommand "pi-vpn-rotate" { nativeBuildInputs = [ pkgs.makeWrapper ]; } ''
            makeWrapper ${vpnRotateScript}/bin/pi-vpn-rotate-unwrapped $out/bin/pi-vpn-rotate \
              --set PI_VPN_ROTATE_PROTONVPN ${pkgs.proton-vpn-cli}/bin/protonvpn \
              --set PI_VPN_ROTATE_COOLDOWN_S ${toString vpn.cooldownSeconds} \
              --set PI_VPN_ROTATE_MAX_PER_HOUR ${toString vpn.maxPerHour} \
              --set PI_VPN_ROTATE_COUNTRIES ${lib.escapeShellArg (lib.concatStringsSep "," vpn.countries)}
          '';

          vpnRotateHook = {
            command = [ "${vpnRotate}/bin/pi-vpn-rotate" ];
            inherit (vpn) providers;
            errorKinds = [
              "rate_limit"
              "usage_limit"
            ];
            timeoutMs = 310000;
          };

          rateLimitHooks =
            map (h: {
              inherit (h) command providers errorKinds;
              timeoutMs = if h.timeoutMs == 0 then 120000 else h.timeoutMs;
            }) cfg.hooks.rateLimit
            ++ lib.optional vpn.enable vpnRotateHook;
          errorKindNames = [
            "rate_limit"
            "usage_limit"
            "not_found"
            "timeout"
          ];

          shipped =
            lib.optional ext.meters.cost (
              substituteTs "pi-agent-cost-footer" ./extensions/cost-footer {
                speed = lib.boolToString ext.meters.speed;
                powerCard = power.card;
                powerRate = builtins.toJSON power.rateKwh;
                powerIdle = builtins.toJSON power.idleWatts;
                powerCurrency = power.currency;
                powerWindows = lib.concatStringsSep "," (
                  if power.windows == [ ] then
                    [
                      "1h"
                      "24h"
                      "7d"
                      "30d"
                    ]
                  else
                    power.windows
                );
                powerProviders = lib.concatStringsSep "," (lib.optionals power.enable power.providers);
              }
            )
            ++ lib.optional ext.context-window-cap.enable (
              substituteTs "context-cap.ts" ./extensions/context-cap.ts {
                contextWindowPercent = toString ext.context-window-cap.percent;
              }
            )
            ++ lib.optional (rateLimitHooks != [ ]) (
              substituteTs "rate-limit-hook.ts" ./extensions/rate-limit-hook.ts {
                rateLimitHooks = builtins.toJSON rateLimitHooks;
              }
            )
            ++ lib.optional ext.opencodeFree (
              substituteTs "opencode-free.ts" ./extensions/opencode-free.ts {
                bunBin = "${pkgs.bun}/bin/bun";
              }
            )
            ++ lib.optional vpn.enable (
              pkgs.writeText "vpn-row.ts" (builtins.readFile ./extensions/vpn-row.ts)
            );

          # Invalid names are asserted below; filtering keeps writeText from failing first.
          inlinePaths = lib.mapAttrs (name: text: pkgs.writeText "${name}.ts" text) (
            lib.filterAttrs (name: _: !(lib.elem name invalidInline)) ext.inline
          );

          extensionPaths = map toString (
            extLib.order {
              builtin = builtinPaths;
              inherit shipped;
              inline = inlinePaths;
            }
          );

          clmEnv = {
            PI_CLM_NATIVE_COMPACTION = clm.nativeCompaction;
            PI_CLM_OVERFLOW = clm.overflow;
            PI_CLM_ONE_TOOL_PER_TURN = if clm.oneToolPerTurn then "1" else "0";
            PI_CLM_SIZE_TRAILER = if clm.sizeTrailer then "1" else "0";
          }
          // lib.filterAttrs (_: v: v != "") {
            PI_CLM_STEERING = clm.steering;
            PI_CLM_BUDGET = clm.budget;
            PI_CLM_RESERVE = clm.reserve;
            PI_CLM_REMIND_AT = clm.remindAt;
            PI_CLM_OBSERVATION_CAP = clm.observationCap;
            PI_CLM_ESTIMATE_FACTOR = clm.estimateFactor;
            PI_CLM_COMPACT_PROMPT = clm.compactPrompt;
          };

          clmWrapperArgs = lib.optionals clm.enable (
            lib.concatLists (
              lib.mapAttrsToList (k: v: [
                "--set-default"
                k
                v
              ]) clmEnv
            )
            # pi installs packages under <agentDir>/npm/node_modules; the wrapper sets PI_CODING_AGENT_DIR first.
            ++ lib.optionals (clm.steering == "") [
              "--run"
              "export PI_CLM_STEERING=\"\${PI_CLM_STEERING:-$PI_CODING_AGENT_DIR/npm/node_modules/@lolipopshock/pi-clm/steering/house-brief.md}\""
            ]
          );

          rlmWrapperArgs = lib.optionals rlm.enable [
            "--set-default"
            "PI_RLM_PYTHON"
            "${pkgs.python3}/bin/python3"
            "--set-default"
            "PI_RLM_MAX_DEPTH"
            (toString rlm.maxDepth)
          ];

          # pi hides a provider without an API key; the gateway's free tier takes "public".
          envWrapperArgs = lib.concatLists (
            lib.mapAttrsToList
              (k: v: [
                "--set-default"
                k
                v
              ])
              (
                lib.optionalAttrs ext.opencodeFree { PI_OPENCODE_FREE_API_KEY = "public"; }
                // cfg.environmentVariables
              )
          );

          seedSettings =
            cfg.settings.extra
            // {
              skills = [ "${skillsDir}" ] ++ cfg.skills.dirs;
              extensions = extensionPaths;
            }
            // lib.optionalAttrs (cfg.default.provider != "") { defaultProvider = cfg.default.provider; }
            // lib.optionalAttrs (cfg.default.model != "") { defaultModel = cfg.default.model; }
            # pi runs `npm` from PATH by default, and the system has none there.
            // lib.optionalAttrs (piPackages != [ ]) {
              packages = piPackages;
              npmCommand = [ "${pkgs.nodejs}/bin/npm" ];
            };

          # Runs per user under a minimal env that only has HOME.
          gcHook = ''
            D=${lib.escapeShellArg cfg.settings.dataDir}
            [ -z "$D" ] && D="$HOME/.config/pi-agent"
            D=''${D/\$XDG_CONFIG_HOME/$HOME/.config}
            D=''${D/\$XDG_DATA_HOME/$HOME/.local/share}
            D=''${D/#\~/$HOME}
            D=''${D//\$HOME/$HOME}
            [ -d "$D" ] || { log_warn "pi-agent gc: data dir $D not found; skipping"; exit 0; }
            find "$D/sessions" -mindepth 2 -maxdepth 2 -type f -name '*.jsonl' -mtime "+${toString cfg.sessionRetentionDays}" -delete 2>/dev/null || true
            find "$D/sessions" -mindepth 1 -maxdepth 1 -type d -empty -delete 2>/dev/null || true
            find "$D/pi-rlm/snapshots" -maxdepth 1 -type f -name '*.pickle' -mtime "+${toString cfg.sessionRetentionDays}" -delete 2>/dev/null || true
          '';

          settingsSeed = pkgs.writeText "pi-agent-settings.json" (builtins.toJSON seedSettings);
          modelsSeed = pkgs.writeText "pi-agent-models.json" (builtins.toJSON { inherit providers; });
        in
        {
          assertions = [
            {
              assertion = invalidSkillNames == [ ];
              message = ''
                pi-agent skill names (icedos.ai-tools.skills and pi-agent.skills.extra-builtin) must be
                lowercase a-z, 0-9 and single hyphens, at most 64 chars: ${lib.concatStringsSep ", " invalidSkillNames}
              '';
            }
            {
              assertion = missingBuiltins == [ ];
              message = ''
                icedos.ai-tools.pi-agent.extensions.builtin names not found in pi's examples/extensions:
                ${lib.concatStringsSep ", " missingBuiltins}
              '';
            }
            {
              assertion = invalidInline == [ ];
              message = ''
                icedos.ai-tools.pi-agent.extensions.inline names must match [A-Za-z0-9_-]+:
                ${lib.concatStringsSep ", " invalidInline}
              '';
            }
            {
              # llamacpp adds itself to providers; without one the meter records nothing.
              assertion =
                !power.enable
                || (ext.meters.cost && power.providers != [ ] && power.rateKwh > 0 && power.currency != "");
              message = ''
                icedos.ai-tools.pi-agent.extensions.meters.power.enable needs meters.cost = true,
                a non-empty providers list, rateKwh > 0 and a currency.
              '';
            }
            {
              assertion = invalidWindows == [ ];
              message = ''
                icedos.ai-tools.pi-agent.extensions.meters.power.windows entries must be a positive
                integer plus s, m, h, d, w or M: ${lib.concatStringsSep ", " invalidWindows}
              '';
            }
            {
              assertion = lib.all (p: builtins.match "(npm:|git:|https?://|/|\\./).+" p != null) cfg.packages;
              message = ''
                icedos.ai-tools.pi-agent.packages entries must start with npm:, git:, http(s)://, / or ./:
                ${lib.concatStringsSep ", " cfg.packages}
              '';
            }
            {
              assertion = lib.all (h: h.command != [ ] && h.providers != [ ]) cfg.hooks.rateLimit;
              message = "Every icedos.ai-tools.pi-agent.hooks.rateLimit entry needs a non-empty command and providers.";
            }
            {
              assertion = lib.all (
                h: h.errorKinds != [ ] && lib.all (k: lib.elem k errorKindNames) h.errorKinds
              ) cfg.hooks.rateLimit;
              message = ''
                icedos.ai-tools.pi-agent.hooks.rateLimit errorKinds must be a non-empty list of
                ${lib.concatStringsSep ", " errorKindNames}.
              '';
            }
            {
              assertion = !vpn.enable || vpn.providers != [ ];
              message = "icedos.ai-tools.pi-agent.extensions.vpn-rotate.providers must name at least one provider.";
            }
            {
              assertion = badVersions == [ ];
              message = ''
                Pinned pi package versions must be an exact version such as 1.0.0, or "" for unpinned:
                ${lib.concatMapStringsSep ", " (x: "${x.path} = \"${x.value}\"") badVersions}
              '';
            }
          ];

          nixpkgs.overlays = [
            (final: _prev: {
              pi-agent = final.callPackage ./package.nix {
                inherit (icedosLib.packaging) installDesktopEntry;
                inherit (cfg) desktopEntry;
                defaultAgentDir = dataDirLib.shell cfg.settings.dataDir;
                extraWrapperArgs = clmWrapperArgs ++ rlmWrapperArgs ++ envWrapperArgs;
              };
            })
          ];

          environment.systemPackages = [
            (
              if aiAccountsOn && hasAccounts "pi-agent" then
                wrapAccounts {
                  agent = "pi-agent";
                  package = pkgs.pi-agent;
                  bin = "pi";
                }
              else
                pkgs.pi-agent
            )
          ]
          ++ lib.optional vpn.enable pkgs.proton-vpn-cli;

          icedos.ai-tools.pi-agent.users = icedosLib.users.genDefaults {
            inherit (config.icedos) users;
          };

          icedos.system.gc.hooks.postGc = lib.mkIf cfg.includeInIcedosGc [ gcHook ];

          icedos.system.toolset.commands = lib.mkIf (cfg.users != { }) [
            {
              command = "pi-agent";
              bin = "${pkgs.pi-agent}/bin/pi";
              help = "pi coding agent (interactive session)";
            }
          ];

          icedos.ai-tools.agent-launch.agents.pi-agent = {
            command = lib.mkDefault [ "pi" ];
            locator = lib.mkDefault "pi-agent";
            resumeArgs = lib.mkDefault [
              "--session"
              "{id}"
            ];
            label = lib.mkDefault "Pi";
          };

          home-manager.sharedModules = [
            (
              # home-manager's own lib: only it has lib.hm.
              {
                config,
                lib,
                pkgs,
                ...
              }:

              let
                userCfg = cfg.users.${config.home.username} or null;

                dataDir = dataDirLib.expand {
                  raw = cfg.settings.dataDir;
                  home = config.home.homeDirectory;
                  configHome = config.xdg.configHome;
                  dataHome = config.xdg.dataHome;
                };
                relDataDir = lib.removePrefix (config.home.homeDirectory + "/") dataDir;

                registry = if config.programs.mcp.enable or false then config.programs.mcp.servers or { } else { };
                invalidServers = mcpJson.invalidNames registry;

                rulesText = lib.concatStringsSep "\n\n" (lib.attrValues rules);
              in
              {
                options.icedos.managed-settings.pi-agent = icedosLib.mkAttrsOption {
                  default = seedSettings;
                };

                config = lib.mkIf (userCfg != null) {
                  assertions = [
                    {
                      assertion = lib.hasPrefix (config.home.homeDirectory + "/") dataDir && relDataDir != "";
                      message = ''
                        icedos.ai-tools.pi-agent.settings.dataDir must expand to a path inside the home
                        directory (got "${dataDir}" from "${cfg.settings.dataDir}").
                      '';
                    }
                    {
                      assertion = invalidServers == [ ];
                      message = ''
                        pi accepts only letters, digits, _ and - in MCP server names: ${lib.concatStringsSep ", " invalidServers}
                      '';
                    }
                  ];

                  home.file = lib.mkMerge [
                    {
                      "${relDataDir}/mcp.json".text = builtins.toJSON (
                        mcpJson.render {
                          servers = registry;
                          timeout = cfg.settings.mcpCallTimeout;
                        }
                      );
                    }
                    (lib.mkIf (rulesText != "") {
                      "${relDataDir}/APPEND_SYSTEM.md".text = rulesText;
                    })
                  ];

                  home.activation.seed-pi-agent = lib.hm.dag.entryAfter [ "writeBoundary" ] ''
                    PATH=${
                      lib.makeBinPath [
                        pkgs.jq
                        pkgs.coreutils
                      ]
                    }:$PATH ${pkgs.bash}/bin/bash ${./lib/seed.sh} ${lib.escapeShellArg dataDir} ${settingsSeed} ${modelsSeed}
                  '';
                };
              }
            )
          ];

          icedos.system.tips.list =
            lib.optionals cfg.desktopEntry [
              "pi is in the app launcher; it opens in your default terminal."
            ]
            ++ lib.optionals (ext.meters.cost && ext.meters.speed) [
              "The pi footer shows output tok/s per response and a session average; /speed on/off toggles it."
            ]
            ++ lib.optionals cfg.includeInIcedosGc [
              "icedos gc also clears out old pi sessions."
            ];
        }
      )
    ];

  meta = {
    name = "pi-agent";
    dependencies = [ { modules = [ "agent-launch" ]; } ];
  };
}
