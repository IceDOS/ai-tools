{ pkgs }:

let
  inherit (pkgs) lib;

  nodejs = pkgs.nodejs_24;

  # Pins refreshed by ./update.sh, which also regenerates package-lock.json.
  source = lib.importJSON ./source.json;

  unwrapped = pkgs.buildNpmPackage {
    pname = "pagespeed-insights-mcp";
    inherit (source) version;
    inherit nodejs;

    # The published tarball ships the compiled dist, so nothing is built here.
    src = pkgs.fetchzip {
      url = "https://registry.npmjs.org/pagespeed-insights-mcp/-/pagespeed-insights-mcp-${source.version}.tgz";
      inherit (source) hash;
    };

    # package-lock.json was generated from this trimmed package.json.
    postPatch = ''
      ${lib.getExe pkgs.jq} 'del(.devDependencies, .scripts.prepublishOnly)' \
        package.json > package.json.new
      mv package.json.new package.json
      cp ${./package-lock.json} package-lock.json
    '';

    inherit (source) npmDepsHash;
    dontNpmBuild = true;

    nativeBuildInputs = [ pkgs.makeWrapper ];

    postInstall = ''
      rm $out/bin/*
      makeWrapper ${lib.getExe nodejs} $out/bin/pagespeed-insights-mcp \
        --add-flags $out/lib/node_modules/pagespeed-insights-mcp/dist/index.js
    '';

    meta.mainProgram = "pagespeed-insights-mcp";
  };
in
# GOOGLE_API_KEY_FILE names a file outside the store holding the key, read when GOOGLE_API_KEY is unset.
pkgs.writeShellApplication {
  name = "pagespeed-insights-mcp";
  text = ''
    if [ -z "''${GOOGLE_API_KEY:-}" ] && [ -n "''${GOOGLE_API_KEY_FILE:-}" ]; then
      file="''${GOOGLE_API_KEY_FILE/#\~/$HOME}"
      if [ ! -r "$file" ]; then
        echo "pagespeed-insights-mcp: cannot read GOOGLE_API_KEY_FILE $file" >&2
        exit 1
      fi
      GOOGLE_API_KEY="$(< "$file")"
      export GOOGLE_API_KEY
    fi
    exec ${lib.getExe unwrapped} "$@"
  '';
  derivationArgs.passthru = { inherit unwrapped; };
}
