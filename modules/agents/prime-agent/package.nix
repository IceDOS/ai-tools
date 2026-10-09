{
  lib,
  stdenv,
  buildNpmPackage,
  # Shell-expandable default for PRIME_AGENT_CODING_AGENT_DIR, applied in the wrapper
  # so launches that never see the session variables still find the configured dir.
  # "" keeps upstream's ~/.prime/agent.
  defaultAgentDir ? "",
  # name -> full SKILL.md content, shipped into dist/skills by the install phase.
  extraBuiltinSkills ? { },
  # Ship the code-review skill (two-pass fan-out review) into dist/skills.
  codeReview ? true,
  # Install a Terminal=true desktop entry launching the TUI in the default terminal.
  desktopEntry ? true,
  # Ship the bundled Ollama Cloud provider (24 models + /login API-key flow with
  # OLLAMA_API_KEY). Disable to keep the built-in catalog Ollama-free.
  ollamaCloud ? true,
  # Terminal title status glyphs, set as wrapper defaults; all should share one width.
  titleIdleGlyph ? "❄",
  titleWorkingGlyphs ? [
    "❅"
    "❅"
    "❆"
    "❆"
  ],
  fetchFromGitHub,
  autoPatchelfHook,
  bash,
  cacert,
  fd,
  git,
  gnutar,
  installDesktopEntry,
  makeDesktopItem,
  makeWrapper,
  nodejs,
  npm-lockfile-fix,
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

  # Without it the sandboxed build ships the 4-model test fixture as the bundled catalog.
  catalog = fetchFromGitHub {
    owner = "PrimeIntellect-ai";
    repo = "prime-agent-catalog";
    rev = source.catalogRev;
    hash = source.catalogHash;
  };

  runtimePath = [
    nodejs
    bash
    fd
    git
    ripgrep
    gnutar
    uv
    python311
  ]
  ++ lib.optionals stdenv.hostPlatform.isLinux [ xdg-utils ];

  # Collision-check + write each extra skill's SKILL.md into dist/skills/<name>.
  # toFile paths are build inputs, so content edits rebuild the package.
  extraBuiltinSkillsCheck = lib.concatStringsSep "\n" (
    lib.mapAttrsToList (name: _: ''
      if [ -e "$packageDir/dist/skills/${name}" ]; then
        echo "error: extraBuiltinSkills skill '${name}' already exists upstream" >&2
        exit 1
      fi
    '') extraBuiltinSkills
  );

  extraBuiltinSkillsInstall = lib.concatStringsSep "\n" (
    lib.mapAttrsToList (name: content: ''
      mkdir -p "$packageDir/dist/skills/${name}"
      cp ${builtins.toFile "SKILL.md" content} "$packageDir/dist/skills/${name}/SKILL.md"
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
    icon = "/@out@/share/icons/hicolor/scalable/apps/${icon}";
    exec = "/@out@/bin/prime-agent";
    terminal = true;
    type = "Application";
    categories = [
      "Development"
      "Utility"
    ];
  };

  icon = "prime-agent.svg";
  desktopFile = "prime-agent.desktop";

  # Assert the shared skill-name rule; names are spliced into bash paths below.
  skillName = import ./lib/skill-name.nix { inherit lib; };
  invalidSkillNames = skillName.invalidSkillNames (builtins.attrNames extraBuiltinSkills);
in
assert lib.assertMsg (invalidSkillNames == [ ]) (
  "extraBuiltinSkills names must match the skill-name rule, got: "
  + lib.concatStringsSep ", " invalidSkillNames
);
buildNpmPackage (finalAttrs: {
  pname = "prime-agent";
  inherit (source) version;
  __structuredAttrs = true;

  src = fetchFromGitHub {
    owner = "PrimeIntellect-ai";
    repo = "prime-agent";
    tag = "v${finalAttrs.version}";
    inherit (source) hash;

    # The upstream lockfile omits registry metadata for workspace dependencies.
    postFetch = ''
      ${lib.optionalString stdenv.hostPlatform.isDarwin "export REQUESTS_CA_BUNDLE=${cacert}/etc/ssl/certs/ca-bundle.crt"}
      ${lib.getExe npm-lockfile-fix} $out/package-lock.json
    '';
  };

  patches = [
    # modelOverrides is only applied to built-in models; custom models from
    # models[] overwrite them, ignoring overrides like contextWindow.
    ./patches/model-registry-model-overrides.patch

    # bootstrap.ts runs `uv python install 3.11` before creating the venv, downloading
    # a Python that UV_PYTHON_PREFERENCE=system never uses. Drop the redundant fetch.
    ./patches/remove-uv-python-install.patch

    # daemon writes its ownership registry to hardcoded ~/.prime; its env override is
    # stripped on launch. Follow PRIME_AGENT_CODING_AGENT_DIR instead so nothing lives there.
    ./patches/daemon-supervisor-registry-follow-agentdir.patch

    # Title is set once at startup. Keep it as "<status glyph> <name|recap|first message>"
    # so Zed terminal threads show live status and topic. A failure
    # recap outlives the failure, so the title drops it once the agent moves on.
    ./patches/terminal-title-session-headline.patch

    # /speed is a runtime-only session toggle; seed its default from terminal.speed
    # in settings.json so a session starts with the footer readout on.
    ./patches/speed-session-default.patch

    # opencode zen 403s a free-tier request whose tools omit bash, grep, glob and
    # read. Append stubs for the missing names so every zen request is accepted.
    ./patches/opencode-zen-tool-floor.patch

    # zen wraps flaky upstream rejections as 400 "Upstream request failed", which
    # prime treats as permanent and ends the turn. Retry it like a server error.
    ./patches/opencode-zen-upstream-retry.patch

    # big-pickle ignores tool_choice "none" on stub-only requests and leaks DSML
    # tool-call text, breaking JSON side calls. Tell it no tools, retry on leaked markup.
    ./patches/opencode-zen-toolless-guard.patch

    # Refine and auto-refine review fail outright on one malformed reply. Resend once.
    ./patches/refinement-json-retry.patch

    # A child whose turn ends on a provider error (rate limit, quota) counts as done
    # and the parent gets a bare "no-reply" notice. Report it as child-failed with the
    # error, and show provider retries as progress notes in collect/list snapshots.
    ./patches/rlm-child-provider-error-notice.patch

    # Extensions run in the daemon, which forwards only string widgets, so none can reach
    # the editor. Route the reserved "prompt-suggest" widget into it as ghost text; Tab inserts it.
    ./patches/prompt-suggestion-ghost-text.patch
  ]
  ++ lib.optionals ollamaCloud [
    # Ollama Cloud provider: bundled models (https://ollama.com/v1) + the /login
    # API-key flow (OLLAMA_API_KEY), upstream-style openai-completions integration.
    ./patches/ollama-cloud-provider.patch

    # The upstream catalog has no Ollama entries, and only catalog-listed models reach
    # the registry. Keep installed providers the catalog does not list.
    ./patches/ollama-cloud-catalog-keep.patch
  ];

  npmDepsFetcherVersion = 2;
  inherit (source) npmDepsHash;

  # Install scripts would download or build native addons (koffi, esbuild, ...);
  # runtime addons ship prebuilts or are Linux-unused (koffi), so skip them.
  npmRebuildFlags = [ "--ignore-scripts" ];

  nativeBuildInputs = [
    makeWrapper
  ]
  ++ lib.optionals stdenv.hostPlatform.isLinux [ autoPatchelfHook ];

  # @mariozechner/clipboard's napi addon links libgcc_s; autoPatchelfHook must
  # find it (also in the LD_LIBRARY_PATH of the wrapper below).
  buildInputs = [ stdenv.cc.cc.lib ];

  dontConfigure = true;
  # Build the workspace packages explicitly because the root package is not the CLI.
  dontNpmBuild = true;
  buildPhase = ''
    runHook preBuild

    export PATH="$PWD/node_modules/.bin:$PATH"
    mkdir -p packages/coding-agent/catalog
    cp ${catalog}/models/catalog.v1.json packages/coding-agent/catalog/models.bundled.json
    cp ${catalog}/plugins/catalog.v2.json packages/coding-agent/catalog/mcp-services.bundled.json
    npm --workspace packages/tui run build
    (cd packages/ai && tsgo -p tsconfig.build.json)
    npm --workspace packages/agent run build
    npm --workspace packages/coding-agent run build

    runHook postBuild
  '';

  installPhase = ''
    runHook preInstall

    npm prune --omit=dev --ignore-scripts
    packageDir=$out/lib/prime-agent
    mkdir -p "$packageDir" $out/bin
    cp -R node_modules "$packageDir/node_modules"
    rm -rf "$packageDir/node_modules/koffi"
    cp packages/coding-agent/package.json "$packageDir/package.json"
    cp -R packages/coding-agent/dist "$packageDir/dist"
    for path in README.md CHANGELOG.md docs examples skills; do
      cp -R "packages/coding-agent/$path" "$packageDir/$path"
    done

    ${lib.optionalString codeReview ''
      if [ -e "$packageDir/dist/skills/code-review" ]; then
        echo "error: code-review skill already exists upstream" >&2
        exit 1
      fi
      cp -R ${./skills}/code-review "$packageDir/dist/skills/"
    ''}

    # Ship module-configured extra built-in skills (markdown-only) into dist/skills.
    ${extraBuiltinSkillsCheck}
    ${extraBuiltinSkillsInstall}

    mkdir -p "$packageDir/packages"
    for workspace in ai agent tui coding-agent; do
      mkdir -p "$packageDir/packages/$workspace"
      cp "packages/$workspace/package.json" "$packageDir/packages/$workspace/package.json"
      cp -R "packages/$workspace/dist" "$packageDir/packages/$workspace/dist"
    done
    for path in docs examples skills; do
      cp -R "packages/coding-agent/$path" "$packageDir/packages/coding-agent/$path"
    done

    # The venv uses nix's Python; UV_PYTHON_DOWNLOADS=manual stops surprise downloads.
    makeWrapper ${lib.getExe nodejs} $out/bin/prime-agent \
      --add-flags "$packageDir/dist/bundle/cli.js" \
      --set PI_PACKAGE_DIR "$packageDir" \
      --set PI_SKIP_VERSION_CHECK 1 \
      --set UV_PYTHON_PREFERENCE system \
      --set UV_PYTHON_DOWNLOADS manual \
      --set-default PRIME_AGENT_TITLE_IDLE_GLYPH ${lib.escapeShellArg titleIdleGlyph} \
      --set-default PRIME_AGENT_TITLE_WORKING_GLYPHS ${lib.escapeShellArg (lib.concatStringsSep " " titleWorkingGlyphs)} \
      ${agentDirFlags}\
      --prefix PATH : ${lib.makeBinPath runtimePath} \
      ${lib.optionalString stdenv.hostPlatform.isLinux "--prefix LD_LIBRARY_PATH : ${
        lib.makeLibraryPath [ stdenv.cc.cc.lib ]
      }"}

    ${lib.optionalString desktopEntry ''
      ${installDesktopEntry { inherit desktopItem desktopFile; }}
      install -Dm644 assets/brand/prime-butterfly.svg \
        "$out/share/icons/hicolor/scalable/apps/${icon}"
    ''}

    runHook postInstall
  '';

  doInstallCheck = true;
  nativeInstallCheckInputs = [ versionCheckHook ];
  versionCheckProgramArg = "--version";

  installCheckPhase = ''
    runHook preInstallCheck

    requiredPythonVersion=$(grep -oP 'PYTHON_VERSION = "\K[^"]+' \
      packages/coding-agent/src/core/kernel/bootstrap.ts)
    actualVersion=$(${lib.getExe python311} -c 'import sys;print("%d.%d"%sys.version_info[:2])')
    if [ "$actualVersion" != "$requiredPythonVersion" ]; then
      echo "ERROR: prime-agent requires Python $requiredPythonVersion but $actualVersion provided"
      exit 1
    fi

    runHook postInstallCheck
  '';

  meta = {
    description = "Self-improving RLM coding and research agent";
    homepage = "https://github.com/PrimeIntellect-ai/prime-agent";
    changelog = "https://github.com/PrimeIntellect-ai/prime-agent/releases/tag/v${finalAttrs.version}";
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
})
