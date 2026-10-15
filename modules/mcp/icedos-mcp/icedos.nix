{ icedosLib, lib, ... }:

{
  # Skills for this module (name = SKILL.md), loaded by every agent via icedos.ai-tools.skills.
  options.icedos.ai-tools.mcp-server.skills = icedosLib.mkAttrsOfOption {
    default = (lib.importTOML ./config.toml).icedos.ai-tools.mcp-server.skills;
  } lib.types.lines;

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
        in
        {
          # Skills for this module, added to the shared icedos.ai-tools.skills pool.
          icedos.ai-tools.skills = lib.mapAttrs (_: lib.mkDefault) config.icedos.ai-tools.mcp-server.skills;

          environment.systemPackages = [ icedosMcpServer ];

          icedos.system.toolset.commands = [
            {
              command = "mcp";
              help = "MCP server for AI agent integration";

              commands = [
                {
                  command = "serve";
                  help = "start the IceDOS MCP server (stdio); tools use a temp config copy unless --live";
                  script = "ICEDOS_CONFIG_DIR=${config.icedos.configurationLocation}/.. exec ${icedosMcpServer}/bin/icedos-mcp-server \"$@\"";
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
