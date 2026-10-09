# Shared with the agent modules whose status line renders through alimit.
{
  git,
  lib,
  libnotify,
  python3Packages,
}:

python3Packages.buildPythonApplication {
  pname = "alimit";
  version = "0.2.0";
  pyproject = true;
  src = ./src;
  build-system = [ python3Packages.setuptools ];
  nativeCheckInputs = [ python3Packages.pytestCheckHook ];
  # notify-send for the timer's alerts; git as a fallback for the status line.
  makeWrapperArgs = [
    "--prefix"
    "PATH"
    ":"
    "${lib.makeBinPath [ libnotify ]}"
    "--suffix"
    "PATH"
    ":"
    "${lib.makeBinPath [ git ]}"
  ];
  meta = {
    description = "Track AI usage limits and burn rate across agents and providers";
    mainProgram = "alimit";
  };
}
