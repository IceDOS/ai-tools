{ icedosLib, lib, ... }:

let
  defaults = (lib.importTOML ./config.toml).icedos.ai-tools.mcp-server;
in
{
  options.icedos.ai-tools.mcp-server = {
    # Tool-call timeout in milliseconds for agents that honour it.
    timeout = icedosLib.mkIntBetweenOption {
      path = "icedos.ai-tools.mcp-server.timeout";
      source = ./config.toml;
      default = defaults.timeout;
    } 1000 86400000;

    build = {
      hosts = icedosLib.mkStrListOption { default = defaults.build.hosts; };

      minFreeGb = icedosLib.mkIntBetweenOption {
        path = "icedos.ai-tools.mcp-server.build.minFreeGb";
        source = ./config.toml;
        default = defaults.build.minFreeGb;
      } 0 100000;

      pressureLimit = icedosLib.mkIntBetweenOption {
        path = "icedos.ai-tools.mcp-server.build.pressureLimit";
        source = ./config.toml;
        default = defaults.build.pressureLimit;
      } 0 100;
    };

    storage = {
      drives = icedosLib.mkAttrsOfOption { default = defaults.storage.drives; } (
        lib.types.enum [
          "random"
          "sequential"
        ]
      );

      priority = icedosLib.mkStrListOption { default = defaults.storage.priority; };

      reserveGb = icedosLib.mkIntBetweenOption {
        path = "icedos.ai-tools.mcp-server.storage.reserveGb";
        source = ./config.toml;
        default = defaults.storage.reserveGb;
      } 0 100000;

      largeGb = icedosLib.mkIntBetweenOption {
        path = "icedos.ai-tools.mcp-server.storage.largeGb";
        source = ./config.toml;
        default = defaults.storage.largeGb;
      } 0 100000;

      maxFlakeCopyMb = icedosLib.mkIntBetweenOption {
        path = "icedos.ai-tools.mcp-server.storage.maxFlakeCopyMb";
        source = ./config.toml;
        default = defaults.storage.maxFlakeCopyMb;
      } 0 1000000;
    };

    # Skills for this module (name = SKILL.md), loaded by every agent via icedos.ai-tools.skills.
    skills = icedosLib.mkAttrsOfOption { default = defaults.skills; } lib.types.lines;
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
          icedosMcpServer = pkgs.python3Packages.buildPythonApplication {
            pname = "icedos-mcp-server";
            version = "0.1.0";
            pyproject = true;

            src = builtins.path {
              path = ./src;
              name = "icedos-mcp-server-src";
              filter =
                path: _:
                let
                  rel = lib.removePrefix (toString ./src + "/") (toString path);
                in
                rel == "pyproject.toml" || rel == "icedos_mcp" || lib.hasPrefix "icedos_mcp/" rel;
            };

            build-system = [ pkgs.python3Packages.hatchling ];

            dependencies = with pkgs.python3Packages; [
              mcp
              tomlkit
              httpx
            ];

            pythonImportsCheck = [ "icedos_mcp" ];

            mainProgram = "icedos-mcp-server";
          };

          settingsFile = pkgs.writeText "icedos-mcp-settings.json" (
            builtins.toJSON { inherit (config.icedos.ai-tools.mcp-server) build storage; }
          );
        in
        {
          # Skills for this module, added to the shared icedos.ai-tools.skills pool.
          icedos.ai-tools.skills = lib.mapAttrs (_: lib.mkDefault) config.icedos.ai-tools.mcp-server.skills;

          environment.systemPackages = [ icedosMcpServer ];

          icedos.ai-tools.mcp-servers.icedos = {
            command = "icedos";
            args = [
              "mcp"
              "serve"
            ];
            inherit (config.icedos.ai-tools.mcp-server) timeout;
          };

          icedos.system.toolset.commands = [
            {
              command = "mcp";
              help = "MCP server for AI agent integration";

              commands = [
                {
                  command = "serve";
                  help = "start the IceDOS MCP server (stdio); tools use a temp config copy unless --live";
                  script = "ICEDOS_CONFIG_DIR=${config.icedos.configurationLocation}/.. ICEDOS_MCP_SETTINGS=${settingsFile} exec ${icedosMcpServer}/bin/icedos-mcp-server \"$@\"";
                  completion.files = false;
                }
              ];
            }
          ];

          icedos.system.tips.list = [
            "Add icedos mcp serve to your AI assistant so it can manage this machine for you."
            "An AI assistant connected with icedos mcp serve can turn modules on or off for you."
            "An AI assistant connected with icedos mcp serve can find and explain any setting."
            "An AI assistant connected with icedos mcp serve can check health and free disk space."
          ];
        }
      )
    ];

  meta = {
    name = "mcp-server";
    description = "MCP server for AI agent integration with IceDOS";
  };
}
