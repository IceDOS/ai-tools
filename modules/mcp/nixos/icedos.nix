{ icedosLib, lib, ... }:

{
  options.icedos.ai-tools.nixos-mcp = {
    skills = icedosLib.mkAttrsOfOption {
      default = (lib.importTOML ./config.toml).icedos.ai-tools.nixos-mcp.skills;
    } lib.types.lines;

    # How the MCP gateway runs this server: global, per-agent or stdio.
    gatewayMode =
      icedosLib.mkEnumOption
        {
          path = "icedos.ai-tools.nixos-mcp.gatewayMode";
          source = ./config.toml;
          default = (lib.importTOML ./config.toml).icedos.ai-tools.nixos-mcp.gatewayMode;
        }
        [
          "global"
          "per-agent"
          "stdio"
        ];
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

        {
          environment.systemPackages = [ pkgs.mcp-nixos ];
          icedos.ai-tools.mcp-servers.nixos.command = lib.getExe pkgs.mcp-nixos;
          icedos.ai-tools.mcp-gateway-modes.nixos = lib.mkDefault config.icedos.ai-tools.nixos-mcp.gatewayMode;
          icedos.ai-tools.skills.pool = lib.mapAttrs (
            _: lib.mkDefault
          ) config.icedos.ai-tools.nixos-mcp.skills;
        }
      )
    ];

  meta = {
    name = "nixos-mcp";
    description = "MCP server for nixpkgs packages, NixOS and Home Manager options, flakes and the NixOS wiki";
  };
}
