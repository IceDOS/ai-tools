{ icedosLib, lib, ... }:

let
  inherit (icedosLib)
    mkAttrsOfOption
    mkListOption
    mkNonEmptyStrOption
    mkStrListOption
    ;

  defaults = (lib.importTOML ./config.toml).icedos.ai-tools.aws-mcp;
in
{
  options.icedos.ai-tools.aws-mcp = {
    # Managed AWS MCP endpoint the local proxy signs requests for.
    url = mkNonEmptyStrOption { default = defaults.url; };

    extraArgs = mkStrListOption { default = defaults.extraArgs; };

    # Agent Toolkit plugins installed for Claude Code.
    plugins = mkListOption { default = defaults.plugins; } (
      lib.types.enum [
        "aws-agents"
        "aws-agents-for-devsecops"
        "aws-core"
        "aws-data-analytics"
        "aws-startup-advisor"
      ]
    );

    skills = mkAttrsOfOption { default = defaults.skills; } lib.types.lines;
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
          cfg = config.icedos.ai-tools.aws-mcp;
          package = import ./package.nix { inherit pkgs; };
        in
        {
          environment.systemPackages = [ package.proxy ];

          icedos.ai-tools.mcp-servers.aws-mcp = {
            command = lib.getExe package.proxy;
            args = [ cfg.url ] ++ cfg.extraArgs;
          };

          icedos.ai-tools.skills = lib.mapAttrs (_: lib.mkDefault) cfg.skills;

          home-manager.sharedModules = [
            {
              programs.claude-code.plugins = lib.genAttrs cfg.plugins package.plugin;
            }
          ];
        }
      )
    ];

  meta = {
    name = "aws-mcp";
    description = "AWS MCP server proxy and Agent Toolkit for AWS plugins for Claude Code";
  };
}
