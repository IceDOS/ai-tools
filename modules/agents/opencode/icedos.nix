{ icedosLib, lib, ... }:

{
  options.icedos.ai-tools.opencode =
    let
      inherit (icedosLib)
        mkAttrsOfOption
        mkBoolOption
        mkIntBetweenOption
        ;
      inherit (lib) importTOML;

      inherit ((importTOML ./config.toml).icedos.ai-tools.opencode)
        desktopEntry
        extraSettings
        includeInIcedosGc
        peonPingOverrides
        sessionRetentionDays
        skills
        ;
    in
    {
      # Whether the launcher installs a desktop entry opening in the default terminal.
      desktopEntry = mkBoolOption { default = desktopEntry; };

      extraSettings = mkAttrsOfOption { default = extraSettings; } lib.types.anything;

      # Merged on top of the resolved peon-ping user settings when generating
      # ~/.config/opencode/peon-ping/config.json. The plugin's loadConfig()
      # reads that file and merges it over its own DEFAULT_CONFIG, so writing
      # only overrides would silently restore plugin defaults for every omitted
      # key — therefore the module writes the *full* resolved config (base
      # settings + overrides via recursiveUpdate, so every key here wins over
      # the base). Keys recognised by the adapter:
      # default_pack, active_pack, volume, enabled, desktop_notifications,
      # pack_rotation, pack_rotation_mode, path_rules, exclude_dirs, ide_rules,
      # mobile_notify, spam_threshold, spam_window_seconds, plus categories
      # (deep-merged over the base map, not replaced).
      peonPingOverrides = mkAttrsOfOption { default = peonPingOverrides; } lib.types.anything;

      skills = mkAttrsOfOption { default = skills; } lib.types.anything;

      # Whether `icedos gc` prunes stale opencode data under
      # ~/.local/share/opencode (unshade-style postGc hook).
      includeInIcedosGc = mkBoolOption { default = includeInIcedosGc; };

      # Retain runtime files/logs newer than this many days when GC runs.
      sessionRetentionDays = mkIntBetweenOption {
        path = "icedos.ai-tools.opencode.sessionRetentionDays";
        source = ./config.toml;
        default = sessionRetentionDays;
      } 1 3650;
    };

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
          inherit (lib) recursiveUpdate;
          inherit (config.icedos.ai-tools.opencode)
            desktopEntry
            extraSettings
            includeInIcedosGc
            peonPingOverrides
            sessionRetentionDays
            ;

          # Shared icedos.ai-tools entries first, so a same-named opencode skill wins.
          skills = config.icedos.ai-tools.skills // config.icedos.ai-tools.opencode.skills;
          inherit (config.icedos.ai-tools) rules;

          peonPingUsers = config.icedos.ai-tools.peon-ping.users or { };
          peonPingEnabled = peonPingUsers != { };

          # Pin sqlite3 for the prune; it's absent from the minimal gc PATH.
          sqlite = "${pkgs.sqlite}/bin/sqlite3";

          # Prune idle opencode runtime data during `icedos gc` (unshade-style).
          opencodeGcHook = ''
            O="''${HOME}/.local/share/opencode"
            [ -d "''${O}" ] || exit 0
            find "''${O}/log" -maxdepth 1 -type f -mtime "+${toString sessionRetentionDays}" -delete 2>/dev/null || true
            find "''${O}/tool-output" -maxdepth 1 -type f -mtime "+${toString sessionRetentionDays}" -delete 2>/dev/null || true
            find "''${O}/storage/session_diff" -maxdepth 1 -type f -name '*.json' -mtime "+${toString sessionRetentionDays}" -delete 2>/dev/null || true
            # snapshot/<id> are git object stores. Dir mtime tracks only the single
            # worktree child's creation, so test file recency, not -mtime.
            for d in "''${O}"/snapshot/*/; do
              [ -d "$d" ] || continue
              case "''${d%/}" in */global) continue ;; esac
              find "$d" -type f -newermt "@$(( $(date +%s) - ${toString sessionRetentionDays}*86400 ))" -print -quit | grep -q . || rm -rf -- "$d"
            done
            # SQLite store dominates disk. Delete per-session children explicitly
            # (FKs are off) and VACUUM only while no live writer holds the lock.
            NOW_MS=$(( $(date +%s) * 1000 ))
            for DB in "''${O}"/*.db; do
              [ -f "$DB" ] || continue
              # Probe units; an empty store or non-ms timestamps must not mass-delete.
              probe=$("${sqlite}" "$DB" "SELECT CASE WHEN (SELECT COUNT(*) FROM session)=0 THEN 'empty' WHEN (SELECT MAX(time_updated) FROM session)>1000000000000 THEN 'ms' ELSE 'x' END;" 2>/dev/null)
              case "$probe" in
                empty) continue ;;
                ms) ;;
                *) log_warn "opencode gc: $DB timestamps not epoch-ms; skipping prune"; continue ;;
              esac
              if ! perr=$("${sqlite}" "$DB" "
                PRAGMA busy_timeout=8000;
                BEGIN;
                CREATE TEMP TABLE _stale AS SELECT id FROM session WHERE time_updated < $NOW_MS - ${toString sessionRetentionDays}*86400000;
                DELETE FROM part WHERE session_id IN (SELECT id FROM _stale);
                DELETE FROM message WHERE session_id IN (SELECT id FROM _stale);
                DELETE FROM session_message WHERE session_id IN (SELECT id FROM _stale);
                DELETE FROM session_input WHERE session_id IN (SELECT id FROM _stale);
                DELETE FROM session_context_epoch WHERE session_id IN (SELECT id FROM _stale);
                DELETE FROM todo WHERE session_id IN (SELECT id FROM _stale);
                DELETE FROM session_share WHERE session_id IN (SELECT id FROM _stale);
                DELETE FROM event WHERE aggregate_id IN (SELECT id FROM _stale);
                DELETE FROM event_sequence WHERE aggregate_id IN (SELECT id FROM _stale);
                DELETE FROM session WHERE id IN (SELECT id FROM _stale);
                DROP TABLE _stale;
                COMMIT;
              " 2>&1 >/dev/null); then
                log_warn "opencode gc: prune failed for $DB: $perr"
              elif ! v_err=$("${sqlite}" "$DB" "PRAGMA wal_checkpoint(TRUNCATE); VACUUM;" 2>&1 >/dev/null); then
                log_warn "opencode gc: VACUUM skipped for $DB (in use?): $v_err"
              fi
            done
          '';
        in
        {
          # No locator: the zed agent bridge cannot resume opencode sessions yet.
          icedos.ai-tools.agent-launch.agents.opencode = {
            command = lib.mkDefault [ "opencode" ];
            label = lib.mkDefault "OpenCode";
          };

          # `icedos gc` prunes stale opencode data per user (unshade-style).
          icedos.system.gc.hooks.postGc = lib.mkIf includeInIcedosGc [ opencodeGcHook ];
          home-manager.sharedModules = [
            # MCP servers come from the shared programs.mcp.servers registry
            # (the apps `agent-memory` module and the config root's mcp.toml
            # both declare entries there), so there is no per-client wiring
            # left on this side.
            (
              { config, ... }:

              {
                programs.opencode = {
                  enable = true;

                  # Pull every server from the shared programs.mcp.servers
                  # registry — including agent-memory — and merge it into
                  # settings.mcp (registry entries win over extraSettings.mcp —
                  # there are none left in the config root; this is opencode's
                  # side of "declare once").
                  enableMcpIntegration = true;

                  settings = recursiveUpdate (
                    {
                      "$schema" = "https://opencode.ai/config.json";

                      # Auto-allow only the skills declared in the module's
                      # `skills` option (an allowlist), not every skill that
                      # happens to exist under ~/.claude/skills.
                      permission.skill = lib.mapAttrs (name: _: "allow") skills;
                    }
                    // lib.optionalAttrs (rules != { }) {
                      # extraSettings.instructions replaces this list (recursiveUpdate keeps the right side).
                      instructions = lib.mapAttrsToList (
                        name: _: "${config.xdg.configHome}/opencode/rules/${name}.md"
                      ) rules;
                    }
                  ) extraSettings;

                  skills = skills;
                };

                xdg.configFile = lib.mapAttrs' (
                  name: text: lib.nameValuePair "opencode/rules/${name}.md" { inherit text; }
                ) rules;

                # Terminal=true: the desktop environment launches the user's default terminal.
                xdg.desktopEntries.opencode = lib.mkIf desktopEntry {
                  name = "OpenCode";
                  comment = "AI coding assistant (terminal UI)";
                  icon = "utilities-terminal";
                  exec = lib.getExe config.programs.opencode.package;
                  terminal = true;
                  categories = [
                    "Development"
                    "Utility"
                  ];
                };
              }
            )

            (
              {
                config,
                lib,
                pkgs,
                ...
              }:

              lib.mkIf peonPingEnabled (
                let
                  userCfg = peonPingUsers.${config.home.username} or null;

                  baseSettings =
                    if userCfg != null then
                      {
                        default_pack = userCfg.defaultPack;
                        volume = userCfg.volume;
                        desktop_notifications = userCfg.desktopNotifications;
                        suppress_subagent_complete = userCfg.suppressSubagentComplete;
                        silent_window_seconds = userCfg.silentWindowSeconds;
                      }
                      // lib.optionalAttrs (userCfg.categories or { } != { }) {
                        categories = userCfg.categories;
                      }
                    else
                      { };

                  fullConfig = lib.recursiveUpdate baseSettings peonPingOverrides;
                in
                {
                  # Upstream plugin writes an OSC set-title escape to stdout with no
                  # TTY guard. Harmless in the TUI, but under Zed's ACP mode opencode's
                  # stdout is the JSON-RPC pipe, so the escape corrupts the stream and
                  # Zed hangs at "loading…". Guard the write on an interactive TTY.
                  xdg.configFile."opencode/plugins/peon-ping.ts".source =
                    pkgs.runCommand "peon-ping-opencode.ts" { }
                      ''
                        substitute ${inputs.peon-ping}/adapters/opencode/peon-ping.ts "$out" \
                          --replace-fail \
                            'process.stdout.write(`\x1b]0;' \
                            'if (process.stdout.isTTY) process.stdout.write(`\x1b]0;'
                      '';

                  xdg.configFile."opencode/peon-ping/config.json" = lib.mkIf (userCfg != null) {
                    source = (pkgs.formats.json { }).generate "opencode-peon-ping.json" fullConfig;
                  };
                }
              )
            )
          ];

          icedos.system.tips.list = lib.optionals includeInIcedosGc [
            "icedos gc also clears out old opencode sessions and logs."
          ];
        }
      )
    ];

  meta = {
    name = "opencode";
    dependencies = [ { modules = [ "agent-launch" ]; } ];
  };
}
