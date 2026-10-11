# Shared with the agent modules that wrap their binaries through ai-account.
{
  callPackage,
  lib,
  python3Packages,
  xdg-dbus-proxy,
}:

python3Packages.buildPythonApplication {
  pname = "ai-accounts";
  version = "0.1.0";
  pyproject = true;
  src = ./src;
  build-system = [ python3Packages.setuptools ];
  nativeCheckInputs = [ python3Packages.pytestCheckHook ];
  makeWrapperArgs = [
    "--prefix"
    "PATH"
    ":"
    "${lib.makeBinPath [ xdg-dbus-proxy ]}"
  ];
  meta = {
    description = "Run coding agents under several accounts";
    mainProgram = "ai-account";
  };
}
