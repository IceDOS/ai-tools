# The VPN rotation daemon and its socket client.
{ lib, pkgs }:
vpn:
let
  serviceScript = pkgs.writers.writePython3Bin "vpn-rotate-service-unwrapped" {
    flakeIgnore = [ "E501" ];
    libraries = [
      pkgs.python3Packages.proton-vpn-api-core
      pkgs.python3Packages.proton-keyring-linux
    ];
  } (builtins.readFile ./vpn_rotate_service.py);
in
{
  service = pkgs.runCommand "vpn-rotate-service" { nativeBuildInputs = [ pkgs.makeWrapper ]; } ''
    makeWrapper ${serviceScript}/bin/vpn-rotate-service-unwrapped $out/bin/vpn-rotate-service \
      --set VPN_ROTATE_WIREPROXY ${pkgs.wireproxy}/bin/wireproxy \
      --set VPN_ROTATE_PROXY_PORT ${toString vpn.proxyPort} \
      --set VPN_ROTATE_DIRECT_TTL_S ${toString vpn.directRetrySeconds} \
      --set VPN_ROTATE_COOLDOWN_S ${toString vpn.cooldownSeconds} \
      --set VPN_ROTATE_MAX_PER_HOUR ${toString vpn.maxPerHour} \
      --set VPN_ROTATE_COUNTRIES ${lib.escapeShellArg (lib.concatStringsSep "," vpn.countries)} \
      --set VPN_ROTATE_EXCLUDED_COUNTRIES ${lib.escapeShellArg (lib.concatStringsSep "," vpn.excludedCountries)} \
      --set VPN_ROTATE_BUN ${pkgs.bun}/bin/bun
  '';

  rotate = pkgs.writers.writePython3Bin "vpn-rotate" {
    flakeIgnore = [ "E501" ];
  } (builtins.readFile ./vpn_rotate.py);
}
