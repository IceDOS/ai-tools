{ icedosLib, lib, ... }:

{
  options.icedos.ai-tools.agent-relay =
    let
      inherit (icedosLib) mkBoolOption mkIntBetweenOption;
      inherit ((lib.importTOML ./config.toml).icedos.ai-tools.agent-relay) hopLimit statusSegment;
    in
    {
      hopLimit = mkIntBetweenOption {
        path = "icedos.ai-tools.agent-relay.hopLimit";
        source = ./config.toml;
        default = hopLimit;
      } 1 100;

      statusSegment = mkBoolOption { default = statusSegment; };
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
          inherit (lib) mkIf optionalAttrs replaceStrings;
          cfg = config.icedos.ai-tools.agent-relay;
          pkg = pkgs.callPackage ./package.nix { };
          lib' = "${pkg}/lib/agent-relay";
          has = name: icedosLib.hasModule { inherit config repoUrl name; };

          # opencode resolves @opencode-ai/plugin from ~/.config/opencode; core comes from the store.
          opencodeServer =
            replaceStrings
              [ "./core.ts" "./opencode-hooks.ts" ]
              [ "${lib'}/core.ts" "${lib'}/opencode-hooks.ts" ]
              (builtins.readFile ./src/opencode-server.ts);

          claudeMcpConfig = pkgs.writeText "agent-relay-mcp.json" (
            builtins.toJSON {
              mcpServers.agent-relay = {
                type = "stdio";
                command = "${pkg}/bin/agent-relay-mcp";
                args = [ "claude" ];
              };
            }
          );
        in
        {
          icedos.ai-tools =
            optionalAttrs (has "pi-agent") {
              pi-agent.extensions.inline.agent-relay = ''
                export { default } from "${lib'}/pi-extension.ts";
              '';
            }
            // optionalAttrs (has "claude-code") {
              claude-code.extraArgs = [
                "--mcp-config"
                "${claudeMcpConfig}"
                "--dangerously-load-development-channels"
                "server:agent-relay"
              ];
            }
            // optionalAttrs (has "antigravity") {
              antigravity.extra-mcp-servers.agent-relay = {
                command = "${pkg}/bin/agent-relay-mcp";
                args = [ "agy" ];
              };
            }
            // optionalAttrs (has "agent-launch") {
              agent-launch.relayIds = true;
            };

          home-manager.sharedModules = [
            (
              { config, lib, ... }:
              {
                xdg.configFile."agent-relay/config.json".text = builtins.toJSON { inherit (cfg) hopLimit; };

                home.sessionVariables = mkIf (!cfg.statusSegment) { ALIMIT_RELAY_SEGMENT = "0"; };

                programs.claude-code = mkIf (config.programs.claude-code.enable or false) {
                  settings.hooks.UserPromptSubmit = [
                    {
                      hooks = [
                        {
                          type = "command";
                          command = "${pkg}/bin/agent-relay-hook claude-prompt";
                        }
                      ];
                    }
                  ];
                };

                programs.opencode.tui.plugin = mkIf (config.programs.opencode.enable or false) [
                  "${lib'}/opencode-tui.ts"
                ];
                xdg.configFile."opencode/plugins/agent-relay.ts" = mkIf (config.programs.opencode.enable or false) {
                  text = opencodeServer;
                };

                home.file = mkIf (has "antigravity") {
                  ".gemini/config/hooks.json".text = builtins.toJSON {
                    agent-relay = {
                      enabled = true;
                      SessionStart = [
                        {
                          type = "command";
                          command = "${pkg}/bin/agent-relay-hook agy-session-start";
                          timeout = 30;
                        }
                      ];
                      PreInvocation = [
                        {
                          type = "command";
                          command = "${pkg}/bin/agent-relay-hook agy-pre-invocation";
                          timeout = 30;
                        }
                      ];
                      Stop = [
                        {
                          type = "command";
                          command = "${pkg}/bin/agent-relay-hook agy-stop";
                          timeout = 30;
                        }
                      ];
                    };
                  };
                };
              }
            )
          ];
        }
      )
    ];

  meta.name = "agent-relay";
}
