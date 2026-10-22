{ icedosLib, lib, ... }:

let
  inherit (icedosLib) mkAttrsOfOption mkIntBetweenOption mkNonEmptyStrOption;
  defaults = (lib.importTOML ./config.toml).icedos.ai-tools.pagespeed-insights-mcp;
in
{
  options.icedos.ai-tools.pagespeed-insights-mcp = {
    # File outside the Nix store holding the Google API key.
    apiKeyFile = mkNonEmptyStrOption { default = defaults.apiKeyFile; };

    # Tool-call timeout in milliseconds for agents that honour it.
    timeout = mkIntBetweenOption {
      path = "icedos.ai-tools.pagespeed-insights-mcp.timeout";
      source = ./config.toml;
      default = defaults.timeout;
    } 1000 86400000;

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
          cfg = config.icedos.ai-tools.pagespeed-insights-mcp;
          package = import ./package.nix { inherit pkgs; };
        in
        {
          environment.systemPackages = [ package ];

          # The key stays out of the store; the wrapper reads GOOGLE_API_KEY_FILE at start.
          icedos.ai-tools.mcp-servers.pagespeed-insights = {
            command = lib.getExe package;
            env.GOOGLE_API_KEY_FILE = cfg.apiKeyFile;
            inherit (cfg) timeout;
          };

          icedos.ai-tools.skills = lib.mapAttrs (_: lib.mkDefault) cfg.skills;
        }
      )
    ];

  meta = {
    name = "pagespeed-insights-mcp";
    description = "Google PageSpeed Insights and Chrome UX Report MCP server, API key read from a file";
  };
}
