{ icedosLib, lib, ... }:

let
  inherit (lib) importTOML;
  inherit (icedosLib)
    mkAttrsOfOption
    mkBoolOption
    mkIntBetweenOption
    mkListOption
    mkStrListOption
    mkStrOption
    ;

  defaults = (importTOML ./config.toml).icedos.ai-tools.opencode;

  clm = import ./clm/module.nix {
    inherit icedosLib lib;
    defaults = defaults.extensions.context-language-models;
  };

  rlm = import ./rlm/module.nix {
    inherit icedosLib lib;
    defaults = defaults.extensions.recursive-language-models;
  };

  # npm plugin extensions; TUI-only packages go to cli.json, the rest to opencode.json.
  npmExtensions = {
    power = {
      package = "@log0u7/opencode-watt";
      tui = false;
    };
  };

  npmExtensionOptions =
    name:
    let
      d = defaults.extensions.${name};
    in
    {
      enable = mkBoolOption { default = d.enable; };
      version = mkStrOption { default = d.version; };
      options = mkAttrsOfOption { default = d.options; } lib.types.anything;
    };
in
{
  options.icedos.ai-tools.opencode = {
    desktopEntry = mkBoolOption { default = defaults.desktopEntry; };
    enableMcpIntegration = mkBoolOption { default = defaults.enableMcpIntegration; };
    extra-mcp-servers = mkAttrsOfOption { default = defaults.extra-mcp-servers; } lib.types.anything;
    launchWrapper = mkStrListOption { default = defaults.launchWrapper; };
    extraArgs = mkStrListOption { default = defaults.extraArgs; };
    rules = mkAttrsOfOption { default = defaults.rules; } lib.types.lines;
    skills = mkAttrsOfOption { default = defaults.skills; } lib.types.lines;
    excludeSkills = mkStrListOption { default = defaults.excludeSkills; };
    plugins = mkListOption { default = defaults.plugins; } (
      lib.types.either lib.types.str lib.types.attrs
    );
    settings = mkAttrsOfOption { default = defaults.settings; } lib.types.anything;
    cli = mkAttrsOfOption { default = defaults.cli; } lib.types.anything;

    terminal-title = {
      enable = mkBoolOption { default = defaults.terminal-title.enable; };
      idleGlyph = mkStrOption { default = defaults.terminal-title.idleGlyph; };
      workingGlyphs = mkStrListOption { default = defaults.terminal-title.workingGlyphs; };
    };

    turnEndMessage = mkBoolOption { default = defaults.turnEndMessage; };
    toolTimer = mkBoolOption { default = defaults.toolTimer; };

    codeReview = mkBoolOption { default = defaults.codeReview; };
    freeReview = mkBoolOption { default = defaults.freeReview; };

    includeInIcedosGc = mkBoolOption { default = defaults.includeInIcedosGc; };
    sessionRetentionDays = mkIntBetweenOption {
      path = "icedos.ai-tools.opencode.sessionRetentionDays";
      source = ./config.toml;
      default = defaults.sessionRetentionDays;
    } 1 3650;

    web = {
      enable = mkBoolOption { default = defaults.web.enable; };
      extraArgs = mkStrListOption { default = defaults.web.extraArgs; };
    };

    extensions = {
      context-language-models = clm.options;
      recursive-language-models = rlm.options;

      # Used when the vpn-rotate module is enabled; it owns the daemon and its options.
      vpn-rotate = {
        maxRetries = mkIntBetweenOption {
          path = "icedos.ai-tools.opencode.extensions.vpn-rotate.maxRetries";
          source = ./config.toml;
          default = defaults.extensions.vpn-rotate.maxRetries;
        } 0 100;
        fallbackModels = mkStrListOption { default = defaults.extensions.vpn-rotate.fallbackModels; };
        # "prompt", "stay" or a duration such as "15m"; the plugin parses it.
        fallbackReturn = lib.mkOption {
          type = lib.types.strMatching "prompt|stay|[0-9]+[smh]";
          default = defaults.extensions.vpn-rotate.fallbackReturn;
        };
      };
    }
    // lib.mapAttrs (name: _: npmExtensionOptions name) npmExtensions;
  };

  outputs.nixosModules =
    { repoUrl, ... }:
    [
      (
        {
          config,
          lib,
          pkgs,
          icedosLib,
          ...
        }:

        let
          inherit (lib)
            attrNames
            concatStringsSep
            mapAttrs
            mkDefault
            mkIf
            optional
            optionalAttrs
            sort
            ;

          hasAccounts = import ../../integrations/ai-accounts/lib/has-accounts.nix { inherit lib; } config;
          wrapAccounts = import ../../integrations/ai-accounts/lib/wrap.nix { inherit lib pkgs; };
          aiAccountsOn = icedosLib.hasModule {
            inherit config repoUrl;
            name = "ai-accounts";
          };

          cfg = config.icedos.ai-tools.opencode;
          gcDays = cfg.sessionRetentionDays;

          vpn = {
            enable = icedosLib.hasModule {
              inherit config repoUrl;
              name = "vpn-rotate";
            };
          }
          // cfg.extensions.vpn-rotate;

          vpnPlugin = optional vpn.enable {
            package = "${./plugins/vpn-rotate}";
            options = {
              command = [ "${pkgs.vpn-rotate}/bin/vpn-rotate" ];
              inherit (vpn)
                maxRetries
                fallbackModels
                fallbackReturn
                ;
            };
          };

          # Concatenate shared and per-agent rules sorted by name into AGENTS.md context
          allRules =
            config.icedos.ai-tools.rules
            // optionalAttrs vpn.enable {
              vpn-rotate = "When the opencode provider rate-limits a request, a background service retries it through a private userspace tunnel of its own and may change servers inside that tunnel. Do not run protonvpn connect or disconnect yourself: the host network must stay as it is.";
            }
            // cfg.rules;
          sortedRuleNames = sort (a: b: a < b) (attrNames allRules);
          renderedContext = concatStringsSep "

" (map (name: allRules.${name}) sortedRuleNames);

          # Filter shared skills pool minus excludeSkills plus per-agent and RLM skills
          # The bundled review drives rlm() children, so it needs the RLM extension.
          reviewOn = cfg.extensions.recursive-language-models.enable && cfg.codeReview;
          skills = removeAttrs (
            config.icedos.ai-tools.skills.pool
            // optionalAttrs reviewOn { code-review = builtins.readFile ./skills/code-review/SKILL.md; }
            // optionalAttrs (reviewOn && cfg.freeReview) {
              free-review = builtins.readFile ./skills/free-review/SKILL.md;
            }
            // cfg.skills
            // (rlm.skills cfg.extensions.recursive-language-models)
          ) cfg.excludeSkills;

          opencodeCommand = cfg.launchWrapper ++ [ "opencode" ];

          # Prune stale OpenCode logs and old sessions via CLI when not running
          opencodeGcHook = ''
            O="''${HOME}/.local/share/opencode"
            [ -d "''${O}" ] || exit 0

            gc_section "''${O}" "path"
            gc_note "Keeping paths newer than ''${DIM_GREEN}${toString gcDays}''${NC}d"

            if [ -d "''${O}/log" ]; then
              gc_by_age ${toString gcDays} "''${O}/log" -type f
            fi

            # Per-account DBs (ai-accounts, OPENCODE_DB) are out of scope; GC prunes the default user data dir only.
            if [ -f "''${O}/opencode.db" ]; then
              cutoff_ms=$(( ($(date +%s) - ${toString gcDays}*86400) * 1000 ))
              old_sessions=$(${pkgs.sqlite}/bin/sqlite3 -readonly "''${O}/opencode.db"                 "SELECT id FROM session_v2 WHERE parent_id IS NULL AND time_updated < $cutoff_ms;" 2>/dev/null || true)
              for s in $old_sessions; do
                ${pkgs.opencode}/bin/opencode --standalone session delete "$s" >/dev/null 2>&1 || true
              done
            fi

            gc_commit
            exit 0
          '';
          npmEntries =
            tui:
            lib.concatLists (
              lib.mapAttrsToList (
                name: ext:
                let
                  e = cfg.extensions.${name};
                  pkg = ext.package + lib.optionalString (e.version != "") "@${e.version}";
                in
                optional (e.enable && ext.tui == tui) (
                  if e.options == { } then
                    pkg
                  else
                    {
                      package = pkg;
                      inherit (e) options;
                    }
                )
              ) npmExtensions
            );

          tuiPlugins =
            optional
              (
                cfg.terminal-title.enable
                || cfg.turnEndMessage
                || cfg.toolTimer
                || vpn.enable
                || cfg.extensions.recursive-language-models.enable
              )
              {
                package = "${./plugins/icedos-status}";
                options = {
                  inherit (cfg.terminal-title) idleGlyph workingGlyphs;
                  terminalTitle = cfg.terminal-title.enable;
                  turnEnd = cfg.turnEndMessage;
                  inherit (cfg) toolTimer;
                  vpnRow = vpn.enable;
                  rlmRow = cfg.extensions.recursive-language-models.enable;
                };
              };
        in
        {
          icedos.ai-tools.agent-launch.agents.opencode = {
            command = mkDefault opencodeCommand;
            resumeArgs = mkDefault [
              "--session"
              "{id}"
            ];
            locator = mkDefault "opencode";
            label = mkDefault "OpenCode";
          };

          icedos.system.gc.hooks.postGc =
            optional cfg.includeInIcedosGc opencodeGcHook
            ++ rlm.postGc cfg.extensions.recursive-language-models;

          home-manager.sharedModules = [
            (
              {
                config,
                lib,
                options,
                pkgs,
                ...
              }:

              let
                basePackage =
                  if aiAccountsOn && hasAccounts "opencode" then
                    wrapAccounts {
                      agent = "opencode";
                      package = pkgs.opencode;
                      bin = "opencode";
                    }
                  else
                    pkgs.opencode;

                wrappedPackage =
                  if cfg.extraArgs == [ ] then
                    basePackage
                  else
                    pkgs.symlinkJoin {
                      name = "opencode-extra-args";
                      version = basePackage.version;
                      paths = [ basePackage ];
                      nativeBuildInputs = [ pkgs.makeWrapper ];
                      postBuild = ''
                        rm "$out/bin/opencode"
                        makeWrapper ${basePackage}/bin/opencode "$out/bin/opencode"                           --add-flags ${lib.escapeShellArg (lib.concatStringsSep " " cfg.extraArgs)}
                      '';
                      meta.mainProgram = "opencode";
                    };

                mergedPlugins =
                  cfg.plugins
                  ++ vpnPlugin
                  ++ npmEntries false
                  ++ (clm.pluginEntry cfg.extensions.context-language-models)
                  ++ (rlm.pluginEntry cfg.extensions.recursive-language-models pkgs)
                  ++ (cfg.settings.plugins or [ ]);
                mergedMcp = cfg.extra-mcp-servers // (cfg.settings.mcp or { });

                mergedCliPlugins = tuiPlugins ++ npmEntries true ++ (cfg.cli.plugins or [ ]);
                cliTerminal =
                  (optionalAttrs cfg.terminal-title.enable { title = false; }) // (cfg.cli.terminal or { });
              in
              {
                options.icedos.managed-settings.opencode = icedosLib.mkAttrsOption {
                  default = config.programs.opencode.settings;
                };

                config = {
                  programs.opencode = {
                    enable = mkDefault true;
                    package = mkIf (cfg.extraArgs != [ ] || (aiAccountsOn && hasAccounts "opencode")) wrappedPackage;
                    enableMcpIntegration = cfg.enableMcpIntegration;

                    agents = rlm.agents cfg.extensions.recursive-language-models (
                      attrNames ((optionalAttrs cfg.enableMcpIntegration config.programs.mcp.servers) // mergedMcp)
                    );
                    context = mkDefault renderedContext;
                    skills = mapAttrs (_: mkDefault) skills;

                    settings =
                      lib.recursiveUpdate
                        (lib.recursiveUpdate (
                          (removeAttrs cfg.settings [
                            "plugins"
                            "mcp"
                          ])
                          // optionalAttrs (mergedPlugins != [ ]) { plugins = mergedPlugins; }
                          // optionalAttrs (mergedMcp != { }) { mcp = mergedMcp; }
                        ) (clm.settings cfg.extensions.context-language-models))
                        (rlm.settings cfg.extensions.recursive-language-models pkgs config.programs.opencode.package);

                    cli =
                      (removeAttrs cfg.cli [
                        "plugins"
                        "terminal"
                      ])
                      // optionalAttrs (cfg.terminal-title.enable || cfg.cli ? terminal) { terminal = cliTerminal; }
                      // optionalAttrs (mergedCliPlugins != [ ]) { plugins = mergedCliPlugins; };

                    web = {
                      enable = cfg.web.enable;
                      extraArgs = cfg.web.extraArgs;
                    };
                  };

                  xdg.desktopEntries.opencode = mkIf (cfg.desktopEntry && config.programs.opencode.enable) {
                    name = "OpenCode";
                    comment = "AI coding agent (terminal UI)";
                    icon = "utilities-terminal";
                    exec = concatStringsSep " " opencodeCommand;
                    terminal = true;
                    categories = [
                      "Development"
                      "Utility"
                    ];
                  };
                }
                # Stylix's opencode target writes tui.json, which OpenCode 2 ignores; cli.theme sets the theme.
                // optionalAttrs (options ? stylix) { stylix.targets.opencode.enable = mkDefault false; };
              }
            )
          ];
        }
      )
    ];

  meta = {
    name = "opencode";
    dependencies = [ { modules = [ "agent-launch" ]; } ];
  };
}
