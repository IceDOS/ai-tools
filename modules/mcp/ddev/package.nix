{ pkgs }:

let
  inherit (pkgs) lib;

  nodejs = pkgs.nodejs_24;

  # Pins refreshed by ./update.sh, which also regenerates package-lock.json.
  source = lib.importJSON ./source.json;
in
pkgs.buildNpmPackage {
  pname = "ddev-mcp";
  inherit (source) version;
  inherit nodejs;

  # The published tarball ships the compiled dist, so nothing is built here.
  src = pkgs.fetchzip {
    url = "https://registry.npmjs.org/ddev-mcp/-/ddev-mcp-${source.version}.tgz";
    inherit (source) hash;
  };

  # package-lock.json was generated from this trimmed package.json. ts-node,
  # typescript and @types/node are build tools listed as runtime dependencies.
  postPatch = ''
    ${lib.getExe pkgs.jq} 'del(.devDependencies, .dependencies["ts-node", "typescript", "@types/node"], .scripts.prepublishOnly)' \
      package.json > package.json.new
    mv package.json.new package.json
    cp ${./package-lock.json} package-lock.json
  '';

  inherit (source) npmDepsHash;
  dontNpmBuild = true;

  nativeBuildInputs = [ pkgs.makeWrapper ];

  # Suffixed, so a ddev already on PATH wins; node-notifier calls notify-send.
  postInstall = ''
    rm $out/bin/*
    makeWrapper ${lib.getExe nodejs} $out/bin/ddev-mcp \
      --add-flags $out/lib/node_modules/ddev-mcp/dist/index.js \
      --suffix PATH : ${
        lib.makeBinPath [
          pkgs.ddev
          pkgs.libnotify
        ]
      }
  '';

  meta.mainProgram = "ddev-mcp";
}
