{
  lib,
  stdenv,
  stdenvNoCC,
  rustPlatform,
  # Shell-expandable default for PRIME_AGENT_CODING_AGENT_DIR, applied in the wrapper
  # so launches that never see the session variables still find the configured dir.
  # "" keeps upstream's ~/.prime/agent.
  defaultAgentDir ? "",
  # name -> full SKILL.md content, shipped into libexec/prime-agent/skills.
  extraBuiltinSkills ? { },
  # Ship the code-review skill (two-pass fan-out review) into libexec/prime-agent/skills.
  codeReview ? true,
  # Install a Terminal=true desktop entry launching the TUI in the default terminal.
  desktopEntry ? false,
  # Terminal title status glyphs, set as wrapper defaults; all should share one width.
  titleIdleGlyph ? "❄",
  titleWorkingGlyphs ? [
    "❅"
    "❅"
    "❆"
    "❆"
  ],
  fetchFromGitHub,
  bash,
  coreutils,
  fd,
  git,
  gnutar,
  installDesktopEntry,
  makeDesktopItem,
  makeWrapper,
  nodejs,
  python3,
  python311,
  ripgrep,
  uv,
  versionCheckHook,
  xdg-utils,
}:

let
  # Pin refreshed by ./update.sh; `rev` is tracked separately from `version` so an
  # upstream tag-prefix change does not need a package edit.
  source = builtins.fromJSON (builtins.readFile ./source.json);

  # Without it the sandboxed build ships the upstream fixture as the bundled catalog.
  catalog = fetchFromGitHub {
    owner = "PrimeIntellect-ai";
    repo = "prime-agent-catalog";
    rev = source.catalogRev;
    hash = source.catalogHash;
  };

  runtimePath = [
    nodejs
    bash
    coreutils
    fd
    git
    ripgrep
    gnutar
    uv
    python311
  ]
  ++ lib.optionals stdenv.hostPlatform.isLinux [ xdg-utils ];

  # ./patches/series is the single source of patch order; blanks and comments are skipped.
  patchSeries = lib.filter (line: line != "" && !(lib.hasPrefix "#" line)) (
    lib.splitString "\n" (builtins.readFile ./patches/series)
  );

  # Collision-check + write each extra skill's SKILL.md into the payload skills dir.
  # Only the cheap wrapper layer depends on these, so edits never recompile the Rust build.
  extraBuiltinSkillsCheck = lib.concatStringsSep "\n" (
    lib.mapAttrsToList (name: _: ''
      if [ -e "$packageDir/skills/${name}" ]; then
        echo "error: extraBuiltinSkills skill '${name}' already exists upstream" >&2
        exit 1
      fi
    '') extraBuiltinSkills
  );

  extraBuiltinSkillsInstall = lib.concatStringsSep "\n" (
    lib.mapAttrsToList (name: content: ''
      install -Dm644 ${builtins.toFile "SKILL.md" content} "$packageDir/skills/${name}/SKILL.md"
    '') extraBuiltinSkills
  );

  # One line carrying its own trailing backslash. A multi-line block here ends in a
  # newline, and an empty continued line terminates the makeWrapper command early.
  agentDirFlags = lib.optionalString (defaultAgentDir != "") (
    "--run 'export PRIME_AGENT_CODING_AGENT_DIR=\"\${PRIME_AGENT_CODING_AGENT_DIR:-${defaultAgentDir}}\"' "
    + "--run 'export PRIME_AGENT_KERNEL_VENV=\"\${PRIME_AGENT_KERNEL_VENV:-$PRIME_AGENT_CODING_AGENT_DIR/kernel-venv}\"' "
  );

  # Terminal=true: the desktop environment launches the user's default terminal,
  # which is how the interactive TUI is meant to run.
  desktopItem = makeDesktopItem {
    name = "prime-agent";
    desktopName = "Prime Agent";
    comment = "AI coding assistant with a Python REPL tool";
    # Same theme icon as the claude-code and agent-launch entries.
    icon = "utilities-terminal";
    exec = "/@out@/bin/prime-agent";
    terminal = true;
    type = "Application";
    categories = [
      "Development"
      "Utility"
    ];
  };

  desktopFile = "prime-agent.desktop";

  meta = {
    description = "Self-improving RLM coding and research agent";
    homepage = "https://github.com/PrimeIntellect-ai/prime-agent";
    changelog = "https://github.com/PrimeIntellect-ai/prime-agent/releases/tag/${source.rev}";
    license = lib.licenses.mit;
    maintainers = [ lib.maintainers.okwilkins ];
    mainProgram = "prime-agent";
    platforms = [
      "aarch64-linux"
      "x86_64-linux"
      "aarch64-darwin"
    ];
    sourceProvenance = with lib.sourceTypes; [ fromSource ];
  };

  # Assert the shared skill-name rule; names are spliced into bash paths below.
  skillName = import ./lib/skill-name.nix { inherit lib; };
  invalidSkillNames = skillName.invalidSkillNames (builtins.attrNames extraBuiltinSkills);
in
assert lib.assertMsg (invalidSkillNames == [ ]) (
  "extraBuiltinSkills names must match the skill-name rule, got: "
  + lib.concatStringsSep ", " invalidSkillNames
);
let
  # Rust build plus the upstream payload. It takes no module settings, so skill or
  # wrapper edits reuse the cached build.
  unwrapped = rustPlatform.buildRustPackage (finalAttrs: {
    pname = "prime-agent-unwrapped";
    inherit (source) version;
    __structuredAttrs = true;

    src = fetchFromGitHub {
      owner = "PrimeIntellect-ai";
      repo = "prime-agent";
      tag = source.rev;
      inherit (source) hash;
    };

    inherit (source) cargoHash;

    patches = map (patch: ./patches + "/${patch}") patchSeries;

    # The vendored tree includes every workspace member; ship only the CLI binary.
    cargoBuildFlags = [
      "-p"
      "pa-cli"
      "--bin"
      "prime-agent"
    ];

    # The full suite needs PTYs, tmux and gh; run it in the scratch tree instead.
    doCheck = false;

    nativeBuildInputs = [ python3 ];

    postInstall = ''
      packageDir=$out/libexec/prime-agent
      mkdir -p "$packageDir"
      mv $out/bin/prime-agent "$packageDir/prime-agent"
      rmdir $out/bin

      cp -R prime-agent-runtime skills "$packageDir/"
      rm -rf "$packageDir/prime-agent-runtime/test"
      cp LICENSE README.md "$packageDir/"
      cp ${catalog}/models/catalog.v1.json "$packageDir/models.bundled.json"
      cp ${catalog}/plugins/catalog.v2.json "$packageDir/mcp-services.bundled.json"
      python3 scripts/release/bundle_catalog.py verify --out "$packageDir"
    '';

    doInstallCheck = true;

    installCheckPhase = ''
      runHook preInstallCheck

      requiredPythonVersion=$(grep -oP 'const PYTHON_VERSION: &str = "\K[^"]+' \
        crates/pa-core/src/kernel/bootstrap/venv.rs)
      actualVersion=$(${lib.getExe python311} -c 'import sys;print("%d.%d"%sys.version_info[:2])')
      if [ "$actualVersion" != "$requiredPythonVersion" ]; then
        echo "ERROR: prime-agent requires Python $requiredPythonVersion but $actualVersion provided"
        exit 1
      fi
      for payload in prime-agent-runtime/pyproject.toml skills models.bundled.json mcp-services.bundled.json; do
        [ -e "$packageDir/$payload" ] || { echo "ERROR: missing payload $payload" >&2; exit 1; }
      done

      runHook postInstallCheck
    '';

    inherit meta;
  });
in
stdenvNoCC.mkDerivation {
  pname = "prime-agent";
  inherit (unwrapped) version;
  __structuredAttrs = true;

  dontUnpack = true;

  nativeBuildInputs = [ makeWrapper ];

  # The payload is small text, so copy it; PI_PACKAGE_DIR makes the linked binary use it.
  installPhase = ''
    runHook preInstall

    packageDir=$out/libexec/prime-agent
    mkdir -p "$packageDir"
    cp -R ${unwrapped}/libexec/prime-agent/. "$packageDir/"
    chmod -R u+w "$packageDir"
    ln -sf ${unwrapped}/libexec/prime-agent/prime-agent "$packageDir/prime-agent"

    ${lib.optionalString codeReview ''
      if [ -e "$packageDir/skills/code-review" ]; then
        echo "error: code-review skill already exists upstream" >&2
        exit 1
      fi
      cp -R ${./skills}/code-review "$packageDir/skills/"
    ''}
    ${extraBuiltinSkillsCheck}
    ${extraBuiltinSkillsInstall}

    # The venv uses nix's Python; UV_PYTHON_DOWNLOADS=never stops surprise downloads
    # now that the build patch drops `uv python install`.
    makeWrapper "$packageDir/prime-agent" $out/bin/prime-agent \
      --set PI_PACKAGE_DIR "$packageDir" \
      --set PI_SKIP_VERSION_CHECK 1 \
      --set UV_PYTHON_PREFERENCE only-system \
      --set UV_PYTHON_DOWNLOADS never \
      --set-default PRIME_AGENT_TITLE_IDLE_GLYPH ${lib.escapeShellArg titleIdleGlyph} \
      --set-default PRIME_AGENT_TITLE_WORKING_GLYPHS ${lib.escapeShellArg (lib.concatStringsSep " " titleWorkingGlyphs)} \
      ${agentDirFlags}\
      --prefix PATH : ${lib.makeBinPath runtimePath}

    ${lib.optionalString desktopEntry ''
      ${installDesktopEntry { inherit desktopItem desktopFile; }}
    ''}

    runHook postInstall
  '';

  doInstallCheck = true;
  nativeInstallCheckInputs = [ versionCheckHook ];
  versionCheckProgramArg = "--version";

  passthru = { inherit unwrapped; };

  inherit meta;
}
