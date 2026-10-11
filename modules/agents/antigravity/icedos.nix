{ icedosLib, lib, ... }:

{
  options.icedos.ai-tools.antigravity =
    let
      inherit (lib) importTOML;
      inherit (icedosLib)
        mkAttrsOfOption
        mkBoolOption
        mkStrListOption
        mkStrOption
        ;

      inherit ((importTOML ./config.toml).icedos.ai-tools.antigravity)
        desktopEntry
        enableMcpIntegration
        extraMcpServers
        peonPing
        rules
        skills
        statusLine
        statusLineStackWithDefault
        terminalTitle
        ;
    in
    {
      # Whether the launcher installs a desktop entry opening in the default terminal.
      desktopEntry = mkBoolOption { default = desktopEntry; };

      # Whether to wire Home Manager's shared programs.mcp.servers registry
      # into ~/.gemini/config/mcp_config.json for Antigravity.
      enableMcpIntegration = mkBoolOption { default = enableMcpIntegration; };

      # Additional MCP servers registered exclusively for Antigravity.
      extraMcpServers = mkAttrsOfOption { default = extraMcpServers; } lib.types.anything;

      # Play peon-ping sounds for agy sessions (upstream antigravity watcher as a user service),
      # for users with icedos.ai-tools.peon-ping configured.
      peonPing = mkBoolOption { default = peonPing; };

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
      terminalTitle = {
        enable = mkBoolOption { default = terminalTitle.enable; };
        idleGlyph = mkStrOption { default = terminalTitle.idleGlyph; };
        workingGlyphs = mkStrListOption { default = terminalTitle.workingGlyphs; };
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
            extraMcpServers
            peonPing
            statusLine
            statusLineStackWithDefault
            terminalTitle
            ;

          # Shared icedos.ai-tools entries first, so a same-named antigravity entry wins.
          rules = config.icedos.ai-tools.rules // config.icedos.ai-tools.antigravity.rules;

          peonPingUsers = config.icedos.ai-tools.peon-ping.users or { };

          # Upstream watcher already globs ~/.gemini/antigravity-cli; it needs python3 with watchdog.
          peonWatcher =
            peonPkg:
            pkgs.writeShellApplication {
              name = "peon-ping-antigravity";
              runtimeInputs = [
                (pkgs.python3.withPackages (p: [ p.watchdog ]))
                pkgs.coreutils
                pkgs.gnugrep
                pkgs.procps
              ];
              text = ''
                exec bash ${peonPkg}/share/peon-ping/adapters/antigravity-py.sh
              '';
            };
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
              export ANTIGRAVITY_TITLE_IDLE_GLYPH=${lib.escapeShellArg terminalTitle.idleGlyph}
              export ANTIGRAVITY_TITLE_WORKING_GLYPHS=${lib.escapeShellArg (lib.concatStringsSep " " terminalTitle.workingGlyphs)}
              exec ${pkgs.python3}/bin/python3 ${statusLineScripts}/statusline.py "$@"
            '';
          };

          # agy runs the status line on every agent state change, so the title rides on it;
          # with the bar off, an empty custom line stacked on agy's default renders as the default.
          statusLineCommand = lib.concatStringsSep " " (
            [ "${statusLinePkg}/bin/antigravity-statusline" ]
            ++ lib.optional terminalTitle.enable "--title"
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
        in
        {
          icedos.ai-tools.agent-launch.agents.antigravity = {
            # agy is not on PATH; this module only wires its config.
            command = lib.mkDefault [ agyBin ];
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

                allServers = mcpRegistry // extraMcpServers;

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
                    s;

                mcpConfig = {
                  mcpServers = mapAttrs (_: transformServer) enabledServers;
                };

              in
              {
                # The same block `statusline.py --setup` writes; null clears agy's key.
                options.icedos.managedSettings.antigravity = icedosLib.mkAttrsOption {
                  default =
                    if statusLine || terminalTitle.enable then
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
                  ) skills;

                  # Terminal=true: the desktop environment launches the user's default terminal.
                  xdg.desktopEntries.antigravity-cli = mkIf desktopEntry {
                    name = "Antigravity CLI";
                    comment = "AI coding assistant (terminal UI)";
                    icon = "utilities-terminal";
                    exec = agyBin;
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
                      if statusLine || terminalTitle.enable then "on" else "off"
                    } ${lib.escapeShellArg statusLineCommand}${
                      lib.optionalString (statusLineStackWithDefault || !statusLine) " --stack"
                    }
                  '';

                  systemd.user.services.peon-ping-antigravity =
                    mkIf (peonPing && peonPingUsers ? ${config.home.username})
                      {
                        Unit = {
                          Description = "peon-ping sounds for Antigravity CLI";
                          After = [ "graphical-session.target" ];
                          PartOf = [ "graphical-session.target" ];
                        };
                        Service = {
                          ExecStart = lib.getExe (peonWatcher config.programs.peon-ping.package);
                          Environment = [
                            "CLAUDE_PEON_DIR=%h/.openpeon"
                            "PEON_DIR=%h/.openpeon"
                          ];
                          Restart = "on-failure";
                          RestartSec = 5;
                        };
                        Install.WantedBy = [ "graphical-session.target" ];
                      };
                };
              }
            )
          ];

          icedos.system.tips.list = [
            "Antigravity automatically discovers MCP servers declared in programs.mcp.servers."
          ];
        }
      )
    ];

  meta = {
    name = "antigravity";
    dependencies = [ { modules = [ "agent-launch" ]; } ];
  };
}
