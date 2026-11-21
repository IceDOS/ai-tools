{ icedosLib, lib, ... }:

let
  agents = [
    "claude-code"
    "antigravity"
    "opencode"
    "pi-agent"
  ];
in
{
  options.icedos.ai-tools =
    let
      inherit (icedosLib) mkAttrsOfOption mkStrListOption;
      inherit ((lib.importTOML ./config.toml).icedos.ai-tools)
        mcp-servers
        per-agent
        plugins
        rules
        skills
        ;
    in
    {
      # Rules every agent loads: name = markdown.
      rules = mkAttrsOfOption { default = rules; } lib.types.lines;

      # Skills every agent loads: name = full SKILL.md content.
      skills.pool = mkAttrsOfOption { default = skills.pool; } lib.types.lines;

      # Global plugin storage: name = store path. Agents load ones listed in per-agent.<agent>.globalPlugins.
      plugins.pool = mkAttrsOfOption { default = plugins.pool; } (
        lib.types.either lib.types.path lib.types.str
      );

      # Per-agent overrides and additions: skills, plugins, globalPlugins, and excluded skill names.
      per-agent = lib.genAttrs agents (agent: {
        skills = mkAttrsOfOption { default = per-agent.${agent}.skills; } lib.types.lines;
        plugins = mkAttrsOfOption { default = per-agent.${agent}.plugins; } (
          lib.types.either lib.types.path lib.types.str
        );
        globalPlugins = mkStrListOption { default = per-agent.${agent}.globalPlugins; };
        excludeSkills = mkStrListOption { default = per-agent.${agent}.excludeSkills; };
      });

      # Shared agents tree derivation: holding global/, per-agent/, and view/ symlinks.
      agents-dir = lib.mkOption {
        type = lib.types.package;
        readOnly = true;
        internal = true;
        description = "Derivation containing the shared agents tree.";
      };

      # Per-agent skill views: list of skill names present in view/<agent>/skills.
      agent-views = lib.genAttrs agents (_: {
        skills = lib.mkOption {
          type = lib.types.listOf lib.types.str;
          readOnly = true;
          internal = true;
          description = "List of skill names in this agent's skills view.";
        };
      });

      # MCP servers every agent loads: name = programs.mcp.servers entry.
      mcp-servers = mkAttrsOfOption { default = mcp-servers; } lib.types.anything;

      # How the MCP gateway runs each server: name = global (one process for every
      # agent), per-agent (one per agent session) or stdio (not routed). Each MCP
      # module fills in its own entry; only the mcp-gateway module reads the result.
      mcp-gateway-modes = mkAttrsOfOption { default = { }; } (
        lib.types.enum [
          "global"
          "per-agent"
          "stdio"
        ]
      );

      # URLs of the servers the MCP gateway fronts: name = http url. Only the
      # mcp-gateway module fills it; internal, since it just shapes what agents get.
      mcp-http-endpoints = mkAttrsOfOption {
        default = { };
        internal = true;
      } lib.types.str;
    };

  outputs.nixosModules =
    { ... }:
    [
      (
        {
          config,
          lib,
          pkgs,
          ...
        }:

        let
          servers = config.icedos.ai-tools.mcp-servers;
          endpoints = config.icedos.ai-tools.mcp-http-endpoints;

          # A routed server is dialled through the gateway; the rest stay as registered.
          # Keep its timeout, since the http entry has no command the agents could read it from.
          resolved = lib.mapAttrs (
            name: server:
            if endpoints ? ${name} then
              {
                type = "http";
                url = endpoints.${name};
              }
              // lib.optionalAttrs (server ? timeout) { inherit (server) timeout; }
            else
              server
          ) servers;

          # Agents outside Home Manager read the common mcpServers JSON, where remote servers need a type.
          standalone = lib.mapAttrs (
            _: server: lib.optionalAttrs (server ? url) { type = "http"; } // server
          ) resolved;

          globalSkills = config.icedos.ai-tools.skills.pool;
          globalPlugins = config.icedos.ai-tools.plugins.pool;
          perAgentConfig = config.icedos.ai-tools.per-agent;

          globalSkillFiles = lib.mapAttrs (
            name: content: pkgs.writeText "${name}-SKILL.md" content
          ) globalSkills;

          perAgentSkillFiles = lib.genAttrs agents (
            agent:
            lib.mapAttrs (name: content: pkgs.writeText "${agent}-${name}-SKILL.md" content) (
              perAgentConfig.${agent}.skills or { }
            )
          );

          # Symlinks: global + per-agent merged, per-agent wins, minus excludeSkills.
          viewSkills = lib.genAttrs agents (
            agent:
            let
              pSkills = perAgentConfig.${agent}.skills or { };
              exclude = perAgentConfig.${agent}.excludeSkills or [ ];
            in
            lib.filterAttrs (name: _: !lib.elem name exclude) (
              (lib.mapAttrs (name: _: "../../../global/skills/${name}") globalSkills)
              // (lib.mapAttrs (name: _: "../../../per-agent/${agent}/skills/${name}") pSkills)
            )
          );

          agentsTree = pkgs.runCommand "icedos-agents" { } ''
            mkdir -p "$out"/global/skills "$out"/global/plugins

            ${lib.concatStringsSep "\n" (
              lib.mapAttrsToList (name: file: ''
                mkdir -p "$out"/global/skills/${lib.escapeShellArg name}
                cp "${file}" "$out"/global/skills/${lib.escapeShellArg name}/SKILL.md
              '') globalSkillFiles
            )}

            ${lib.concatStringsSep "\n" (
              lib.mapAttrsToList (name: path: ''
                ln -s ${lib.escapeShellArg (toString path)} "$out"/global/plugins/${lib.escapeShellArg name}
              '') globalPlugins
            )}

            ${lib.concatStringsSep "\n" (
              map (
                agent:
                let
                  pSkills = perAgentSkillFiles.${agent};
                  pPlugins = perAgentConfig.${agent}.plugins or { };
                  vSkills = viewSkills.${agent};
                in
                ''
                  mkdir -p "$out"/per-agent/${agent}/skills "$out"/per-agent/${agent}/plugins "$out"/view/${agent}/skills

                  ${lib.concatStringsSep "\n" (
                    lib.mapAttrsToList (name: file: ''
                      mkdir -p "$out"/per-agent/${agent}/skills/${lib.escapeShellArg name}
                      cp "${file}" "$out"/per-agent/${agent}/skills/${lib.escapeShellArg name}/SKILL.md
                    '') pSkills
                  )}

                  ${lib.concatStringsSep "\n" (
                    lib.mapAttrsToList (name: path: ''
                      ln -s ${lib.escapeShellArg (toString path)} "$out"/per-agent/${agent}/plugins/${lib.escapeShellArg name}
                    '') pPlugins
                  )}

                  ${lib.concatStringsSep "\n" (
                    lib.mapAttrsToList (name: target: ''
                      ln -s ${lib.escapeShellArg target} "$out"/view/${agent}/skills/${lib.escapeShellArg name}
                    '') vSkills
                  )}
                ''
              ) agents
            )}
          '';
        in
        {
          icedos.ai-tools.agents-dir = agentsTree;
          icedos.ai-tools.agent-views = lib.genAttrs agents (agent: {
            skills = lib.attrNames viewSkills.${agent};
          });

          # mkDefault per server, so a same-named home-manager programs.mcp.servers entry wins.
          home-manager.sharedModules = [
            {
              xdg.dataFile."icedos/agents".source = config.icedos.ai-tools.agents-dir;
            }
          ]
          ++ lib.optionals (servers != { }) [
            {
              programs.mcp = {
                enable = lib.mkDefault true;
                servers = lib.mapAttrs (_: lib.mkDefault) resolved;
              };
            }
          ];

          environment.etc = lib.optionalAttrs (servers != { }) {
            "mcp/servers.json".text = builtins.toJSON { mcpServers = standalone; };
          };
        }
      )
    ];

  meta.name = "default";
}
