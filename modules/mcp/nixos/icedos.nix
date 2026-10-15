{ icedosLib, lib, ... }:

{
  options.icedos.ai-tools.nixos-mcp.skills = icedosLib.mkAttrsOfOption {
    default = (lib.importTOML ./config.toml).icedos.ai-tools.nixos-mcp.skills;
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

        {
          environment.systemPackages = [ pkgs.mcp-nixos ];
          icedos.ai-tools.mcp-servers.nixos.command = lib.getExe pkgs.mcp-nixos;
          icedos.ai-tools.skills = lib.mapAttrs (_: lib.mkDefault) config.icedos.ai-tools.nixos-mcp.skills;
        }
      )
    ];

  meta = {
    name = "nixos-mcp";
    description = "MCP server for nixpkgs packages, NixOS and Home Manager options, flakes and the NixOS wiki";
  };
}
