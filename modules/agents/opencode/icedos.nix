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
        extra-settings
        includeInIcedosGc
        sessionRetentionDays
        skills
        ;
    in
    {
      # Whether the launcher installs a desktop entry opening in the default terminal.
      desktopEntry = mkBoolOption { default = desktopEntry; };

      extra-settings = mkAttrsOfOption { default = extra-settings; } lib.types.anything;

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
    { repoUrl, ... }:
    [
      (
        {
          config,
          lib,
          pkgs,
          ...
        }:
        let
          hasAccounts = import ../../integrations/ai-accounts/lib/has-accounts.nix { inherit lib; } config;
          wrapAccounts = import ../../integrations/ai-accounts/lib/wrap.nix { inherit lib pkgs; };
          aiAccountsOn = icedosLib.hasModule {
            inherit config repoUrl;
            name = "ai-accounts";
          };

          inherit (lib) recursiveUpdate;
          inherit (config.icedos.ai-tools.opencode)
            desktopEntry
            extra-settings
            includeInIcedosGc
            sessionRetentionDays
            ;

          # Shared icedos.ai-tools entries first, so a same-named opencode skill wins.
          skills = config.icedos.ai-tools.skills // config.icedos.ai-tools.opencode.skills;
          inherit (config.icedos.ai-tools) rules;

          # Pin sqlite3 for the prune; it's absent from the minimal gc PATH.
          sqlite = "${pkgs.sqlite}/bin/sqlite3";

          # Prune idle opencode runtime data during `icedos gc` in nh clean style.
          opencodeGcHook = ''
            O="''${HOME}/.local/share/opencode"
            [ -d "''${O}" ] || exit 0

            gc_section "''${O}" "path"

            if [ -d "''${O}/log" ]; then
              while IFS= read -r -d $'\0' f; do
                gc_del "$f"
              done < <(find "''${O}/log" -maxdepth 1 -type f -mtime "+${toString sessionRetentionDays}" -print0 2>/dev/null)
            fi

            if [ -d "''${O}/tool-output" ]; then
              while IFS= read -r -d $'\0' f; do
                gc_del "$f"
              done < <(find "''${O}/tool-output" -maxdepth 1 -type f -mtime "+${toString sessionRetentionDays}" -print0 2>/dev/null)
            fi

            if [ -d "''${O}/storage/session_diff" ]; then
              while IFS= read -r -d $'\0' f; do
                gc_del "$f"
              done < <(find "''${O}/storage/session_diff" -maxdepth 1 -type f -name '*.json' -mtime "+${toString sessionRetentionDays}" -print0 2>/dev/null)
            fi

            # snapshot/<id> are git object stores.
            if [ -d "''${O}/snapshot" ]; then
              shopt -s nullglob
              for d in "''${O}"/snapshot/*/; do
                [ -d "$d" ] || continue
                case "''${d%/}" in */global) continue ;; esac
                find "$d" -type f -newermt "@$(( $(date +%s) - ${toString sessionRetentionDays}*86400 ))" -print -quit | grep -q . || gc_del "''${d%/}"
              done
              shopt -u nullglob
            fi

            # SQLite store dominates disk. Delete per-session children explicitly.
            NOW_MS=$(( $(date +%s) * 1000 ))
            CUTOFF_MS=$(( NOW_MS - ${toString sessionRetentionDays}*86400000 ))
            shopt -s nullglob
            for DB in "''${O}"/*.db; do
              [ -f "$DB" ] || continue
              probe=$("${sqlite}" "$DB" "SELECT CASE WHEN (SELECT COUNT(*) FROM session)=0 THEN 'empty' WHEN (SELECT MAX(time_updated) FROM session)>1000000000000 THEN 'ms' ELSE 'x' END;" 2>/dev/null)
              case "$probe" in
                empty) continue ;;
                ms) ;;
                *) log_warn "opencode gc: $DB timestamps not epoch-ms; skipping prune"; continue ;;
              esac

              stale_count=$("${sqlite}" "$DB" "SELECT COUNT(*) FROM session WHERE time_updated < $CUTOFF_MS;" 2>/dev/null) || stale_count=0
              [ "$stale_count" -gt 0 ] || continue

              gc_del_entry "$DB ($stale_count stale sessions)"
              db_before=$(du -sk -- "$DB" 2>/dev/null | cut -f1) || db_before=0

              if ! perr=$("${sqlite}" "$DB" "
                PRAGMA busy_timeout=8000;
                BEGIN;
                CREATE TEMP TABLE _stale AS SELECT id FROM session WHERE time_updated < $CUTOFF_MS;
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
              else
                db_after=$(du -sk -- "$DB" 2>/dev/null | cut -f1) || db_after=0
                if [ "$db_before" -gt "$db_after" ]; then
                  gc_freed_kb $(( db_before - db_after ))
                fi
              fi
            done
            shopt -u nullglob
            exit 0
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
                  package = lib.mkIf (aiAccountsOn && hasAccounts "opencode") (wrapAccounts {
                    agent = "opencode";
                    package = pkgs.opencode;
                    bin = "opencode";
                  });
                  enable = true;

                  # Pull every server from the shared programs.mcp.servers
                  # registry — including agent-memory — and merge it into
                  # settings.mcp (registry entries win over extra-settings.mcp —
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
                      # extra-settings.instructions replaces this list (recursiveUpdate keeps the right side).
                      instructions = lib.mapAttrsToList (
                        name: _: "${config.xdg.configHome}/opencode/rules/${name}.md"
                      ) rules;
                    }
                  ) extra-settings;

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
