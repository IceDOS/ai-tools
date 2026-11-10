{ icedosLib, lib, ... }:

let
  inherit (icedosLib)
    mkAttrsOfOption
    mkIntBetweenOption
    mkStrListOption
    mkStrOption
    ;
  defaults = (lib.importTOML ./config.toml).icedos.ai-tools.opencart-mcp;
in
{
  options.icedos.ai-tools.opencart-mcp = {
    # KEY=value file with secrets such as OPENCART_DB_PASS; "" reads none.
    envFile = mkStrOption { default = defaults.envFile; };

    # Tool-call timeout in milliseconds for agents that honour it.
    timeout = mkIntBetweenOption {
      path = "icedos.ai-tools.opencart-mcp.timeout";
      source = ./config.toml;
      default = defaults.timeout;
    } 1000 86400000;

    # Plain OPENCART_* settings passed to the server.
    env = mkAttrsOfOption { default = defaults.env; } lib.types.str;

    # Skills shipped in ./skills, references inlined.
    shippedSkills = mkStrListOption { default = defaults.shippedSkills; };

    # Extra skills, name = full SKILL.md content; replaces a same-named shipped skill.
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
          cfg = config.icedos.ai-tools.opencart-mcp;
          package = import ./package.nix { inherit pkgs; };
          shipped = import ../lib/upstream-skills.nix { inherit lib; } ./skills cfg.shippedSkills;
        in
        {
          environment.systemPackages = [ package ];

          # Secrets stay out of the store; the wrapper sources OPENCART_ENV_FILE at start.
          icedos.ai-tools.mcp-servers.opencart = {
            command = lib.getExe package;
            env = cfg.env // lib.optionalAttrs (cfg.envFile != "") { OPENCART_ENV_FILE = cfg.envFile; };
            inherit (cfg) timeout;
          };

          icedos.ai-tools.skills = lib.mapAttrs (_: lib.mkDefault) (shipped // cfg.skills);
        }
      )
    ];

  meta = {
    name = "opencart-mcp";
    description = "OpenCart MCP server over SSH, direct MySQL or DDEV, secrets read from an env file";
  };
}
