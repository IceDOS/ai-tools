{
  pkgs,
  lib,
  cfg,
  agy,
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

  bin = "${rlm-mcp}/bin/rlm-mcp";

  mcpConfig = {
    mcpServers.kernel = {
      command = bin;
      args = [ "serve" ];
      timeoutSeconds = -1;
      env = {
        RLM_AGY = agy;
        RLM_KERNEL = "${kernel}/rlm_kernel.py";
        RLM_PYTHON = "${pkgs.python3}/bin/python3";
        RLM_SELF = bin;
        RLM_MAX_DEPTH = toString cfg.maxDepth;
        RLM_CHILD_TIMEOUT = cfg.childTimeout;
        RLM_CHILD_LOG = if cfg.childLog then "1" else "0";
        RLM_SNAPSHOTS = if cfg.snapshots then "1" else "0";
      };
    };
  };

  hookEnv = lib.concatStringsSep " " [
    "RLM_AGY=${agy}"
    "RLM_SELF=${bin}"
    "RLM_CHILD_TIMEOUT=${cfg.childTimeout}"
  ];

  hook = event: [
    {
      type = "command";
      command = "${pkgs.coreutils}/bin/env ${hookEnv} ${bin} hook ${event}";
      timeout = 25;
    }
  ];

  hooksConfig.rlm = {
    SessionStart = hook "SessionStart";
    PreInvocation = hook "PreInvocation";
    Stop = hook "Stop";
  };

  pluginManifest = {
    name = "rlm";
    displayName = "Recursive Language Models";
    description = "Persistent Python REPL tool whose rlm() runs recursive child agy agents.";
    version = "0.1.0";
  };

  plugin = pkgs.runCommand "antigravity-rlm-plugin" { } ''
    mkdir -p $out
    cp -r ${./plugin}/. $out/
    chmod -R u+w $out
    cp ${pkgs.writeText "plugin.json" (builtins.toJSON pluginManifest)} $out/plugin.json
    cp ${pkgs.writeText "mcp_config.json" (builtins.toJSON mcpConfig)} $out/mcp_config.json
    cp ${pkgs.writeText "hooks.json" (builtins.toJSON hooksConfig)} $out/hooks.json
  '';
in
{
  inherit rlm-mcp kernel plugin;
}
