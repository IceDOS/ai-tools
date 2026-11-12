{
  lib,
  makeWrapper,
  nodejs_24,
  python3,
  stdenvNoCC,
}:

stdenvNoCC.mkDerivation {
  pname = "agent-relay";
  version = "0.1.0";
  src = ./src;
  nativeBuildInputs = [ makeWrapper ];
  nativeCheckInputs = [
    nodejs_24
    python3
  ];
  dontBuild = true;
  doCheck = true;

  checkPhase = ''
    runHook preCheck
    node --test tests/*.test.ts
    python3 tests/test_agy_pty.py
    runHook postCheck
  '';

  installPhase = ''
    runHook preInstall
    mkdir -p $out/lib/agent-relay $out/bin
    cp *.ts *.py $out/lib/agent-relay/
    makeWrapper ${lib.getExe nodejs_24} $out/bin/agent-relay-mcp --add-flags $out/lib/agent-relay/mcp-server.ts
    makeWrapper ${lib.getExe nodejs_24} $out/bin/agent-relay-hook --add-flags $out/lib/agent-relay/hook.ts
    makeWrapper ${lib.getExe nodejs_24} $out/bin/agent-relay-agy-watch --add-flags $out/lib/agent-relay/agy-watch.ts
    makeWrapper ${lib.getExe python3} $out/bin/agent-relay-agy \
      --add-flags $out/lib/agent-relay/agy_pty.py \
      --set AGENT_RELAY_AGY_WATCH $out/bin/agent-relay-agy-watch
    runHook postInstall
  '';

  meta = {
    description = "Message relay between pi, Claude Code, opencode and Antigravity CLI sessions";
    platforms = lib.platforms.linux;
  };
}
