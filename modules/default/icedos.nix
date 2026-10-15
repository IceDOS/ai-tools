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
      skills = mkAttrsOfOption { default = skills; } lib.types.lines;

      # MCP servers every agent loads: name = programs.mcp.servers entry.
      mcp-servers = mkAttrsOfOption { default = mcp-servers; } lib.types.anything;
    };

  outputs.nixosModules =
    { ... }:
    [
      (
        { config, lib, ... }:

        let
          servers = config.icedos.ai-tools.mcp-servers;

          # Agents outside Home Manager read the common mcpServers JSON, where remote servers need a type.
          standalone = lib.mapAttrs (
            _: server: lib.optionalAttrs (server ? url) { type = "http"; } // server
          ) servers;
        in
        lib.mkIf (servers != { }) {
          # mkDefault per server, so a same-named home-manager programs.mcp.servers entry wins.
          home-manager.sharedModules = [
            {
              programs.mcp = {
                enable = lib.mkDefault true;
                servers = lib.mapAttrs (_: lib.mkDefault) servers;
              };
            }
          ];

          environment.etc."mcp/servers.json".text = builtins.toJSON { mcpServers = standalone; };
        }
      )
    ];

  meta.name = "default";
}
