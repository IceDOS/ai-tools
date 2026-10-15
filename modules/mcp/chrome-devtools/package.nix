{ pkgs }:

let
  inherit (pkgs) lib;
  nodejs = pkgs.nodejs_24;

  # Pin refreshed by ./update.sh.
  source = lib.importJSON ./source.json;
in
# The npm tarball is a rollup bundle with no runtime dependencies.
pkgs.stdenvNoCC.mkDerivation (finalAttrs: {
  pname = "chrome-devtools-mcp";
  inherit (source) version;

  src = pkgs.fetchzip {
    url = "https://registry.npmjs.org/chrome-devtools-mcp/-/chrome-devtools-mcp-${finalAttrs.version}.tgz";
    inherit (source) hash;
  };

  nativeBuildInputs = [ pkgs.makeWrapper ];

  installPhase = ''
    runHook preInstall

    mkdir -p $out/lib/chrome-devtools-mcp
    cp -r build package.json LICENSE $out/lib/chrome-devtools-mcp/

    for bin in chrome-devtools-mcp chrome-devtools; do
      makeWrapper ${lib.getExe nodejs} $out/bin/$bin \
        --add-flags $out/lib/chrome-devtools-mcp/build/src/bin/$bin.js \
        --set-default CHROME_DEVTOOLS_MCP_NO_UPDATE_CHECKS 1
    done

    runHook postInstall
  '';

  meta.mainProgram = "chrome-devtools-mcp";
})
