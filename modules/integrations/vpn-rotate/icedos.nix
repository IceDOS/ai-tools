{ icedosLib, lib, ... }:

let
  inherit (icedosLib) mkIntBetweenOption mkStrListOption;
  defaults = (lib.importTOML ./config.toml).icedos.ai-tools.vpn-rotate;

  int =
    key:
    mkIntBetweenOption {
      path = "icedos.ai-tools.vpn-rotate.${key}";
      source = ./config.toml;
      default = defaults.${key};
    };
in
{
  options.icedos.ai-tools.vpn-rotate = {
    countries = mkStrListOption { default = defaults.countries; };
    excludedCountries = mkStrListOption { default = defaults.excludedCountries; };
    cooldownSeconds = int "cooldownSeconds" 0 86400;
    maxPerHour = int "maxPerHour" 0 60;
    proxyPort = int "proxyPort" 1 65535;
    directRetrySeconds = int "directRetrySeconds" 60 86400;
  };

  # pi-agent and opencode wire their retry hooks to this daemon when the module is enabled.
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
          vpnPkgs = import ./package.nix { inherit lib pkgs; } config.icedos.ai-tools.vpn-rotate;
        in
        {
          nixpkgs.overlays = [
            (_: _: {
              vpn-rotate = vpnPkgs.rotate;
              vpn-rotate-service = vpnPkgs.service;
            })
          ];

          # proton-vpn-cli is only there for `protonvpn signin`.
          environment.systemPackages = [ pkgs.proton-vpn-cli ];

          home-manager.sharedModules = [
            {
              systemd.user.services.vpn-rotate = {
                Unit.Description = "VPN rotation daemon for rate-limited agent providers";
                Service = {
                  ExecStart = "${vpnPkgs.service}/bin/vpn-rotate-service";
                  # The probe takes opencode Zen's URL and user-agent from this file.
                  Environment = [
                    "VPN_ROTATE_OPENCODE_FREE_MODULE=${
                      pkgs.replaceVars ../../agents/pi-agent/extensions/free-models.ts {
                        opencodeVersion = pkgs.opencode.version;
                      }
                    }"
                  ];
                  Restart = "always";
                  RestartSec = "5s";
                };
                Install.WantedBy = [ "default.target" ];
              };
            }
          ];
        }
      )
    ];

  meta = {
    name = "vpn-rotate";
    description = "VPN rotation daemon for rate-limited pi-agent and opencode free models";
  };
}
