{
  lib,
  makeWrapper,
  nodejs_24,
  stdenvNoCC,
}:

stdenvNoCC.mkDerivation {
  pname = "agent-relay";
  version = "0.1.0";
  src = ./src;
  nativeBuildInputs = [ makeWrapper ];
  nativeCheckInputs = [ nodejs_24 ];
  dontBuild = true;
  doCheck = true;

  checkPhase = ''
    runHook preCheck
    node --test tests/*.test.ts
    runHook postCheck
  '';

  installPhase = ''
    runHook preInstall
    mkdir -p $out/lib/agent-relay $out/bin
    cp *.ts $out/lib/agent-relay/
    makeWrapper ${lib.getExe nodejs_24} $out/bin/agent-relay-mcp --add-flags $out/lib/agent-relay/mcp-server.ts
    makeWrapper ${lib.getExe nodejs_24} $out/bin/agent-relay-hook --add-flags $out/lib/agent-relay/hook.ts
    runHook postInstall
  '';

  meta = {
    description = "Message relay between pi, Claude Code, opencode and Antigravity CLI sessions";
    platforms = lib.platforms.linux;
  };
}
