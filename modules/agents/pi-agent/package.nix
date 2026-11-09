{
  lib,
  stdenvNoCC,
  makeWrapper,
  makeDesktopItem,
  writeShellScript,
  pi-coding-agent,
  # icedosLib.packaging.installDesktopEntry; only called when desktopEntry is on.
  installDesktopEntry ? null,
  # Shell-expandable PI_CODING_AGENT_DIR default; a value already in the environment wins.
  defaultAgentDir ? "\${XDG_CONFIG_HOME:-$HOME/.config}/pi-agent",
  desktopEntry ? false,
  # Extra makeWrapper flags, e.g. --set-default pairs for extension env vars.
  extraWrapperArgs ? [ ],
}:

let
  noSelfUpdate = writeShellScript "pi-agent-no-self-update" ''
    echo "pi is managed by IceDOS; update it with icedos rebuild." >&2
    exit 1
  '';

  desktopItem = makeDesktopItem {
    name = "pi-agent";
    desktopName = "Pi";
    comment = "Pi coding agent";
    icon = "utilities-terminal";
    exec = "/@out@/bin/pi";
    terminal = true;
    type = "Application";
    categories = [
      "Development"
      "Utility"
    ];
  };
in
assert lib.assertMsg (
  !desktopEntry || installDesktopEntry != null
) "pi-agent: desktopEntry needs installDesktopEntry";
stdenvNoCC.mkDerivation {
  pname = "pi-agent";
  inherit (pi-coding-agent) version;

  dontUnpack = true;
  nativeBuildInputs = [ makeWrapper ];

  installPhase = ''
    runHook preInstall

    makeWrapper ${lib.getExe pi-coding-agent} $out/bin/pi \
      --run 'export PI_CODING_AGENT_DIR="''${PI_CODING_AGENT_DIR:-${defaultAgentDir}}"' \
      --set PI_SKIP_VERSION_CHECK 1 \
      --set-default PI_TELEMETRY 0 \
      --set PI_SELF_UPDATE_COMMAND ${noSelfUpdate} \
      ${lib.escapeShellArgs extraWrapperArgs}

    ${lib.optionalString desktopEntry (installDesktopEntry {
      inherit desktopItem;
      desktopFile = "pi-agent.desktop";
    })}

    runHook postInstall
  '';

  passthru.unwrapped = pi-coding-agent;

  meta = pi-coding-agent.meta // {
    mainProgram = "pi";
  };
}
