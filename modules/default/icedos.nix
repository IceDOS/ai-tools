{ icedosLib, lib, ... }:

{
  options.icedos.ai-tools =
    let
      inherit (icedosLib) mkAttrsOfOption;
      inherit ((lib.importTOML ./config.toml).icedos.ai-tools) mcp-servers rules skills;
    in
    {
      # Rules every agent loads: name = markdown.
      rules = mkAttrsOfOption { default = rules; } lib.types.lines;

      # Skills every agent loads: name = full SKILL.md content.
      skills.pool = mkAttrsOfOption { default = skills.pool; } lib.types.lines;

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
        { config, lib, ... }:

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
        in
        lib.mkIf (servers != { }) {
          # mkDefault per server, so a same-named home-manager programs.mcp.servers entry wins.
          home-manager.sharedModules = [
            {
              programs.mcp = {
                enable = lib.mkDefault true;
                servers = lib.mapAttrs (_: lib.mkDefault) resolved;
              };
            }
          ];

          environment.etc."mcp/servers.json".text = builtins.toJSON { mcpServers = standalone; };
        }
      )
    ];

  meta.name = "default";
}
