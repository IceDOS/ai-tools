{ icedosLib, lib, ... }:

{
  options.icedos.ai-tools.prime-agent =
    let
      inherit (lib) importTOML types;

      inherit (icedosLib)
        mkAttrsOfOption
        mkBoolOption
        mkIntBetweenOption
        mkNumberOption
        mkStrListOption
        mkStrOption
        mkSubmoduleAttrsOption
        ;

      inherit ((importTOML ./config.toml).icedos.ai-tools.prime-agent)
        desktopEntry
        includeInIcedosGc
        providers
        sessionRetentionDays
        terminal-title
        ;

      defaults = (importTOML ./config.toml).icedos.ai-tools.prime-agent.default;
      settings = (importTOML ./config.toml).icedos.ai-tools.prime-agent.settings;
      skills = (importTOML ./config.toml).icedos.ai-tools.prime-agent.skills;
      extensions = (importTOML ./config.toml).icedos.ai-tools.prime-agent.extensions;
    in
    {
      default = {
        provider = mkStrOption { default = defaults.provider; };
        model = mkStrOption { default = defaults.model; };
      };

      # Install a desktop entry launching the TUI in the user's default
      # terminal (Terminal=true; the desktop environment picks the terminal).
      desktopEntry = mkBoolOption { default = desktopEntry; };

      # Ship the bundled Ollama Cloud provider (24 models + /login API-key flow
      # with OLLAMA_API_KEY). Disable to keep the built-in catalog Ollama-free.
      providers = {
        ollama = mkBoolOption { default = providers.ollama; };
      };

      # Whether `icedos gc` prunes stale prime-agent sessions (unshade-style).
      includeInIcedosGc = mkBoolOption { default = includeInIcedosGc; };

      # Sessions/logs older than this many days are pruned by the GC hook.
      sessionRetentionDays = mkIntBetweenOption {
        path = "icedos.ai-tools.prime-agent.sessionRetentionDays";
        source = ./config.toml;
        default = sessionRetentionDays;
      } 1 3650;

      # Status glyph ahead of the terminal title. Working frames cycle per pulse tick
      # (repeat one to hold it longer); give every glyph the same width in the title font.
      terminal-title = {
        idleGlyph = mkStrOption { default = terminal-title.idleGlyph; };
        workingGlyphs = mkStrListOption { default = terminal-title.workingGlyphs; };
      };

      extensions = {
        # Built-in example extensions to load (names under examples/extensions/).
        builtin = mkStrListOption { default = extensions.builtin; };

        # Inline .ts sources for auto-loaded local extensions (like Claude skills).
        inline = mkAttrsOfOption { default = extensions.inline; } types.str;

        # Plain-text rules (name = markdown), each appended to the system prompt every turn.
        rules = mkAttrsOfOption { default = extensions.rules; } types.lines;

        # Install the peon-ping companion extension (session-event bridge to peon.sh).
        peonPing = mkBoolOption { default = extensions.peonPing; };

        context-window-cap = {
          # Install the context-cap extension.
          enable = mkBoolOption { default = extensions.context-window-cap.enable; };

          # Cap every model's contextWindow at this percent of its native context.
          # Excludes llamacpp and models with a contextWindow override.
          percent = mkIntBetweenOption {
            path = "icedos.ai-tools.prime-agent.extensions.context-window-cap.percent";
            source = ./config.toml;
            default = extensions.context-window-cap.percent;
          } 1 100;
        };

        context-language-models = {
          # Install the pi-clm extension (Context Language Models).
          enable = mkBoolOption { default = extensions.context-language-models.enable; };
          budget = mkStrOption { default = extensions.context-language-models.budget or ""; };
          reserve = mkStrOption { default = extensions.context-language-models.reserve or ""; };
          remindAt = mkStrOption { default = extensions.context-language-models.remindAt or ""; };
          observationCap = mkStrOption { default = extensions.context-language-models.observationCap or ""; };
          steering = mkStrOption { default = extensions.context-language-models.steering or ""; };
          overflow = mkStrOption { default = extensions.context-language-models.overflow; };
          estimateFactor = mkStrOption { default = extensions.context-language-models.estimateFactor or ""; };
          oneToolPerTurn = mkBoolOption { default = extensions.context-language-models.oneToolPerTurn; };
          sizeTrailer = mkBoolOption { default = extensions.context-language-models.sizeTrailer; };
          nativeCompaction = mkStrOption { default = extensions.context-language-models.nativeCompaction; };
          compactPrompt = mkStrOption { default = extensions.context-language-models.compactPrompt or ""; };
        };

        prompt-suggestions = {
          # Install the prompt-suggest extension: predicted next prompt as ghost text, Tab accepts.
          enable = mkBoolOption { default = extensions.prompt-suggestions.enable; };

          # "provider/model-id" making the prediction; "" uses the session model.
          model = mkStrOption { default = extensions.prompt-suggestions.model; };
        };

        meters = {
          # Install the cost-footer extension: live session cost (USD, or € for
          # metered local models) in the TUI bottom bar, with token totals.
          cost = mkBoolOption { default = extensions.meters.cost; };

          power = {
            # Meter GPU electricity for locally-served models inside the cost footer.
            enable = mkBoolOption { default = extensions.meters.power.enable; };

            # DRM card to read ("card1"); empty autodetects the first GPU with a sensor.
            card = mkStrOption { default = extensions.meters.power.card; };

            # Price per kWh, in the currency `currency` names.
            rateKwh = mkNumberOption { default = extensions.meters.power.rateKwh; };

            # Idle draw subtracted from each sample, so the meter reports marginal cost.
            idleWatts = mkNumberOption { default = extensions.meters.power.idleWatts; };

            currency = mkStrOption { default = extensions.meters.power.currency; };

            # Providers served by the local GPU; everything else is left unmetered.
            providers = mkStrListOption { default = extensions.meters.power.providers; };

            # Rolling cost windows shown in the footer ("1s", "15m", "24h", "2w", "1M" = calendar month); empty uses 1h/24h/7d/30d.
            windows = mkStrListOption { default = extensions.meters.power.windows; };
          };
        };
      };

      skills = {
        # Extra built-in skills: name -> full SKILL.md content, shipped into dist/skills so
        # they autoload on start; each needs frontmatter `description` (loader skips without).
        extra-builtin = mkAttrsOfOption { default = skills.extra-builtin; } types.str;

        dirs = mkStrListOption { default = skills.dirs; };

        code = {
          # Ship the code-review skill: reviewer children per dimension, then a scoring
          # pass that drops every finding below 80 confidence.
          review = mkBoolOption { default = skills.code.review; };
        };
      };

      settings = {
        dataDir = mkStrOption { default = settings.dataDir; };

        mcpCallTimeout = mkIntBetweenOption {
          path = "icedos.ai-tools.prime-agent.settings.mcpCallTimeout";
          source = ./config.toml;
          default = settings.mcpCallTimeout;
        } 60 3600;

        ports = {
          # Bound is 65335 so base + (sha256(name) mod 200) stays <= 65535.
          base = mkIntBetweenOption {
            path = "icedos.ai-tools.prime-agent.settings.ports.base";
            source = ./config.toml;
            default = settings.ports.base;
          } 0 65335;

          # Pins are system-wide; derived ports fold the username in and cannot collide.
          overrides = mkAttrsOfOption { default = settings.ports.overrides; } (
            lib.types.ints.between 1 65535
          );
        };

        # Provider overrides merged into models.json (modelOverrides is partial merge).
        providers = mkSubmoduleAttrsOption { default = settings.providers; } {
          apiKey = lib.mkOption {
            type = lib.types.nullOr lib.types.str;
            default = null;
            description = "API key for the provider. null = hardcoded default.";
          };

          baseUrl = lib.mkOption {
            type = lib.types.nullOr lib.types.str;
            default = null;
            description = "Base URL for the provider API.";
          };

          api = lib.mkOption {
            type = lib.types.nullOr lib.types.str;
            default = null;
            description = "API protocol for the provider (e.g. openai-completions).";
          };

          headers = lib.mkOption {
            type = lib.types.attrsOf lib.types.str;
            default = { };
            description = "Extra HTTP headers for provider requests.";
          };

          modelOverrides = lib.mkOption {
            type = lib.types.attrsOf (
              lib.types.submodule {
                options = {
                  contextWindow = lib.mkOption {
                    type = lib.types.nullOr lib.types.int;
                    default = null;
                    description = "Context window size in tokens.";
                  };

                  maxTokens = lib.mkOption {
                    type = lib.types.nullOr lib.types.int;
                    default = null;
                    description = "Max output tokens per response.";
                  };

                  name = lib.mkOption {
                    type = lib.types.nullOr lib.types.str;
                    default = null;
                    description = "Model display name.";
                  };

                  reasoning = lib.mkOption {
                    type = lib.types.nullOr lib.types.bool;
                    default = null;
                    description = "Whether the model supports reasoning.";
                  };

                  thinkingLevelMap = lib.mkOption {
                    type = lib.types.nullOr (lib.types.attrsOf (lib.types.nullOr lib.types.str));
                    default = null;
                    description = "Maps prime-agent thinking levels to the reasoning_effort the model accepts. Keys must be one of off, minimal, low, medium, high, xhigh or max; prime-agent's schema is non-strict, so a misspelled level is accepted here and then silently ignored at runtime. A level absent from the map is sent through unmapped, so a model that rejects it errors; null hides the level entirely, and xhigh/max are only offered when present as keys.";
                  };
                };
              }
            );

            default = { };
            description = "Per-model overrides keyed by model id.";
          };

          # Custom model definitions emitted into models.json "models": an unknown id
          # is ADDED (inheriting the built-in api/baseUrl); a known id replaces it.
          models = lib.mkOption {
            type = lib.types.listOf (
              lib.types.submodule {
                options = {
                  id = lib.mkOption {
                    type = lib.types.str;
                    description = "Model id (also the key used in modelOverrides).";
                  };

                  name = lib.mkOption {
                    type = lib.types.nullOr lib.types.str;
                    default = null;
                    description = "Model display name.";
                  };

                  reasoning = lib.mkOption {
                    type = lib.types.nullOr lib.types.bool;
                    default = null;
                    description = "Whether the model supports reasoning.";
                  };

                  contextWindow = lib.mkOption {
                    type = lib.types.nullOr lib.types.int;
                    default = null;
                    description = "Context window size in tokens.";
                  };

                  maxTokens = lib.mkOption {
                    type = lib.types.nullOr lib.types.int;
                    default = null;
                    description = "Max output tokens per response.";
                  };

                  thinkingLevelMap = lib.mkOption {
                    type = lib.types.nullOr (lib.types.attrsOf (lib.types.nullOr lib.types.str));
                    default = null;
                    description = "Maps prime-agent thinking levels to the reasoning_effort the model accepts. Keys must be one of off, minimal, low, medium, high, xhigh or max; prime-agent's schema is non-strict, so a misspelled level is accepted here and then silently ignored at runtime. A level absent from the map is sent through unmapped, so a model that rejects it errors; null hides the level entirely, and xhigh/max are only offered when present as keys.";
                  };

                  input = lib.mkOption {
                    type = lib.types.listOf (
                      lib.types.enum [
                        "text"
                        "image"
                      ]
                    );
                    default = [ ];
                    description = "Input modalities the model accepts.";
                  };

                  cost = lib.mkOption {
                    type = lib.types.nullOr (lib.types.attrsOf lib.types.number);
                    default = null;
                    description = "Per-token cost (USD per 1M tokens). prime-agent requires input, output, cacheRead, cacheWrite.";
                  };

                  api = lib.mkOption {
                    type = lib.types.nullOr lib.types.str;
                    default = null;
                    description = "API protocol; defaults to the provider's built-in api.";
                  };

                  baseUrl = lib.mkOption {
                    type = lib.types.nullOr lib.types.str;
                    default = null;
                    description = "API base URL; defaults to the provider's built-in baseUrl.";
                  };
                };
              }
            );

            default = [ ];
            description = "Custom model definitions for providers. Adds a model the bundled catalog lacks (e.g. a newly released one) so it appears in the model list without rebuilding the package.";
          };
        };

        # Exported as RLM_MAX_DEPTH; a global /rlm-max-depth outranks it, so a TUI
        # choice survives rebuilds. Upper bound guards typos: children fan out per level.
        rlmMaxDepth = mkIntBetweenOption {
          path = "icedos.ai-tools.prime-agent.settings.rlmMaxDepth";
          source = ./config.toml;
          default = settings.rlmMaxDepth;
        } 0 32;

        # Upload full session traces (transcripts, cwd, git repo/commit) to Prime
        # Intellect to train open-source LLMs. Off by default; /traces on toggles it.
        shareTraces = mkBoolOption { default = settings.shareTraces; };

        # Send product analytics (token usage, model/provider categories) to Prime
        # Intellect. Off by default (flips upstream's telemetry.enabled = true).
        telemetry = mkBoolOption { default = settings.telemetry; };

        # Start every session with the native /speed footer readout (output tok/s), via terminal.speed.
        speed = mkBoolOption { default = settings.speed; };

        # "provider/model-id" serving refine, compaction and branch summaries; "" keeps the session model.
        auxiliaryModel = mkStrOption { default = settings.auxiliaryModel; };

        # "provider/model-id" serving turns that carry images when the session model is text-only.
        imageModel = mkStrOption { default = settings.imageModel; };
      };
      users = mkSubmoduleAttrsOption { default = { }; } { };
    };

  outputs.nixosModules =
    { repoUrl, ... }:
    [
      (
        {
          config,
          lib,
          pkgs,
          ...
        }:

        let
          inherit (lib)
            concatMapStrings
            concatStrings
            concatStringsSep
            mapAttrs'
            mkIf
            mkMerge
            nameValuePair
            replaceStrings
            splitString
            stringToCharacters
            substring
            toUpper
            ;

          hasAccounts = import ../../integrations/ai-accounts/lib/has-accounts.nix { inherit lib; } config;
          wrapAccounts = import ../../integrations/ai-accounts/lib/wrap.nix { inherit lib pkgs; };
          aiAccountsOn = icedosLib.hasModule {
            inherit config repoUrl;
            name = "ai-accounts";
          };

          cfg = config.icedos.ai-tools.prime-agent;

          # Each rule becomes an extension appending its text to the system prompt.
          ruleExtension = text: ''
            import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

            const RULE = ${builtins.toJSON text};

            export default function rule(pi: ExtensionAPI) {
              pi.on("before_agent_start", (event) => ({
                systemPrompt: `''${event.systemPrompt}\n\n''${RULE}`,
              }));
            }
          '';

          # Shared icedos.ai-tools entries first, so a same-named prime-agent entry wins.
          prime-agent = cfg // {
            skills = cfg.skills // {
              extra-builtin = config.icedos.ai-tools.skills // cfg.skills.extra-builtin;
            };
            extensions = cfg.extensions // {
              inline =
                lib.mapAttrs' (name: text: lib.nameValuePair "rule-${name}" (ruleExtension text)) (
                  config.icedos.ai-tools.rules // cfg.extensions.rules
                )
                // cfg.extensions.inline;
            };
          };
          primeAgentUsers = prime-agent.users;

          # peon-ping is a standalone module and may not be loaded at all.
          peonPingEnabled = (config.icedos.ai-tools.peon-ping.users or { }) != { };

          # Prune stale prime-agent session data during `icedos gc`.
          primeAgentGcHook = ''
            D='${prime-agent.settings.dataDir}'
            # Matches config.xdg.configHome default ($HOME/.config); env -i strips
            # per-user xdg, so non-default paths go through the dataDir option.
            [ -z "$D" ] && D='$HOME/.config/prime-agent'
            # Expand XDG/tilde refs under the minimal gc env (HOME-only).
            D=''${D/\$XDG_CONFIG_HOME/$HOME/.config}
            D=''${D/\$XDG_DATA_HOME/$HOME/.local/share}
            D=''${D/#~/\$HOME}
            D=''${D//\$HOME/$HOME}
            [ -d "''${D}" ] || { log_warn "prime-agent gc: data dir ''${D} not found; skipping"; exit 0; }
            find "''${D}/sessions" -maxdepth 1 -type f -name '*.jsonl' -mtime "+${toString prime-agent.sessionRetentionDays}" -delete 2>/dev/null || true
            for a in "''${D}"/session-artifacts/*; do
              [ -d "$a" ] || continue
              [ -f "''${D}/sessions/$(basename "$a").jsonl" ] && continue
              [ -n "$(find "$a" -prune -mtime "+${toString prime-agent.sessionRetentionDays}" -print)" ] && rm -rf -- "$a"
            done
            find "''${D}/logs" -maxdepth 1 -type f -mtime "+${toString prime-agent.sessionRetentionDays}" -delete 2>/dev/null || true
          '';

          # ---- skills.extra-builtin validation ----
          skillName = import ./lib/skill-name.nix { inherit lib; };
          invalidSkillNames = skillName.invalidSkillNames (lib.attrNames prime-agent.skills.extra-builtin);

          takeWhile =
            pred: list:
            if list == [ ] then
              [ ]
            else if pred (lib.head list) then
              [ (lib.head list) ] ++ takeWhile pred (lib.tail list)
            else
              [ ];

          # Frontmatter block (between the first two `---` lines); [] if absent.
          skillFrontmatter =
            content:
            let
              lines = lib.splitString "\n" content;
              rest = if lines == [ ] then [ ] else lib.drop 1 lines;
            in
            if lines == [ ] || lib.head lines != "---" then [ ] else takeWhile (l: l != "---") rest;

          hasSkillDescription =
            content: lib.any (l: builtins.match "description[ \t]*:.*" l != null) (skillFrontmatter content);

          descriptionlessSkillNames = lib.attrNames (
            lib.filterAttrs (_: c: !hasSkillDescription c) prime-agent.skills.extra-builtin
          );

          # dataDir as a path the wrapper expands at runtime. The home-manager copy is
          # resolved per user at eval time; this one has to survive an empty environment,
          # so the XDG vars carry their spec defaults.
          shellDataDir =
            let
              raw =
                if prime-agent.settings.dataDir == "" then
                  "$XDG_CONFIG_HOME/prime-agent"
                else
                  prime-agent.settings.dataDir;
              subst =
                builtins.replaceStrings
                  [ "$XDG_CONFIG_HOME" "$XDG_DATA_HOME" ]
                  [ "\${XDG_CONFIG_HOME:-$HOME/.config}" "\${XDG_DATA_HOME:-$HOME/.local/share}" ]
                  raw;
            in
            if subst == "~" then
              "$HOME"
            else if lib.hasPrefix "~/" subst then
              "$HOME" + lib.removePrefix "~" subst
            else
              subst;
        in
        {
          assertions = [
            {
              # Filled by whichever module serves models locally; without one the meter loads nothing.
              # Read at NixOS level: sharedModules reads would cross-depend on another entry and stall.
              assertion =
                !config.icedos.ai-tools.prime-agent.extensions.meters.power.enable
                || config.icedos.ai-tools.prime-agent.extensions.meters.power.providers != [ ];
              message = ''
                icedos.ai-tools.prime-agent.extensions.meters.power.enable is on but
                meters.power.providers is empty, so nothing would be metered. Load a
                module that serves models locally (llamacpp adds itself) or name
                the provider explicitly.
              '';
            }
            {
              assertion = invalidSkillNames == [ ];
              message = ''
                icedos.ai-tools.prime-agent.skills.extra-builtin names must be
                lowercase a-z, 0-9 and hyphens only (no leading/trailing or double
                hyphens, max 64 chars), matching the loader's directory names:
                ${builtins.concatStringsSep ", " invalidSkillNames}
              '';
            }
            {
              assertion = descriptionlessSkillNames == [ ];
              message = ''
                icedos.ai-tools.prime-agent.skills.extra-builtin entries must be
                full SKILL.md files with frontmatter `description:`; the loader
                silently skips files without one. Entries missing it:
                ${builtins.concatStringsSep ", " descriptionlessSkillNames}
              '';
            }
          ];

          # Copy of nixpkgs PR #550774 at 0.7.4; drop overlay, package.nix and patch once merged.
          nixpkgs.overlays = [
            (final: _prev: {
              prime-agent = final.callPackage ./package.nix {
                inherit (icedosLib.packaging) installDesktopEntry;
                desktopEntry = prime-agent.desktopEntry;
                defaultAgentDir = shellDataDir;
                extraBuiltinSkills = prime-agent.skills.extra-builtin;
                codeReview = prime-agent.skills.code.review;
                ollamaCloud = prime-agent.providers.ollama;
                titleIdleGlyph = prime-agent.terminal-title.idleGlyph;
                titleWorkingGlyphs = prime-agent.terminal-title.workingGlyphs;
              };
            })
          ];

          environment.systemPackages = [
            (
              if aiAccountsOn && hasAccounts "prime-agent" then
                wrapAccounts {
                  agent = "prime-agent";
                  package = pkgs.prime-agent;
                  bin = "prime-agent";
                }
              else
                pkgs.prime-agent
            )
          ];

          icedos.ai-tools.prime-agent.users = icedosLib.users.genDefaults {
            inherit (config.icedos) users;
          };

          icedos.ai-tools.agent-launch.agents.prime-agent = {
            command = lib.mkDefault [ "prime-agent" ];
            resumeArgs = lib.mkDefault [
              "--resume"
              "{id}"
            ];
            locator = lib.mkDefault "prime-agent";
            label = lib.mkDefault "Prime Agent";
          };

          # `icedos gc` prunes stale prime-agent sessions per user (unshade-style).
          icedos.system.gc.hooks.postGc = mkIf prime-agent.includeInIcedosGc [
            primeAgentGcHook
          ];

          # Same TUI the Zed task spawns, as a toolset leaf so `icedos <tool>` stays uniform.
          icedos.system.toolset.commands = lib.mkIf (primeAgentUsers != { }) [
            {
              command = "prime-agent";
              bin = "${pkgs.prime-agent}/bin/prime-agent";
              help = "prime-agent AI assistant (interactive session)";
            }
          ];

          home-manager.sharedModules = [
            (
              {
                config,
                lib,
                pkgs,
                ...
              }:

              let
                userCfg = primeAgentUsers.${config.home.username} or null;
                # ---- data dir ----
                # "" means the default; expanded per user here (prime-agent knows ~, not $VARS).
                dataDir =
                  let
                    raw =
                      if prime-agent.settings.dataDir == "" then
                        "$XDG_CONFIG_HOME/prime-agent"
                      else
                        prime-agent.settings.dataDir;

                    expand =
                      builtins.replaceStrings
                        [
                          "$XDG_CONFIG_HOME"
                          "$XDG_DATA_HOME"
                          "$HOME"
                        ]
                        [
                          config.xdg.configHome
                          config.xdg.dataHome
                          config.home.homeDirectory
                        ]
                        raw;
                    # "$HOME/" must hit the rejected "$HOME" case, not a leading-slash relDataDir.
                    stripped =
                      let
                        stripTrailing =
                          s: if s != "/" && lib.hasSuffix "/" s then stripTrailing (lib.removeSuffix "/" s) else s;
                      in
                      stripTrailing expand;
                    # Collapse repeated slashes; they would yield a leading-slash relDataDir.
                    collapse =
                      s:
                      let
                        s' = builtins.replaceStrings [ "//" ] [ "/" ] s;
                      in
                      if s' == s then s else collapse s';
                  in
                  collapse (
                    if stripped == "~" then
                      config.home.homeDirectory
                    else if lib.hasPrefix "~/" stripped then
                      config.home.homeDirectory + lib.removePrefix "~" stripped
                    else
                      stripped
                  );
                # power.ts carries build-time constants, so the dir is copied, not symlinked.
                # The markers sit inside TS string literals; jsStr escapes values for that context, which escapeShellArg (protecting the builder, not the literal) does not.
                powerWindows =
                  let
                    w = prime-agent.extensions.meters.power.windows;
                  in
                  if w == [ ] then
                    [
                      "1h"
                      "24h"
                      "7d"
                      "30d"
                    ]
                  else
                    w;
                jsStr =
                  v: lib.escapeShellArg (lib.replaceStrings [ "\\" "\"" "\n" "\r" ] [ "\\\\" "\\\"" "\\n" "\\r" ] v);
                costFooterSrc = pkgs.runCommand "prime-agent-cost-footer" { } ''
                  cp -r ${./extensions/cost-footer} $out
                  chmod -R +w $out
                  substituteInPlace $out/power.ts \
                    --replace-fail "@powerCard@" ${jsStr prime-agent.extensions.meters.power.card} \
                    --replace-fail "@powerRate@" ${jsStr (builtins.toJSON prime-agent.extensions.meters.power.rateKwh)} \
                    --replace-fail "@powerIdle@" ${jsStr (builtins.toJSON prime-agent.extensions.meters.power.idleWatts)} \
                    --replace-fail "@powerCurrency@" ${jsStr prime-agent.extensions.meters.power.currency} \
                    --replace-fail "@powerWindows@" ${jsStr (lib.concatStringsSep "," powerWindows)} \
                    --replace-fail "@powerProviders@" ${
                      jsStr (
                        lib.concatStringsSep "," (
                          if prime-agent.extensions.meters.power.enable then
                            prime-agent.extensions.meters.power.providers
                          else
                            [ ]
                        )
                      )
                    }

                  # substituteInPlace only fails on a marker it was told about, so
                  # a newly added one would ship as a literal and break the load.
                  if ${pkgs.gnugrep}/bin/grep -rqE '@[a-zA-Z_][0-9A-Za-z_-]*@' $out; then
                    echo "unsubstituted placeholder left in cost-footer" >&2
                    exit 1
                  fi
                '';
                # home.file keys are $HOME-relative; only in-home dataDirs work (asserted below).
                relDataDir = lib.removePrefix (config.home.homeDirectory + "/") dataDir;
                # ---- shared MCP registry ----
                # Also consumed by opencode; gated on programs.mcp.enable, where hm validates it.
                registry = if config.programs.mcp.enable or false then config.programs.mcp.servers or { } else { };
                # enabled/disabled both disable; home-manager's resolver is unexported, so mirror it.
                isEnabled =
                  s:
                  if (s.enabled or null) != null then
                    s.enabled
                  else if (s.disabled or null) != null then
                    !s.disabled
                  else
                    true;

                enabled = lib.filterAttrs (_: isEnabled) registry;

                localServers = lib.filterAttrs (_: s: s.command != null) enabled;
                # ---- deterministic per-server ports ----
                # ports.base + sha256(name) mod 200, collisions bumped; ports.overrides wins.

                hexDigit =
                  c:
                  {
                    "0" = 0;
                    "1" = 1;
                    "2" = 2;
                    "3" = 3;
                    "4" = 4;
                    "5" = 5;
                    "6" = 6;
                    "7" = 7;
                    "8" = 8;
                    "9" = 9;
                    "a" = 10;
                    "b" = 11;
                    "c" = 12;
                    "d" = 13;
                    "e" = 14;
                    "f" = 15;
                  }
                  .${c};

                hashInt = hex: lib.foldl' (acc: c: acc * 16 + hexDigit c) 0 (stringToCharacters hex);

                # Nix has no integer modulo operator; fold modulo by hand.
                mod = a: b: a - b * (a / b);
                # Username folded in so two users' bridges cannot collide; pins stay system-wide.
                hashPort =
                  name:
                  prime-agent.settings.ports.base
                  + mod (hashInt (
                    substring 0 6 (builtins.hashString "sha256" "${config.home.username}:${name}")
                  )) 200;

                portsByServer =
                  let
                    sorted = lib.sort (a: b: a.name < b.name) (
                      lib.mapAttrsToList (name: s: s // { inherit name; }) localServers
                    );
                    # Reserved up front so derived ports bump around pins; double pins fail below.
                    pinnedUsed = lib.listToAttrs (
                      map (p: lib.nameValuePair (toString p) true) (lib.attrValues prime-agent.settings.ports.overrides)
                    );

                    step =
                      { ports, used }:
                      s:
                      let
                        find = n: if used.${toString n} or false then find (n + 1) else n;
                        p =
                          if prime-agent.settings.ports.overrides ? ${s.name} then
                            prime-agent.settings.ports.overrides.${s.name}
                          else
                            find (hashPort s.name);
                      in
                      {
                        ports = ports // {
                          ${s.name} = p;
                        };
                        used = used // {
                          ${toString p} = true;
                        };
                      };
                  in
                  (lib.foldl' step {
                    ports = { };
                    used = pinnedUsed;
                  } sorted).ports;

                # Shared final ports; empty unless the config is wrong, asserted below.
                duplicatePorts =
                  let
                    entries = lib.mapAttrsToList (name: p: { inherit name p; }) portsByServer;
                  in
                  lib.filter (e: 1 < lib.length (lib.filter (o: o.p == e.p) entries)) entries;

                portFor = name: portsByServer.${name};

                urlFor =
                  name: s: if s.command != null then "http://127.0.0.1:${toString (portFor name)}/mcp" else s.url;

                # Via home-manager's transform so freeform keys survive; prime-agent only speaks HTTP.
                mcpServers = lib.mapAttrs (
                  name: s:
                  lib.hm.mcp.transformMcpServer {
                    server = s;
                    exclude = [
                      "args"
                      "command"
                      "enabled"
                      "env"
                    ];
                    extraTransforms = [
                      (
                        srv:
                        # Default per-server timeout; a server config setting callTimeoutMs wins.
                        {
                          callTimeoutMs = prime-agent.settings.mcpCallTimeout * 1000;
                        }
                        // srv
                        // {
                          type = "http";
                          url = urlFor name srv;
                        }
                      )
                    ];
                  }
                ) enabled;

                # Local bridges accept any token; remotes get none, as they can reject the header.
                dummyCreds = mapAttrs' (
                  name: _:
                  nameValuePair "mcp:${name}" {
                    type = "api_key";
                    key = "dummy";
                  }
                ) localServers;

                remoteServers = lib.filterAttrs (_: s: s.command == null) enabled;

                # Named so the seed script can retract exactly the dummies it once wrote.
                remoteCredKeysFile = pkgs.writeText "prime-agent-remote-cred-keys.json" (
                  builtins.toJSON (map (name: "mcp:${name}") (lib.attrNames remoteServers))
                );

                isNoAuthRemote =
                  s: s.command == null && (s.bearerTokenEnvVar or null) == null && !(s.oauth or false);

                # Seed-wins merge, so empty values would reset TUI choices; mcpServers always rides along.
                seedSettings =
                  (lib.optionalAttrs (prime-agent.default.provider != "") {
                    defaultProvider = prime-agent.default.provider;
                  })
                  // (lib.optionalAttrs (prime-agent.default.model != "") {
                    defaultModel = prime-agent.default.model;
                  })
                  // (lib.optionalAttrs (prime-agent.settings.auxiliaryModel != "") {
                    auxiliaryModel = prime-agent.settings.auxiliaryModel;
                  })
                  // (lib.optionalAttrs (prime-agent.settings.imageModel != "") {
                    imageModel = prime-agent.settings.imageModel;
                  })
                  // (lib.optionalAttrs (prime-agent.skills.dirs != [ ]) {
                    skills = prime-agent.skills.dirs;
                  })
                  // {
                    mcpServers = mcpServers;
                    # Deterministic even at the upstream default (agentTraces off).
                    agentTraces.enabled = prime-agent.settings.shareTraces;
                    # Upstream defaults this to true; expose it so a user can flip it on.
                    telemetry.enabled = prime-agent.settings.telemetry;
                    terminal.speed = prime-agent.settings.speed;
                  };

                # ---- per-server skill files ----
                importName = name: replaceStrings [ "-" ] [ "_" ] name;

                className =
                  name: concatStrings (map (p: toUpper (substring 0 1 p) + substring 1 99 p) (splitString "-" name));

                skillMd = name: ''
                  ---
                  name: ${name}
                  description: ${name} MCP integration — import ${importName name} in the kernel and call its tools over the local prime-agent MCP bridge.
                  ---

                  # ${name} MCP integration

                  The ${name} server is exposed as a Python-backed skill so the kernel can
                  call it over MCP. Import the module and discover tools before calling:

                  ```python
                  import ${importName name}

                  for tool in await ${importName name}.list_tools():
                      print(tool["name"], "-", tool["description"])

                  result = await ${importName name}.call_tool("<tool_name>", {...})
                  ```

                  - Every tool is an `async` method — always `await`.
                  - Results are already-parsed Python (dict / str / list of content blocks).
                  - A tool whose name isn't a valid Python identifier is called via the
                    escape hatch: `await ${importName name}.call_tool("tool-name", {...})`.
                  - If a call fails to connect, the local MCP service may be down:
                    `systemctl --user status prime-agent-${name}`.
                '';

                pyproject = name: ''
                  [project]
                  name = "prime-agent-skill-${name}"
                  version = "0.1.0"
                  requires-python = ">=3.10"
                  dependencies = ["mcp", "httpx", "prime-agent-runtime"]

                  [build-system]
                  requires = ["hatchling"]
                  build-backend = "hatchling.build"

                  [tool.hatch.build.targets.wheel]
                  packages = ["src/${importName name}"]
                '';

                pyStr = s: "\"" + replaceStrings [ "\\" "\"" ] [ "\\\\" "\\\"" ] s + "\"";

                pyDict =
                  attrs:
                  "{" + concatStringsSep ", " (lib.mapAttrsToList (k: v: "${pyStr k}: ${pyStr v}") attrs) + "}";

                # Nix strips the block's common indent, dropping this method to column 0.
                indentPy =
                  block:
                  concatStringsSep "\n" (
                    map (line: if line == "" then "" else "    " + line) (splitString "\n" block)
                  );

                # _open_session always sends Authorization; a keyless endpoint may reject it.
                noAuthSession = ''
                  async def _open_session(self, stack):
                      import inspect

                      from mcp import ClientSession

                      try:
                          from rlm.mcp_base import _resolve_streamable_http
                      except ImportError as exc:
                          raise RuntimeError(
                              "rlm.mcp_base._resolve_streamable_http is gone; the no-auth MCP "
                              "override generated by the IceDOS prime-agent module needs updating"
                          ) from exc

                      url, headers = await self._resolve_config()
                      if not url:
                          raise ValueError(f"{type(self).__name__} has no url")
                      transport = _resolve_streamable_http()
                      params = inspect.signature(transport).parameters
                      if "headers" in params:
                          cm = transport(url, headers=headers)
                      elif "http_client" in params:
                          import httpx

                          client = await stack.enter_async_context(httpx.AsyncClient(headers=headers))
                          cm = transport(url, http_client=client)
                      else:
                          raise RuntimeError(f"unsupported streamable-HTTP signature: {tuple(params)}")
                      read, write, *_ = await stack.enter_async_context(cm)
                      session = await stack.enter_async_context(ClientSession(read, write))
                      await session.initialize()
                      return session'';

                initPy =
                  name: s: url:
                  let
                    body = [
                      "    server = ${pyStr name}"
                      "    url = ${pyStr url}"
                    ]
                    # A remote server may need headers to answer at all.
                    ++ lib.optional (s.headers or { } != { }) "    headers = ${pyDict s.headers}"
                    # _token() checks bearer_token_env before auth.json; these skip the cred seed.
                    ++ lib.optional (
                      (s.bearerTokenEnvVar or null) != null
                    ) "    bearer_token_env = ${pyStr s.bearerTokenEnvVar}"
                    ++ lib.optional (isNoAuthRemote s) (indentPy noAuthSession);
                  in
                  ''
                    from rlm import McpIntegration


                    class ${className name}(McpIntegration):
                    ${concatStringsSep "\n" body}


                    ${importName name} = ${className name}()

                    _RESERVED = {"run", "__wrapped__", "__call__"}


                    def __getattr__(name):
                        if name.startswith("_") or name in _RESERVED:
                            raise AttributeError(name)
                        return getattr(${importName name}, name)
                  '';

                skillFiles = name: s: {
                  "${relDataDir}/skills/${name}/SKILL.md".text = skillMd name;
                  "${relDataDir}/skills/${name}/pyproject.toml".text = pyproject name;
                  "${relDataDir}/skills/${name}/src/${importName name}/__init__.py".text = initPy name s (
                    urlFor name s
                  );
                };

                # ---- systemd user services (local servers) ----

                # ExecStart is one line: quote each argv element, `$` as `$$` for the inner shell.
                quoteSystemdArg =
                  arg:
                  "\""
                  + concatMapStrings (
                    c:
                    if c == "\\" then
                      "\\\\"
                    else if c == "\"" then
                      "\\\""
                    else if c == "$" then
                      "$$"
                    else
                      c
                  ) (stringToCharacters arg)
                  + "\"";
                # Environment= does not expand `$`, and reads `%` as a specifier, so double it.
                quoteSystemdEnv =
                  arg:
                  "\""
                  + concatMapStrings (
                    c:
                    if c == "\\" then
                      "\\\\"
                    else if c == "\"" then
                      "\\\""
                    else if c == "%" then
                      "%%"
                    else
                      c
                  ) (stringToCharacters arg)
                  + "\"";
                # env may hold secret file refs; hm wraps those, leaving literals for Environment=.
                withEnvFiles = name: s: lib.hm.mcp.wrapEnvFilesCommand { inherit pkgs name; } s;
                # --pass-environment is required: unlike supergateway's `sh -c`, the child gets no env.
                mcpProxy = "${pkgs.mcp-proxy}/bin/mcp-proxy";

                execStart =
                  name: s:
                  let
                    wrapped = withEnvFiles name s;
                  in
                  concatStringsSep " " (
                    map quoteSystemdArg (
                      [
                        mcpProxy
                        "--host"
                        "127.0.0.1"
                        "--port"
                        (toString (portFor name))
                        "--pass-environment"
                        "--"
                        wrapped.command
                      ]
                      ++ (wrapped.args or [ ])
                    )
                  );

                service =
                  name: s:
                  let
                    # Literal env only; file refs were consumed by the wrapper.
                    literalEnv = (withEnvFiles name s).env or { };
                  in
                  {
                    "prime-agent-${name}" = {
                      Unit = {
                        Description = "prime-agent ${name} MCP server";
                        After = [ "network.target" ];
                      };
                      Service = {
                        ExecStart = execStart name s;
                        Environment = [
                          # Don't depend on whatever PATH the user manager inherited.
                          (quoteSystemdEnv "PATH=${
                            concatStringsSep ":" [
                              "/run/current-system/sw/bin"
                              "${pkgs.nix}/bin"
                              "${config.home.profileDirectory}/bin"
                            ]
                          }")
                        ]
                        ++ lib.mapAttrsToList (k: v: quoteSystemdEnv "${k}=${v}") literalEnv;
                        Restart = "on-failure";
                        RestartSec = 5;
                      };
                      Install.WantedBy = [ "default.target" ];
                    };
                  };

                # opencode 429s unless identified as a client; zen also wants a session
                # header, which is per-run, so zen-session.ts registers both at load.
                opencodeVersion = config.programs.opencode.package.version or pkgs.opencode.version;
                # zen/v1 and zen/go/v1 are separate providers upstream; both 429 unidentified.
                providerDefaults.opencode.headers."User-Agent" = "opencode/${opencodeVersion}";
                providerDefaults.opencode-go.headers."User-Agent" = "opencode/${opencodeVersion}";

                zenSessionSrc = pkgs.replaceVars ./extensions/zen-session.ts {
                  inherit opencodeVersion;
                };

                # Caps model context windows at extensions.context-window-cap.percent of native;
                # llamacpp + explicitly-overridden models are left untouched.
                contextCapSrc = pkgs.replaceVars ./extensions/context-cap.ts {
                  contextWindowPercent = prime-agent.extensions.context-window-cap.percent;
                };

                # Context Language Models (pi-clm) extension.
                clmDeriv = pkgs.callPackage ./extensions/pi-clm.nix { };
                clmSrc = clmDeriv;
                # modelOverrides stays as-is for partial merge; models[] merges in
                # custom models (adds unknown ids, replaces known ones).
                userProvidersConverted = lib.mapAttrs (
                  name: p:
                  let
                    attrs = lib.filterAttrs (_: v: v != null) {
                      inherit (p)
                        api
                        apiKey
                        baseUrl
                        headers
                        ;
                    };
                    overrides = lib.mapAttrs (_: o: lib.filterAttrs (_: v: v != null) o) (p.modelOverrides or { });
                    models = map (m: lib.filterAttrs (_: v: v != null) m) (p.models or [ ]);
                  in
                  attrs
                  // lib.optionalAttrs (overrides != { }) { modelOverrides = overrides; }
                  // lib.optionalAttrs (models != [ ]) { models = models; }
                ) prime-agent.settings.providers;

                mergedProviders = lib.recursiveUpdate providerDefaults userProvidersConverted;

                modelsJson = builtins.toJSON { providers = mergedProviders; };

                modelsFile = pkgs.writeText "prime-agent-models.json" modelsJson;
                # ---- first-run seed ----
                # Idempotent: never clobbers state prime-agent writes itself.

                seedSettingsFile = pkgs.writeText "prime-agent-settings.json" (builtins.toJSON seedSettings);
                dummyCredsFile = pkgs.writeText "prime-agent-auth-seed.json" (builtins.toJSON dummyCreds);
                # `install` not `cp` (cp copies the store read-only mode), `+` not `*` for
                # auth.json (`*` recurses into credentials), `--slurpfile` (jq 1.8 dropped --argfile).
                seedScript = ''
                  JQ=${pkgs.jq}/bin/jq
                  SYNC=${pkgs.coreutils}/bin/sync
                  DATE=${pkgs.coreutils}/bin/date

                  # jq's `*` errors on a null lhs, so one empty or truncated state file would
                  # fail every later activation and stay broken. Treat unreadable state as absent.
                  json_ok() { [ -s "$1" ] && "$JQ" -e . "$1" >/dev/null 2>&1; }

                  # `> tmp && mv` is not crash-safe under delayed allocation (xfs, ext4):
                  # a crash leaves a 0-byte file, so fsync first; chmod is explicit, mv keeps the umask.
                  commit_json() { chmod "$3" "$1" && "$SYNC" -d "$1" && mv "$1" "$2"; }

                  mkdir -p "${dataDir}"

                  SETTINGS="${dataDir}/settings.json"
                  if json_ok "$SETTINGS"; then
                    "$JQ" -n --slurpfile a "$SETTINGS" --slurpfile b "${seedSettingsFile}" '$a[0] * $b[0]' > "$SETTINGS.tmp" && commit_json "$SETTINGS.tmp" "$SETTINGS" 0644 || { rm -f "$SETTINGS.tmp"; echo "prime-agent: failed to merge $SETTINGS, left unchanged" >&2; }
                  else
                    [ -e "$SETTINGS" ] && echo "prime-agent: $SETTINGS unreadable, reseeding" >&2
                    rm -f "$SETTINGS.tmp"
                    install -m 0644 "${seedSettingsFile}" "$SETTINGS"
                  fi

                  MODELS="${dataDir}/models.json"
                  if json_ok "$MODELS"; then
                    "$JQ" -n --slurpfile a "$MODELS" --slurpfile b "${modelsFile}" '$a[0] * $b[0]' > "$MODELS.tmp" && commit_json "$MODELS.tmp" "$MODELS" 0644 || { rm -f "$MODELS.tmp"; echo "prime-agent: failed to merge $MODELS, left unchanged" >&2; }
                  else
                    [ -e "$MODELS" ] && echo "prime-agent: $MODELS unreadable, reseeding" >&2
                    rm -f "$MODELS.tmp"
                    install -m 0644 "${modelsFile}" "$MODELS"
                  fi

                  AUTH="${dataDir}/auth.json"
                  # auth.json holds real credentials, so a corrupt one is moved aside, never merged over.
                  if ! json_ok "$AUTH"; then
                    if [ -e "$AUTH" ]; then
                      mv "$AUTH" "$AUTH.corrupt.$("$DATE" +%s)"
                      echo "prime-agent: $AUTH was unreadable, moved aside; re-run /login" >&2
                    fi
                    rm -f "$AUTH.tmp"
                    # 0600 from the start; prime-agent writes it 0600 and it holds credentials.
                    printf '%s\n' '{}' > "$AUTH" && chmod 0600 "$AUTH"
                  fi
                  "$JQ" -n --slurpfile a "$AUTH" --slurpfile b "${dummyCredsFile}" '$a[0] + $b[0]' > "$AUTH.tmp" && commit_json "$AUTH.tmp" "$AUTH" 0600 || { rm -f "$AUTH.tmp"; echo "prime-agent: failed to seed MCP credentials into $AUTH" >&2; }
                  # Retract a dummy we seeded, but only if the entry is still exactly ours.
                  "$JQ" -n --slurpfile a "$AUTH" --slurpfile k "${remoteCredKeysFile}" 'reduce $k[0][] as $key ($a[0]; if .[$key] == {"type":"api_key","key":"dummy"} then del(.[$key]) else . end)' > "$AUTH.tmp" && commit_json "$AUTH.tmp" "$AUTH" 0600 || { rm -f "$AUTH.tmp"; echo "prime-agent: failed to retract seeded MCP credentials from $AUTH" >&2; }
                '';

                primeEnv = lib.mkMerge [
                  {
                    PRIME_AGENT_CODING_AGENT_DIR = dataDir;
                    PRIME_AGENT_KERNEL_VENV = "${dataDir}/kernel-venv";
                    RLM_MAX_DEPTH = toString prime-agent.settings.rlmMaxDepth;
                  }
                  (lib.mkIf prime-agent.extensions.context-language-models.enable {
                    PI_CLM_NATIVE_COMPACTION =
                      prime-agent.extensions.context-language-models.nativeCompaction or "auto";
                    PI_CLM_OVERFLOW = prime-agent.extensions.context-language-models.overflow or "withhold";
                    PI_CLM_ONE_TOOL_PER_TURN =
                      if prime-agent.extensions.context-language-models.oneToolPerTurn then "1" else "0";
                    PI_CLM_SIZE_TRAILER =
                      if prime-agent.extensions.context-language-models.sizeTrailer then "1" else "0";
                    # Bundled brief by default, so it tracks the pi-clm version.
                    PI_CLM_STEERING =
                      if prime-agent.extensions.context-language-models.steering != "" then
                        prime-agent.extensions.context-language-models.steering
                      else
                        "${clmSrc}/steering/house-brief.md";
                  })
                  (lib.mkIf
                    (
                      prime-agent.extensions.context-language-models.enable
                      && prime-agent.extensions.context-language-models.budget != ""
                    )
                    {
                      PI_CLM_BUDGET = prime-agent.extensions.context-language-models.budget;
                    }
                  )
                  (lib.mkIf
                    (
                      prime-agent.extensions.context-language-models.enable
                      && prime-agent.extensions.context-language-models.reserve != ""
                    )
                    {
                      PI_CLM_RESERVE = prime-agent.extensions.context-language-models.reserve;
                    }
                  )
                  (lib.mkIf
                    (
                      prime-agent.extensions.context-language-models.enable
                      && prime-agent.extensions.context-language-models.remindAt != ""
                    )
                    {
                      PI_CLM_REMIND_AT = prime-agent.extensions.context-language-models.remindAt;
                    }
                  )
                  (lib.mkIf
                    (
                      prime-agent.extensions.context-language-models.enable
                      && prime-agent.extensions.context-language-models.observationCap != ""
                    )
                    {
                      PI_CLM_OBSERVATION_CAP = prime-agent.extensions.context-language-models.observationCap;
                    }
                  )
                  (lib.mkIf
                    (
                      prime-agent.extensions.context-language-models.enable
                      && prime-agent.extensions.context-language-models.estimateFactor != ""
                    )
                    {
                      PI_CLM_ESTIMATE_FACTOR = prime-agent.extensions.context-language-models.estimateFactor;
                    }
                  )
                  (lib.mkIf
                    (
                      prime-agent.extensions.context-language-models.enable
                      && prime-agent.extensions.context-language-models.compactPrompt != ""
                    )
                    {
                      PI_CLM_COMPACT_PROMPT = prime-agent.extensions.context-language-models.compactPrompt;
                    }
                  )
                ];

                # Mirror requested built-in examples into the auto-load dir.
                upstreamExtDir = "${pkgs.prime-agent}/lib/prime-agent/examples/extensions";

                # Resolve each name to its file (.ts) or folder (index.ts) source; null if absent.
                builtinExtSrc =
                  name:
                  if builtins.pathExists "${upstreamExtDir}/${name}/index.ts" then
                    {
                      src = "${upstreamExtDir}/${name}";
                      target = "${relDataDir}/extensions/${name}";
                    }
                  else if builtins.pathExists "${upstreamExtDir}/${name}.ts" then
                    {
                      src = "${upstreamExtDir}/${name}.ts";
                      target = "${relDataDir}/extensions/${name}.ts";
                    }
                  else
                    null;

                extensionHomeFiles = map (name: {
                  "${(builtinExtSrc name).target}".source = (builtinExtSrc name).src;
                }) prime-agent.extensions.builtin;

                missingExtensions = lib.filter (n: builtinExtSrc n == null) prime-agent.extensions.builtin;

                extensionLocalHomeFiles = lib.mapAttrsToList (name: src: {
                  "${relDataDir}/extensions/${name}.ts".text = src;
                }) prime-agent.extensions.inline;
              in
              {
                options.icedos.managed-settings.prime-agent = icedosLib.mkAttrsOption {
                  default = seedSettings;
                };

                config = mkIf (userCfg != null) {
                  assertions = [
                    {
                      assertion = lib.hasPrefix (config.home.homeDirectory + "/") dataDir && relDataDir != "";
                      message = ''
                        icedos.ai-tools.prime-agent.settings.dataDir must expand to a
                        path inside the home directory so the MCP skill files can
                        be installed via home.file (got "${dataDir}" from
                        "${prime-agent.settings.dataDir}"). Use an in-home path, e.g.
                        "$XDG_CONFIG_HOME/prime-agent".
                      '';
                    }
                    # A pin is only validated by the user that enables the server; another
                    # user's pin is checked in that user's own sharedModule.
                    {
                      assertion = lib.all (n: !(enabled ? ${n}) || localServers ? ${n}) (
                        lib.attrNames prime-agent.settings.ports.overrides
                      );
                      message = ''
                        prime-agent ports.overrides pins a server that is enabled
                        but not a local (stdio) bridge: ${
                          builtins.concatStringsSep ", " (
                            lib.filter (n: enabled ? ${n} && !(localServers ? ${n})) (
                              lib.attrNames prime-agent.settings.ports.overrides
                            )
                          )
                        }. ports.overrides only applies to local MCP servers
                        bridged by mcp-proxy; remote (url) servers have no
                        port.
                      '';
                    }
                    {
                      assertion = duplicatePorts == [ ];
                      message = ''
                        prime-agent MCP bridge port collision: ${
                          builtins.concatStringsSep ", " (map (e: "${e.name} -> ${toString e.p}") duplicatePorts)
                        }. ports.overrides pins are honored verbatim, so two
                        servers resolving to one port must be re-pinned.
                      '';
                    }
                    {
                      assertion = missingExtensions == [ ];
                      message = ''
                        icedos.ai-tools.prime-agent.extensions.builtin references upstream
                        prime-agent example extensions that do not exist:
                        ${builtins.concatStringsSep ", " missingExtensions}
                        Each name is a folder/file under
                        <prime-agent>/lib/prime-agent/examples/extensions/.
                      '';
                    }
                    {
                      assertion =
                        prime-agent.extensions.meters.power.idleWatts >= 0
                        && prime-agent.extensions.meters.power.rateKwh >= 0;
                      message = ''
                        icedos.ai-tools.prime-agent.extensions.meters.power.idleWatts and
                        rateKwh must not be negative: a negative idle floor is
                        added to every sample rather than subtracted, and a negative
                        rate reports negative cost.
                      '';
                    }
                    {
                      assertion = builtins.all (
                        w: builtins.match "[1-9][0-9]*[smhdwM]" w != null
                      ) prime-agent.extensions.meters.power.windows;
                      message = ''
                        icedos.ai-tools.prime-agent.extensions.meters.power.windows entries
                        must be a positive integer followed by s, m, h, d, w or M (e.g. "1s",
                        "15m", "24h", "1M"), got: ${builtins.concatStringsSep ", " prime-agent.extensions.meters.power.windows}
                      '';
                    }
                    {
                      # No sensible default price or currency exists, so the user must set both.
                      assertion =
                        !prime-agent.extensions.meters.power.enable
                        || (
                          prime-agent.extensions.meters.power.rateKwh > 0
                          && prime-agent.extensions.meters.power.currency != ""
                        );
                      message = ''
                        icedos.ai-tools.prime-agent.extensions.meters.power is enabled
                        but rateKwh (${toString prime-agent.extensions.meters.power.rateKwh}) or
                        currency ("${prime-agent.extensions.meters.power.currency}") is unset.
                        Set rateKwh to your price per kWh (> 0) and currency to its symbol.
                      '';
                    }
                    {
                      # Substituted into a JS string literal, so it must be a plain id.
                      assertion =
                        prime-agent.extensions.prompt-suggestions.model == ""
                        ||
                          builtins.match "[A-Za-z0-9._-]+/[A-Za-z0-9._:/@-]+" prime-agent.extensions.prompt-suggestions.model
                          != null;
                      message = ''
                        icedos.ai-tools.prime-agent.extensions.prompt-suggestions.model must be
                        "" or "provider/model-id" (got "${prime-agent.extensions.prompt-suggestions.model}").
                      '';
                    }
                    {
                      # The meter renders inside the cost footer, so it has
                      # nowhere to appear without it.
                      assertion = prime-agent.extensions.meters.cost || !prime-agent.extensions.meters.power.enable;
                      message = ''
                        icedos.ai-tools.prime-agent.extensions.meters.power.enable needs
                        meters.cost = true: the GPU electricity meter is rendered
                        as part of the cost footer.
                      '';
                    }
                    {
                      # Both load a "cost-footer" extension and would double-render.
                      assertion = !prime-agent.extensions.meters.cost || !(prime-agent.extensions.inline ? "cost-footer");
                      message = ''
                        icedos.ai-tools.prime-agent.extensions.inline already declares
                        "cost-footer"; the built-in cost footer ships the same
                        extension. Remove the inline declaration or set
                        meters.cost = false.
                      '';
                    }
                    {
                      # registerProvider replaces the whole stored request config, so
                      # zen-session.ts drops any apiKey or headers models.json set for opencode.
                      assertion =
                        let
                          oc = prime-agent.settings.providers.opencode or { };
                        in
                        (oc.apiKey or null) == null && (oc.headers or { }) == { };
                      message = ''
                        icedos.ai-tools.prime-agent.settings.providers.opencode sets apiKey or
                        headers, which the zen-session extension drops when it refreshes the
                        session header. Keep the key in prime-agent's auth store, and set
                        extra opencode headers from an extension instead.
                      '';
                    }
                    {
                      assertion = lib.all (n: builtins.match "[A-Za-z0-9._-]+" n != null) (
                        lib.attrNames prime-agent.extensions.inline
                      );
                      message = ''
                        icedos.ai-tools.prime-agent.extensions.inline names must be
                        simple: ${
                          builtins.concatStringsSep ", " (
                            lib.filter (n: builtins.match "[A-Za-z0-9._-]+" n == null) (
                              lib.attrNames prime-agent.extensions.inline
                            )
                          )
                        }
                      '';
                    }
                  ];

                  # Login shells read home.sessionVariables; the graphical session needs systemd.user.
                  home.sessionVariables = primeEnv;
                  systemd.user.sessionVariables = primeEnv;

                  home.file = mkMerge (
                    (lib.mapAttrsToList skillFiles enabled)
                    ++ extensionHomeFiles
                    ++ extensionLocalHomeFiles
                    ++ [
                      # No shell hooks; extensions auto-load from <agentDir>/extensions/*.ts.
                      (mkIf (peonPingEnabled && prime-agent.extensions.peonPing) {
                        "${relDataDir}/extensions/peon-ping.ts".source = pkgs.replaceVars ./extensions/peon-ping.ts {
                          # Installed by peon-ping's hm module; bin/peon carries its own PATH.
                          peonSh = "${config.home.homeDirectory}/.openpeon/peon.sh";
                        };
                      })

                      # Live session cost (USD, or € for metered local models) in the TUI bottom bar.
                      (mkIf prime-agent.extensions.meters.cost {
                        "${relDataDir}/extensions/cost-footer".source = costFooterSrc;
                      })

                      # Mints a fresh zen session id per pi session; models.json cannot.
                      {
                        "${relDataDir}/extensions/zen-session.ts".source = zenSessionSrc;
                      }

                      # Caps model context windows at extensions.context-window-cap.percent of native.
                      (mkIf prime-agent.extensions.context-window-cap.enable {
                        "${relDataDir}/extensions/context-cap.ts".source = contextCapSrc;
                      })

                      # The model curates its own context in a mirrored file (pi-clm).
                      (mkIf prime-agent.extensions.context-language-models.enable {
                        "${relDataDir}/extensions/pi-clm".source = clmSrc;
                      })

                      # Predicted next prompt as ghost text in the empty editor; Tab accepts.
                      (mkIf prime-agent.extensions.prompt-suggestions.enable {
                        "${relDataDir}/extensions/prompt-suggest.ts".source =
                          pkgs.replaceVars ./extensions/prompt-suggest.ts
                            {
                              suggestModel = prime-agent.extensions.prompt-suggestions.model;
                            };
                      })
                    ]
                  );

                  systemd.user.services = mkMerge (lib.mapAttrsToList service localServers);

                  home.activation.seed-prime-agent = lib.hm.dag.entryAfter [
                    "writeBoundary"
                  ] seedScript;
                };
              }
            )
          ];

          icedos.system.tips.list =
            lib.optionals prime-agent.desktopEntry [
              "prime-agent is in the app launcher; it opens in your default terminal."
            ]
            ++ lib.optionals prime-agent.extensions.meters.cost [
              "prime-agent shows what the session has cost so far; /cost hides the line."
            ]
            ++ lib.optionals prime-agent.extensions.meters.power.enable [
              "Models running on your own graphics card are priced by the electricity they use."
            ]
            ++ lib.optionals prime-agent.settings.speed [
              "The TUI footer shows output tok/s per response and a session average; /speed on/off toggles it."
            ]
            ++ lib.optionals prime-agent.includeInIcedosGc [
              "icedos gc also clears out old prime-agent sessions and logs."
            ]
            ++ lib.optionals prime-agent.providers.ollama [
              "prime-agent supports Ollama Cloud; type /login in its tui and pick Ollama to connect using an API key."
            ];
        }
      )

    ];

  meta = {
    name = "prime-agent";
    dependencies = [ { modules = [ "agent-launch" ]; } ];
  };
}
