{ pkgs }:

let
  inherit (pkgs) lib;

  nodejs = pkgs.nodejs_24;

  # Pins refreshed by ./update.sh, which also regenerates package-lock.json.
  source = lib.importJSON ./source.json;

  runtimeInputs = with pkgs; [
    binutils
    ghidra
    jdk21
    procps
    rizin
  ];
in
pkgs.buildNpmPackage {
  pname = "rea-agents";
  inherit (source) version;
  inherit nodejs;

  # The published tarball ships the compiled dist, so nothing is built here.
  src = pkgs.fetchzip {
    url = "https://registry.npmjs.org/rea-agents/-/rea-agents-${source.version}.tgz";
    inherit (source) hash;
  };

  # package-lock.json was generated from this trimmed package.json. frida is dropped:
  # its install script downloads a prebuilt binding, which the sandbox forbids.
  postPatch = ''
    ${lib.getExe pkgs.jq} 'del(.devDependencies, .optionalDependencies.frida, .scripts.prepare, .scripts.prepack)' \
      package.json > package.json.new
    mv package.json.new package.json
    cp ${./package-lock.json} package-lock.json
  '';

  inherit (source) npmDepsHash;
  dontNpmBuild = true;

  nativeBuildInputs = [
    pkgs.autoPatchelfHook
    pkgs.makeWrapper
  ];
  buildInputs = [ pkgs.stdenv.cc.cc.lib ];
  # Prebuilt node-pty binaries for other platforms ship in the tree too.
  autoPatchelfIgnoreMissingDeps = true;

  postInstall = ''
    rm $out/bin/*
    makeWrapper ${lib.getExe nodejs} $out/bin/rea \
      --add-flags $out/lib/node_modules/rea-agents/scripts/rea.mjs \
      --prefix PATH : ${lib.makeBinPath runtimeInputs} \
      --set-default GHIDRA_INSTALL_DIR ${pkgs.ghidra}/lib/ghidra
  '';

  meta.mainProgram = "rea";
}
