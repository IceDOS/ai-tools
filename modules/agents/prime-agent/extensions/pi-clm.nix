{ pkgs }:

pkgs.stdenv.mkDerivation {
  pname = "prime-agent-pi-clm";
  version = "1.0.0";
  src = pkgs.fetchurl {
    url = "https://registry.npmjs.org/@lolipopshock/pi-clm/-/pi-clm-1.0.0.tgz";
    sha256 = "RAi5bY2AL5np6sLNnSaSnnV2v88vAHkGQLT/LSvPJ80=";
  };
  unpackPhase = "tar xzf $src --strip-components=1";
  dontBuild = true;
  installPhase = ''
    mkdir -p $out
    cp -r . $out/
    chmod -R u+w $out
  '';
}
