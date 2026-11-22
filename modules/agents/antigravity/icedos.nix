{ icedosLib, lib, ... }:

{
  options.icedos.ai-tools.antigravity =
    let
      inherit (lib) importTOML;
      inherit (icedosLib)
        mkAttrsOfOption
        mkBoolOption
        mkIntBetweenOption
        mkStrListOption
        mkStrOption
        ;

      inherit ((importTOML ./config.toml).icedos.ai-tools.antigravity)
        desktopEntry
        enableMcpIntegration
        excludeSkills
        extensions
        extra-mcp-servers
        launchWrapper
        rules
        settings
        skills
        statusLine
        statusLineStackWithDefault
        terminal-title
        ;
    in
    {
      # Whether the launcher installs a desktop entry opening in the default terminal.
      desktopEntry = mkBoolOption { default = desktopEntry; };

      # Whether to wire Home Manager's shared programs.mcp.servers registry
      # into ~/.config/gemini/mcp_config.json for Antigravity.
      enableMcpIntegration = mkBoolOption { default = enableMcpIntegration; };

      # Shared skills Antigravity skips.
      excludeSkills = mkStrListOption { default = excludeSkills; };

      # Additional MCP servers registered exclusively for Antigravity.
      extra-mcp-servers = mkAttrsOfOption { default = extra-mcp-servers; } lib.types.anything;

      # Command prepended to agy in agent-launch and the desktop entry,
      # such as agent-relay's PTY proxy.
      launchWrapper = mkStrListOption { default = launchWrapper; };

      # Global rules, written to ~/.config/gemini/rules/<name>.md (agy reads that folder's top level only).
      rules = mkAttrsOfOption { default = rules; } lib.types.lines;

      # agy settings.json keys merged on activation and launch (null removes).
      settings = mkAttrsOfOption { default = settings; } lib.types.anything;

      # Global skills (name = SKILL.md), written to ~/.local/share/gemini/antigravity-cli/skills/<name>/SKILL.md.
      skills = mkAttrsOfOption { default = skills; } lib.types.lines;

      # Bottom status bar (/statusline), alimit's shared agent bar; set in agy's own settings.json.
      statusLine = mkBoolOption { default = statusLine; };

      # Render the bar below agy's built-in header instead of replacing it.
      statusLineStackWithDefault = mkBoolOption { default = statusLineStackWithDefault; };

      # Terminal title "<glyph> <title>"; the status line command drives it, and a detached
      # ticker animates the working glyph. Use glyphs of one width in the title font.
      terminal-title = {
        enable = mkBoolOption { default = terminal-title.enable; };
        idleGlyph = mkStrOption { default = terminal-title.idleGlyph; };
        workingGlyphs = mkStrListOption { default = terminal-title.workingGlyphs; };
      };

      extensions.recursive-language-models =
        let
          e = extensions.recursive-language-models;
          path = "icedos.ai-tools.antigravity.extensions.recursive-language-models";
        in
        {
          # rlm plugin: ipython MCP tool whose rlm() runs child agy agents; adds /rlm and /refine.
          enable = mkBoolOption { default = e.enable; };
          # Launch agy with --agent rlm from agent-launch and the desktop entry.
          autoload = mkBoolOption { default = e.autoload; };
          maxDepth = mkIntBetweenOption {
            path = "${path}.maxDepth";
            source = ./config.toml;
            default = e.maxDepth;
          } 0 10;
          maxParallel = mkIntBetweenOption {
            path = "${path}.maxParallel";
            source = ./config.toml;
            default = e.maxParallel;
          } 1 64;
          childLog = mkBoolOption { default = e.childLog; };
          childTimeout = mkStrOption { default = e.childTimeout; };
          snapshots = mkBoolOption { default = e.snapshots; };
          includeInIcedosGc = mkBoolOption { default = e.includeInIcedosGc; };
          retentionDays = mkIntBetweenOption {
            path = "${path}.retentionDays";
            source = ./config.toml;
            default = e.retentionDays;
          } 1 3650;
        };
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
          hasAccounts = import ../../integrations/ai-accounts/lib/has-accounts.nix { inherit lib; } config;
          wrapAccounts = import ../../integrations/ai-accounts/lib/wrap.nix { inherit lib pkgs; };
          aiAccountsOn = icedosLib.hasModule {
            inherit config repoUrl;
            name = "ai-accounts";
          };

          inherit (config.icedos.ai-tools.antigravity)
            desktopEntry
            enableMcpIntegration
            extra-mcp-servers
            launchWrapper
            settings
            statusLine
            statusLineStackWithDefault
            terminal-title
            ;

          # Shared icedos.ai-tools entries first, so a same-named antigravity entry wins.
          rules = config.icedos.ai-tools.rules // config.icedos.ai-tools.antigravity.rules;

          antigravityViewSkills = config.icedos.ai-tools.agent-views.antigravity.skills;
          antigravityGlobalPlugins = config.icedos.ai-tools.per-agent.antigravity.globalPlugins;
          antigravityPerAgentPlugins = config.icedos.ai-tools.per-agent.antigravity.plugins;

          inherit (lib)
            filterAttrs
            hasPrefix
            mapAttrs
            mapAttrs'
            mkIf
            nameValuePair
            optionalAttrs
            ;

          jsonFormat = pkgs.formats.json { };

          # agy drops a rules/*.md file without a trigger, so plain text becomes always_on.
          withTrigger =
            text: if hasPrefix "---\n" text then text else "---\ntrigger: always_on\n---\n\n${text}";

          # The bar is alimit's shared agent status line.
          alimitPkg = pkgs.callPackage ../../integrations/alimit/package.nix { };

          # statusline.py imports terminal_title.py and tool_state.py from its own directory,
          # and the tool timer hook runs from the same one.
          agentScripts = pkgs.runCommand "antigravity-agent-scripts" { } ''
            mkdir -p $out
            cp ${./statusline.py} $out/statusline.py
            cp ${../lib/terminal-title/terminal_title.py} $out/terminal_title.py
            cp ${../lib/terminal-title/tool_state.py} $out/tool_state.py
            cp ${./tool_timer.py} $out/tool_timer.py
          '';

          # Wrapper so activation and settings.json share one store path.
          statusLinePkg = pkgs.writeShellApplication {
            name = "antigravity-statusline";
            text = ''
              export ANTIGRAVITY_ALIMIT=${lib.getExe alimitPkg}
              export ANTIGRAVITY_BIN=${agyBin}
              export ANTIGRAVITY_TITLE_IDLE_GLYPH=${lib.escapeShellArg terminal-title.idleGlyph}
              export ANTIGRAVITY_TITLE_WORKING_GLYPHS=${lib.escapeShellArg (lib.concatStringsSep " " terminal-title.workingGlyphs)}
              ${lib.optionalString rlmCfg.enable ''
                # The title counts the rlm() children still running from their logs here.
                export ANTIGRAVITY_RLM_DATA_DIR="${rlmDataDir}"
                [ -d "$ANTIGRAVITY_RLM_DATA_DIR" ] || export ANTIGRAVITY_RLM_DATA_DIR="$HOME/.local/state/icedos/rlm"
              ''}
              exec ${pkgs.python3}/bin/python3 ${agentScripts}/statusline.py "$@"
            '';
          };

          # agy runs the status line on every agent state change, so the title rides on it;
          # with the bar off, an empty custom line stacked on agy's default renders as the default.
          statusLineCommand = lib.concatStringsSep " " (
            [ "${statusLinePkg}/bin/antigravity-statusline" ]
            ++ lib.optional terminal-title.enable "--title"
            ++ lib.optional (!statusLine) "--no-bar"
          );

          # agy loads hooks.json from a plugin root and merges its hooks with the user's own
          # ~/.config/gemini/hooks.json, which agent-relay owns. The hook records the tool call
          # that is running, so the title's ticker can time it, and answers the
          # PreToolUse decision agy requires to keep its own permission flow.
          toolTimerHook = event: [
            {
              type = "command";
              command = "${pkgs.python3}/bin/python3 ${agentScripts}/tool_timer.py ${event}";
              timeout = 10;
            }
          ];

          toolTimerPlugin = pkgs.runCommand "antigravity-tool-timer-plugin" { } ''
            mkdir -p $out
            cp ${
              pkgs.writeText "plugin.json" (
                builtins.toJSON {
                  name = "tool-timer";
                  displayName = "Tool timer";
                  description = "Records the running tool call, so the terminal title can time it.";
                  version = "0.1.0";
                }
              )
            } $out/plugin.json
            cp ${
              pkgs.writeText "hooks.json" (
                builtins.toJSON {
                  tool-timer = {
                    PreToolUse = [
                      {
                        matcher = "*";
                        hooks = toolTimerHook "PreToolUse";
                      }
                    ];
                    PostToolUse = [
                      {
                        matcher = "*";
                        hooks = toolTimerHook "PostToolUse";
                      }
                    ];
                    # Stop takes handlers directly; a matcher wrapper makes agy reject the whole plugin.
                    Stop = toolTimerHook "Stop";
                  };
                }
              )
            } $out/hooks.json
          '';

          # agy hardcodes $HOME/.gemini, so agy_home.py runs it with HOME at a symlink farm
          # of ~ whose .gemini is ~/.local/share/gemini; ai-accounts' mirror.py builds the farm.
          agyHomeLauncher = pkgs.runCommand "antigravity-home-launcher" { } ''
            mkdir -p $out
            cp ${./agy_home.py} $out/agy_home.py
            cp -r ${../../integrations/ai-accounts/src/ai_accounts} $out/ai_accounts
          '';

          agyPkg = pkgs.symlinkJoin {
            pname = "antigravity-cli";
            inherit (pkgs.antigravity-cli) version;
            paths = [ pkgs.antigravity-cli ];
            nativeBuildInputs = [ pkgs.makeWrapper ];
            postBuild = ''
              rm $out/bin/agy
              makeWrapper ${pkgs.python3}/bin/python3 $out/bin/agy \
                --add-flags "-E ${agyHomeLauncher}/agy_home.py ${pkgs.antigravity-cli}/bin/agy"
            '';
            meta.mainProgram = "agy";
          };

          agyBin =
            if aiAccountsOn && hasAccounts "antigravity" then
              "${
                wrapAccounts {
                  agent = "antigravity";
                  package = agyPkg;
                  bin = "agy";
                }
              }/bin/agy"
            else
              "${agyPkg}/bin/agy";

          rlmCfg = config.icedos.ai-tools.antigravity.extensions.recursive-language-models;
          rlm = import ./plugins/rlm/package.nix {
            inherit pkgs lib;
            cfg = rlmCfg;
            agy = "${pkgs.antigravity-cli}/bin/agy";
          };
          rlmDataDir = "$HOME/.local/share/gemini/antigravity-cli/plugin_data/rlm";

          # A launch wrapper such as agent-relay's PTY proxy takes the agy path as its last argument.
          agyCommand =
            launchWrapper
            ++ [ agyBin ]
            ++ lib.optionals (rlmCfg.enable && rlmCfg.autoload) [
              "--agent"
              "rlm"
            ];
        in
        {
          icedos.ai-tools.per-agent.antigravity = {
            skills = config.icedos.ai-tools.antigravity.skills;
            excludeSkills = config.icedos.ai-tools.antigravity.excludeSkills;
            plugins =
              (lib.optionalAttrs rlmCfg.enable {
                rlm = rlm.plugin;
              })
              // (lib.optionalAttrs terminal-title.enable {
                tool-timer = toolTimerPlugin;
              });
          };

          icedos.ai-tools.agent-launch.agents.antigravity = {
            # agy is not on PATH; this module only wires its config.
            command = lib.mkDefault agyCommand;
            resumeArgs = lib.mkDefault [
              "--conversation"
              "{id}"
            ];
            locator = lib.mkDefault "antigravity";
            label = lib.mkDefault "Antigravity CLI";
          };

          home-manager.sharedModules = [
            (
              { config, lib, ... }:

              let
                # Pull every server from the shared programs.mcp.servers registry
                mcpRegistry =
                  if enableMcpIntegration && (config.programs.mcp.enable or false) then
                    config.programs.mcp.servers or { }
                  else
                    { };

                allServers = mcpRegistry // extra-mcp-servers;

                isEnabled =
                  s:
                  if (s.enabled or null) != null then
                    s.enabled
                  else if (s.disabled or null) != null then
                    !s.disabled
                  else
                    true;

                enabledServers = filterAttrs (_: isEnabled) allServers;

                transformServer =
                  s:
                  let
                    timeoutAttrs =
                      if (s.timeoutSeconds or null) != null then
                        { inherit (s) timeoutSeconds; }
                      else if (s.timeout or null) != null then
                        { timeoutSeconds = s.timeout / 1000; }
                      else
                        { };
                  in
                  (
                    if (s.command or null) != null then
                      {
                        inherit (s) command;
                      }
                      // optionalAttrs ((s.args or [ ]) != [ ]) { inherit (s) args; }
                      // optionalAttrs ((s.env or { }) != { }) {
                        env = mapAttrs (_: v: if builtins.isAttrs v && v ? file then v.file else v) s.env;
                      }
                    else if (s.url or null) != null then
                      {
                        serverUrl = s.url;
                      }
                      // optionalAttrs ((s.headers or { }) != { }) { inherit (s) headers; }
                    else if (s.serverUrl or null) != null then
                      {
                        inherit (s) serverUrl;
                      }
                    else
                      s
                  )
                  // timeoutAttrs;

                mcpConfig = {
                  mcpServers = mapAttrs (_: transformServer) enabledServers;
                };

                antigravityPlugins =
                  (lib.genAttrs antigravityGlobalPlugins (
                    name: "${config.xdg.dataHome}/icedos/agents/global/plugins/${name}"
                  ))
                  // (lib.mapAttrs (
                    name: _: "${config.xdg.dataHome}/icedos/agents/per-agent/antigravity/plugins/${name}"
                  ) antigravityPerAgentPlugins);

                antigravitySkills = lib.genAttrs antigravityViewSkills (
                  name: "${config.xdg.dataHome}/icedos/agents/view/antigravity/skills/${name}"
                );

              in
              {
                # The managed block for agy's settings.json; null clears a key.
                options.icedos.managed-settings.antigravity = icedosLib.mkAttrsOption {
                  default =
                    let
                      statusBlock =
                        if statusLine || terminal-title.enable then
                          {
                            statusLine = {
                              type = "command";
                              command = statusLineCommand;
                            }
                            // lib.optionalAttrs (statusLineStackWithDefault || !statusLine) {
                              stack_with_default = true;
                            };
                          }
                        else
                          { statusLine = null; };
                    in
                    lib.recursiveUpdate statusBlock settings;
                };

                config = {
                  xdg.configFile = {
                    "gemini/mcp_config.json" = mkIf (enabledServers != { }) {
                      source = jsonFormat.generate "antigravity-mcp-config.json" mcpConfig;
                    };
                  }
                  // mapAttrs' (
                    name: text: nameValuePair "gemini/rules/${name}.md" { text = withTrigger text; }
                  ) rules
                  // lib.mapAttrs' (
                    name: target:
                    nameValuePair "gemini/plugins/${name}" {
                      source = config.lib.file.mkOutOfStoreSymlink target;
                    }
                  ) antigravityPlugins;

                  xdg.dataFile = lib.mapAttrs' (
                    name: target:
                    nameValuePair "gemini/antigravity-cli/skills/${name}" {
                      source = config.lib.file.mkOutOfStoreSymlink target;
                    }
                  ) antigravitySkills;

                  # Terminal=true: the desktop environment launches the user's default terminal.
                  xdg.desktopEntries.antigravity-cli = mkIf desktopEntry {
                    name = "Antigravity CLI";
                    comment = "AI coding assistant (terminal UI)";
                    icon = "utilities-terminal";
                    exec = lib.escapeShellArgs agyCommand;
                    terminal = true;
                    categories = [
                      "Development"
                      "Utility"
                    ];
                  };

                  home.activation.icedosAntigravityLayout = lib.hm.dag.entryBefore [ "checkLinkTargets" ] ''
                    set -eu
                    GEMINI="${config.home.homeDirectory}/.gemini"
                    SHARE_GEMINI="${config.home.homeDirectory}/.local/share/gemini"
                    CONFIG_GEMINI="${config.home.homeDirectory}/.config/gemini"

                    # 1. Move ~/.gemini to ~/.local/share/gemini; the agy launcher points its own home's .gemini there.
                    if [ -L "$GEMINI" ]; then
                      # The old layout's link; agy sessions started before the launcher still use it.
                      if [ "$(readlink "$GEMINI")" = "$SHARE_GEMINI" ]; then
                        if ${pkgs.procps}/bin/pgrep -r R,S,D,T -x agy >/dev/null 2>&1; then
                          echo "antigravity layout: agy is running; keeping $GEMINI until a later activation"
                        else
                          run rm "$GEMINI"
                        fi
                      fi
                    elif [ -d "$GEMINI" ]; then
                      if [ ! -e "$SHARE_GEMINI" ]; then
                        run mkdir -p "${config.home.homeDirectory}/.local/share"
                        run mv "$GEMINI" "$SHARE_GEMINI"
                      else
                        echo "antigravity layout: both $GEMINI and $SHARE_GEMINI exist; skipping migration" >&2
                      fi
                    fi
                    [ -e "$SHARE_GEMINI" ] || run mkdir -p "$SHARE_GEMINI"

                    # 2. Move ~/.local/share/gemini/config to ~/.config/gemini and symlink ~/.local/share/gemini/config -> ~/.config/gemini
                    if [ -d "$SHARE_GEMINI/config" ] && [ ! -L "$SHARE_GEMINI/config" ]; then
                      if [ ! -e "$CONFIG_GEMINI" ]; then
                        run mkdir -p "${config.home.homeDirectory}/.config"
                        run mv "$SHARE_GEMINI/config" "$CONFIG_GEMINI"
                        run ln -s "$CONFIG_GEMINI" "$SHARE_GEMINI/config"
                      else
                        echo "antigravity layout: both $SHARE_GEMINI/config and $CONFIG_GEMINI exist; skipping migration" >&2
                      fi
                    elif [ ! -e "$CONFIG_GEMINI" ]; then
                      run mkdir -p "$CONFIG_GEMINI"
                    fi
                    if [ ! -e "$SHARE_GEMINI/config" ] && [ ! -L "$SHARE_GEMINI/config" ]; then
                      run ln -s "$CONFIG_GEMINI" "$SHARE_GEMINI/config"
                    fi

                    # agy's own skills migration can leave a dangling link here, and HM's mkdir then fails
                    SKILLS="$SHARE_GEMINI/antigravity-cli/skills"
                    if [ -L "$SKILLS" ] && [ ! -e "$SKILLS" ]; then
                      run rm "$SKILLS"
                    fi

                    # Remove old HM store links inside moved config and skills dirs before HM checkLinkTargets
                    for d in "$CONFIG_GEMINI" "$SHARE_GEMINI/antigravity-cli/skills"; do
                      if [ -d "$d" ]; then
                        find "$d" -type l -print0 | while IFS= read -r -d "" link; do
                          target=$(readlink "$link" || true)
                          case "$target" in
                            /nix/store/*-home-manager-files/*)
                              run rm "$link"
                              ;;
                          esac
                        done
                      fi
                    done
                    find "$SHARE_GEMINI/antigravity-cli/skills" -mindepth 1 -maxdepth 1 -type d -empty -delete 2>/dev/null || true

                    # 3. Move ai-accounts state to ~/.local/state/icedos/ai-accounts
                    OLD_STATE="${config.home.homeDirectory}/.local/state/ai-accounts"
                    NEW_STATE="${config.home.homeDirectory}/.local/state/icedos/ai-accounts"
                    if [ -d "$OLD_STATE" ] && [ ! -L "$OLD_STATE" ]; then
                      if [ ! -e "$NEW_STATE" ]; then
                        run mkdir -p "${config.home.homeDirectory}/.local/state/icedos"
                        run mv "$OLD_STATE" "$NEW_STATE"
                        if ${pkgs.procps}/bin/pgrep -x agy >/dev/null 2>&1 || ${pkgs.procps}/bin/pgrep -f antigravity-cli >/dev/null 2>&1; then
                          run ln -s "icedos/ai-accounts" "$OLD_STATE"
                          echo "antigravity layout: created compat symlink $OLD_STATE -> icedos/ai-accounts for running sessions; will remove on a later activation without running agy"
                        fi
                      else
                        echo "antigravity layout: both $OLD_STATE and $NEW_STATE exist; skipping migration" >&2
                      fi
                    elif [ -L "$OLD_STATE" ]; then
                      if ! ${pkgs.procps}/bin/pgrep -x agy >/dev/null 2>&1 && ! ${pkgs.procps}/bin/pgrep -f antigravity-cli >/dev/null 2>&1; then
                        run rm "$OLD_STATE"
                        echo "antigravity layout: removed compat symlink $OLD_STATE"
                      fi
                    fi

                    # 4. Remove orphan cache ~/.local/state/antigravity-statusline
                    if [ -e "${config.home.homeDirectory}/.local/state/antigravity-statusline" ] || [ -L "${config.home.homeDirectory}/.local/state/antigravity-statusline" ]; then
                      run rm -rf "${config.home.homeDirectory}/.local/state/antigravity-statusline"
                    fi
                  '';

                  home.activation.icedosAntigravitySettings = lib.hm.dag.entryAfter [ "writeBoundary" ] ''
                    run ${pkgs.python3}/bin/python3 ${../../integrations/ai-accounts/src/ai_accounts/settings.py} \
                      "${config.home.homeDirectory}/.local/share/gemini/antigravity-cli/settings.json" \
                      ${jsonFormat.generate "antigravity-managed-settings.json" config.icedos.managed-settings.antigravity} \
                      "''${XDG_STATE_HOME:-${config.home.homeDirectory}/.local/state}/icedos/antigravity/settings-ledger.json"
                  '';
                };
              }
            )
          ];

          icedos.system.tips.list = [
            "Antigravity automatically discovers MCP servers declared in programs.mcp.servers."
          ];

          icedos.system.gc.hooks.postGc = lib.mkIf (rlmCfg.enable && rlmCfg.includeInIcedosGc) [
            ''
              D="${rlmDataDir}"
              D=''${D//\$HOME/$HOME}
              [ -d "$D" ] || D="$HOME/.local/state/icedos/rlm"
              [ -d "$D" ] || exit 0
              gc_section "$D" "path"
              gc_note "Keeping paths newer than ''${DIM_GREEN}${toString rlmCfg.retentionDays}''${NC}d"
              gc_by_age ${toString rlmCfg.retentionDays} "$D/snapshots" -maxdepth 1 -type f -name '*.pickle'
              gc_by_age ${toString rlmCfg.retentionDays} "$D/children" -mindepth 2 -maxdepth 2 -type f -name '*.jsonl'
              gc_by_age ${toString rlmCfg.retentionDays} "$D/harness" -maxdepth 1 -type f -name '*.jsonl'
              gc_by_age ${toString rlmCfg.retentionDays} "$D/sessions" -maxdepth 1 -type f -name '*.json'
              gc_commit
            ''
          ];
        }
      )
    ];

  meta = {
    name = "antigravity";
    dependencies = [ { modules = [ "agent-launch" ]; } ];
  };
}
