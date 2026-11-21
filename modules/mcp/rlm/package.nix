{
  pkgs,
  lib,
}:

let
  pin = lib.importJSON ./rlm-kernel.json;

  kernelSrc = pkgs.fetchurl {
    url = "https://registry.npmjs.org/@florianguyonnet/pi-rlm/-/pi-rlm-${pin.version}.tgz";
    inherit (pin) hash;
  };

  kernel = pkgs.runCommand "pi-rlm-kernel-${pin.version}" { } ''
    tar xzf ${kernelSrc} package/kernel/rlm_kernel.py
    install -Dm644 package/kernel/rlm_kernel.py $out/rlm_kernel.py
  '';

  rlm-mcp = pkgs.python3Packages.buildPythonApplication {
    pname = "rlm-mcp";
    version = "0.1.0";
    pyproject = true;
    src = ./server;
    build-system = [ pkgs.python3Packages.setuptools ];
    dependencies = [ pkgs.python3Packages.mcp ];
    nativeCheckInputs = [ pkgs.python3Packages.pytestCheckHook ];
    preCheck = ''
      cp -r ${./tests} tests
      chmod -R u+w tests
      export RLM_KERNEL=${kernel}/rlm_kernel.py HOME=$TMPDIR
    '';
  };
in
{
  inherit rlm-mcp kernel;
}
