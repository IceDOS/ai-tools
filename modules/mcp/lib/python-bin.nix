# Runs a Python entry point ("module:function") with the interpreter of `env`, so a
# server that re-runs sys.executable finds its own packages in the child too.
{
  pkgs,
  env,
  name,
  entry,
  runtimeInputs ? [ ],
  preExec ? "",
}:

let
  parts = pkgs.lib.splitString ":" entry;
  module = builtins.head parts;
  function = pkgs.lib.last parts;
in
pkgs.writeShellApplication {
  inherit name runtimeInputs;
  text = ''
    ${preExec}
    exec ${env}/bin/python3 -c "import sys; sys.argv[0] = '${name}'; from ${module} import ${function}; sys.exit(${function}())" "$@"
  '';
}
