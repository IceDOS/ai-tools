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
        extensions
        extra-mcp-servers
        launchWrapper
        rules
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
      # into ~/.gemini/config/mcp_config.json for Antigravity.
      enableMcpIntegration = mkBoolOption { default = enableMcpIntegration; };

      # Additional MCP servers registered exclusively for Antigravity.
      extra-mcp-servers = mkAttrsOfOption { default = extra-mcp-servers; } lib.types.anything;

      # Command prepended to agy in agent-launch and the desktop entry,
      # such as agent-relay's PTY proxy.
      launchWrapper = mkStrListOption { default = launchWrapper; };

      # Global rules, written to ~/.gemini/config/rules/<name>.md (agy reads that folder's top level only).
      rules = mkAttrsOfOption { default = rules; } lib.types.lines;

      # Global skills (name = SKILL.md), written to ~/.gemini/antigravity-cli/skills/<name>/SKILL.md.
      skills = mkAttrsOfOption { default = skills; } lib.types.lines;

      # Bottom status bar (/statusline), alimit's shared agent bar; set in agy's own settings.json.
      statusLine = mkBoolOption { default = statusLine; };

      # Render the bar below agy's built-in header instead of replacing it.
      statusLineStackWithDefault = mkBoolOption { default = statusLineStackWithDefault; };

      # Terminal title "<status glyph> <conversation title>"; the status line command drives it.
      # Give every glyph the same width in the title font.
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
            statusLine
            statusLineStackWithDefault
            terminal-title
            ;

          # Shared icedos.ai-tools entries first, so a same-named antigravity entry wins.
          rules = config.icedos.ai-tools.rules // config.icedos.ai-tools.antigravity.rules;

          skills = config.icedos.ai-tools.skills // config.icedos.ai-tools.antigravity.skills;

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

          # statusline.py imports terminal_title.py from its own directory.
          statusLineScripts = pkgs.runCommand "antigravity-statusline-scripts" { } ''
            mkdir -p $out
            cp ${./statusline.py} $out/statusline.py
            cp ${./terminal_title.py} $out/terminal_title.py
          '';

          # Wrapper so activation and settings.json share one store path.
          statusLinePkg = pkgs.writeShellApplication {
            name = "antigravity-statusline";
            text = ''
              export ANTIGRAVITY_ALIMIT=${lib.getExe alimitPkg}
              export ANTIGRAVITY_BIN=${agyBin}
              export ANTIGRAVITY_TITLE_IDLE_GLYPH=${lib.escapeShellArg terminal-title.idleGlyph}
              export ANTIGRAVITY_TITLE_WORKING_GLYPHS=${lib.escapeShellArg (lib.concatStringsSep " " terminal-title.workingGlyphs)}
              exec ${pkgs.python3}/bin/python3 ${statusLineScripts}/statusline.py "$@"
            '';
          };

          # agy runs the status line on every agent state change, so the title rides on it;
          # with the bar off, an empty custom line stacked on agy's default renders as the default.
          statusLineCommand = lib.concatStringsSep " " (
            [ "${statusLinePkg}/bin/antigravity-statusline" ]
            ++ lib.optional terminal-title.enable "--title"
            ++ lib.optional (!statusLine) "--no-bar"
          );

          agyBin =
            if aiAccountsOn && hasAccounts "antigravity" then
              "${
                wrapAccounts {
                  agent = "antigravity";
                  package = pkgs.antigravity-cli;
                  bin = "agy";
                }
              }/bin/agy"
            else
              "${pkgs.antigravity-cli}/bin/agy";

          rlmCfg = config.icedos.ai-tools.antigravity.extensions.recursive-language-models;
          rlm = import ./plugins/rlm/package.nix {
            inherit pkgs lib;
            cfg = rlmCfg;
            agy = "${pkgs.antigravity-cli}/bin/agy";
          };
          rlmDataDir = "$HOME/.gemini/antigravity-cli/plugin_data/rlm";

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

              in
              {
                # The same block `statusline.py --setup` writes; null clears agy's key.
                options.icedos.managed-settings.antigravity = icedosLib.mkAttrsOption {
                  default =
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
                };

                config = {
                  home.file = {
                    ".gemini/config/mcp_config.json" = mkIf (enabledServers != { }) {
                      source = jsonFormat.generate "antigravity-mcp-config.json" mcpConfig;
                    };
                  }
                  // mapAttrs' (
                    name: text: nameValuePair ".gemini/config/rules/${name}.md" { text = withTrigger text; }
                  ) rules
                  // mapAttrs' (
                    name: text: nameValuePair ".gemini/antigravity-cli/skills/${name}/SKILL.md" { inherit text; }
                  ) skills
                  // lib.optionalAttrs rlmCfg.enable {
                    ".gemini/config/plugins/rlm".source = rlm.plugin;
                  };

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

                  # agy owns ~/.gemini/antigravity-cli/settings.json (it writes on settings
                  # changes), so Home Manager only merges/removes its statusLine key.
                  home.activation.icedosAntigravityStatusLine = lib.hm.dag.entryAfter [ "writeBoundary" ] ''
                    run ${statusLinePkg}/bin/antigravity-statusline --setup ${
                      if statusLine || terminal-title.enable then "on" else "off"
                    } ${lib.escapeShellArg statusLineCommand}${
                      lib.optionalString (statusLineStackWithDefault || !statusLine) " --stack"
                    }
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
              [ -d "$D" ] || D="$HOME/.local/share/icedos-rlm"
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
