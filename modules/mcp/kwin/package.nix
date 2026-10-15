{ pkgs }:

let
  inherit (pkgs) lib;

  python = import ../lib/python.nix { inherit pkgs; };

  # Pin refreshed by ./update.sh. Upstream main runs ahead of its tags, so it tracks main.
  source = lib.importJSON ./source.json;

  src = pkgs.fetchFromGitHub {
    owner = "isac322";
    repo = "kwin-mcp";
    inherit (source) rev hash;
  };

  kwin-mcp = python.pkgs.buildPythonPackage {
    pname = "kwin-mcp";
    inherit (source) version;
    pyproject = true;

    inherit src;

    # nixpkgs uv-build is older than the pinned range; libei and libwayland-client load by store path.
    postPatch = ''
      substituteInPlace pyproject.toml --replace-fail '"uv_build>=0.12.19,<0.14.0"' '"uv_build"'
      substituteInPlace src/kwin_mcp/input.py \
        --replace-fail 'ctypes.CDLL("libei.so.1")' 'ctypes.CDLL("${lib.getLib pkgs.libei}/lib/libei.so.1")'
      substituteInPlace src/kwin_mcp/clipboard.py \
        --replace-fail 'candidates += ["libwayland-client.so.0", "libwayland-client.so"]' \
          'candidates += ["${lib.getLib pkgs.wayland}/lib/libwayland-client.so.0"]'
    '';

    build-system = [ python.pkgs.uv-build ];

    dependencies = with python.pkgs; [
      dbus-python
      mcp
      pillow
      pygobject3
    ];

    pythonImportsCheck = [ "kwin_mcp" ];
    doCheck = false;
  };

  # The server re-runs sys.executable -m kwin_mcp.*, so it needs an interpreter that sees its packages.
  env = python.withPackages (_: [ kwin-mcp ]);

  runtimeInputs = with pkgs; [
    bash
    dbus
    kdePackages.kwin
    kdePackages.spectacle
    wayland-utils
    wl-clipboard
    wtype
  ];

  typelibs = lib.makeSearchPath "lib/girepository-1.0" [
    pkgs.at-spi2-core
    pkgs.glib
  ];

  mkBin =
    name: entry:
    import ../lib/python-bin.nix {
      inherit
        pkgs
        env
        name
        entry
        runtimeInputs
        ;
      preExec = ''
        export GI_TYPELIB_PATH="${typelibs}''${GI_TYPELIB_PATH:+:$GI_TYPELIB_PATH}"
      '';
    };
in
pkgs.symlinkJoin {
  name = "kwin-mcp-${kwin-mcp.version}";
  paths = [
    (mkBin "kwin-mcp" "kwin_mcp.server:main")
    (mkBin "kwin-mcp-cli" "kwin_mcp.cli:main")
  ];
  passthru = {
    inherit src;
    unwrapped = kwin-mcp;
  };
  meta.mainProgram = "kwin-mcp";
}
