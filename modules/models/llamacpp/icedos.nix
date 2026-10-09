{ icedosLib, lib, ... }:

{
  options.icedos.ai-tools.llamacpp =
    let
      inherit (lib) importTOML types;

      inherit (icedosLib)
        mkAttrsOfOption
        mkBoolOption
        mkEitherOption
        mkEnumOption
        mkFloatBetweenOption
        mkIntBetweenOption
        mkStrOption
        mkSubmoduleListOption
        ;

      defaults = (importTOML ./config.toml).icedos.ai-tools.llamacpp;
      inherit (defaults) lifecycle settings;
    in
    {
      # Applied to the nixpkgs llama.cpp with fetchpatch, so any entry rebuilds it locally.
      patches = mkSubmoduleListOption { default = defaults.patches; } {
        url = mkStrOption { };
        hash = mkStrOption { };
      };

      # An allowlist, since a raised nice ceiling can starve interactive work. A user-domain
      # pam_limits line outranks group grants, so a listed user loses e.g. @pipewire's -19.
      priorityUsers = mkEitherOption { default = defaults.priorityUsers; } (
        (types.addCheck types.str (v: v == "all"))
        // {
          description = ''the literal "all"'';
        }
      ) (types.listOf types.str);

      service = mkBoolOption { default = defaults.service; };

      lifecycle = {
        # Owned here, not by prime-agent, so its conflict with sleepIdleSeconds is asserted in one module.
        enable = mkBoolOption { default = lifecycle.enable; };

        idle = mkIntBetweenOption {
          path = "icedos.ai-tools.llamacpp.lifecycle.idle";
          source = ./config.toml;
          default = lifecycle.idle;
        } 0 86400;
      };

      settings = {
        flashAttn = mkBoolOption { default = settings.flashAttn; };

        # Sentinels, not counts: -1 is llama.cpp's "auto" and -2 is "all".
        gpuLayers = mkIntBetweenOption {
          path = "icedos.ai-tools.llamacpp.settings.gpuLayers";
          source = ./config.toml;
          default = settings.gpuLayers;
        } (-2) 1024;

        # -1 means every core.
        threads = mkIntBetweenOption {
          path = "icedos.ai-tools.llamacpp.settings.threads";
          source = ./config.toml;
          default = settings.threads;
        } (-1) 4096;

        batch = {
          size = mkIntBetweenOption {
            path = "icedos.ai-tools.llamacpp.settings.batch.size";
            source = ./config.toml;
            default = settings.batch.size;
          } 1 1048576;

          ubatchSize = mkIntBetweenOption {
            path = "icedos.ai-tools.llamacpp.settings.batch.ubatchSize";
            source = ./config.toml;
            default = settings.batch.ubatchSize;
          } 1 1048576;
        };

        cache = {
          typeK = mkStrOption { default = settings.cache.typeK; };
          typeV = mkStrOption { default = settings.cache.typeV; };
        };

        mmproj = {
          offload = mkBoolOption { default = settings.mmproj.offload; };
          path = mkStrOption { default = settings.mmproj.path; };
        };

        model = {
          # Floor of 1: llama.cpp reads 0 as the model's trained context, which clients
          # would not see, since contextSize is also their context window.
          contextSize = mkIntBetweenOption {
            path = "icedos.ai-tools.llamacpp.settings.model.contextSize";
            source = ./config.toml;
            default = settings.model.contextSize;
          } 1 4194304;

          maxTokens = mkIntBetweenOption {
            path = "icedos.ai-tools.llamacpp.settings.model.maxTokens";
            source = ./config.toml;
            default = settings.model.maxTokens;
          } 1 1048576;

          name = mkStrOption { default = settings.model.name; };
          path = mkStrOption { default = settings.model.path; };
          reasoning = mkBoolOption { default = settings.model.reasoning; };

          # See prime-agent's thinkingLevelMap for the accepted keys.
          thinking-level-map = mkAttrsOfOption {
            default = settings.model.thinking-level-map;
          } (types.nullOr types.str);
        };

        # arg.cpp's own ranges, which differ: --prio accepts low (-1), --prio-batch starts at normal (0).
        priority = {
          process = mkIntBetweenOption {
            path = "icedos.ai-tools.llamacpp.settings.priority.process";
            source = ./config.toml;
            default = settings.priority.process;
          } (-1) 3;

          batch = mkIntBetweenOption {
            path = "icedos.ai-tools.llamacpp.settings.priority.batch";
            source = ./config.toml;
            default = settings.priority.batch;
          } 0 3;
        };

        reasoning = {
          # Bounded here, not by an assertion: the serve script divides by it before
          # assertions run, so 0 would abort with a bare "division by zero".
          budgetDivider = mkFloatBetweenOption {
            path = "icedos.ai-tools.llamacpp.settings.reasoning.budgetDivider";
            source = ./config.toml;
            default = settings.reasoning.budgetDivider;
          } 0.001 1024;

          preserve = mkBoolOption { default = settings.reasoning.preserve; };
        };

        server = {
          host = mkStrOption { default = settings.server.host; };

          port = mkIntBetweenOption {
            path = "icedos.ai-tools.llamacpp.settings.server.port";
            source = ./config.toml;
            default = settings.server.port;
          } 1 65535;

          # 0 omits --parallel (auto slots, unified KV cache); a count splits contextSize across slots.
          parallel = mkIntBetweenOption {
            path = "icedos.ai-tools.llamacpp.settings.server.parallel";
            source = ./config.toml;
            default = settings.server.parallel;
          } 0 256;

          # Int, not number: arg.cpp uses std::stoi, which truncates 2.5 to 2 and rejects 0.5.
          sleepIdleSeconds = mkIntBetweenOption {
            path = "icedos.ai-tools.llamacpp.settings.server.sleepIdleSeconds";
            source = ./config.toml;
            default = settings.server.sleepIdleSeconds;
          } (-1) 86400;
        };

        spec = {
          type = mkStrOption { default = settings.spec.type; };

          draft = {
            nMax = mkIntBetweenOption {
              path = "icedos.ai-tools.llamacpp.settings.spec.draft.nMax";
              source = ./config.toml;
              default = settings.spec.draft.nMax;
            } 0 1024;

            pMin = mkFloatBetweenOption {
              path = "icedos.ai-tools.llamacpp.settings.spec.draft.pMin";
              source = ./config.toml;
              default = settings.spec.draft.pMin;
            } 0.0 1.0;

            # Upstream llama.cpp has no --spec-draft-sampling: without #27694 the server exits on the flag.
            sampling =
              mkEnumOption
                {
                  path = "icedos.ai-tools.llamacpp.settings.spec.draft.sampling";
                  source = ./config.toml;
                  default = settings.spec.draft.sampling;
                }
                [
                  ""
                  "greedy"
                  "probabilistic"
                ];
          };
        };

        vulkan = {
          # Keeps tensors out of the small ReBAR window, which cuts token generation ~3x on cards like Navi21.
          # With Resizable BAR enabled the window is the whole of VRAM, and this costs ~2% instead.
          disableHostVisibleVidmem = mkBoolOption { default = settings.vulkan.disableHostVisibleVidmem; };

          # RADV-only: appends nogttspill to RADV_PERFTEST so allocations stay out of system RAM under VRAM pressure.
          radvNoGttSpill = mkBoolOption { default = settings.vulkan.radvNoGttSpill; };
        };
      };

      # Skills for this module (name = SKILL.md), loaded by every agent via icedos.ai-tools.skills.
      skills = mkAttrsOfOption { default = defaults.skills; } lib.types.lines;
    };

  outputs.nixosModules =
    { repoUrl, ... }:
    [
      (
        {
          config,
          pkgs,
          lib,
          ...
        }:

        let
          inherit (lib)
            attrNames
            concatMap
            elem
            filterAttrs
            mkIf
            optionals
            ;
          inherit (config.icedos) users;

          cfg = config.icedos.ai-tools.llamacpp;
          inherit (cfg) patches priorityUsers service;
          inherit (cfg.settings) flashAttn gpuLayers threads;
          inherit (cfg.settings.server)
            host
            parallel
            port
            sleepIdleSeconds
            ;

          batchSize = cfg.settings.batch.size;
          ubatchSize = cfg.settings.batch.ubatchSize;
          cacheTypeK = cfg.settings.cache.typeK;
          cacheTypeV = cfg.settings.cache.typeV;
          contextSize = cfg.settings.model.contextSize;
          mmproj = cfg.settings.mmproj.path;
          mmprojOffload = cfg.settings.mmproj.offload;
          model = cfg.settings.model.path;
          prio = cfg.settings.priority.process;
          prioBatch = cfg.settings.priority.batch;
          reasoningBudgetDivider = cfg.settings.reasoning.budgetDivider;
          reasoningPreserve = cfg.settings.reasoning.preserve;
          specType = cfg.settings.spec.type;
          specDraftNMax = cfg.settings.spec.draft.nMax;
          specDraftPMin = cfg.settings.spec.draft.pMin;
          specDraftSampling = cfg.settings.spec.draft.sampling;
          vkDisableHostVisibleVidmem = cfg.settings.vulkan.disableHostVisibleVidmem;
          radvNoGttSpill = cfg.settings.vulkan.radvNoGttSpill;

          lifecycle = cfg.lifecycle.enable;
          lifecycleIdleSeconds = cfg.lifecycle.idle;

          # Not configurable: opencode and prime-agent's context-cap match this exact name.
          provider = "llamacpp";

          # The GGUF file name, minus a split model's -00001-of-00003 suffix.
          modelId =
            let
              stem = lib.removeSuffix ".gguf" (baseNameOf model);
              shard = lib.match "(.*)-[0-9]{5}-of-[0-9]{5}" stem;
            in
            if shard == null then stem else lib.head shard;
          modelName = cfg.settings.model.name;
          modelReasoning = cfg.settings.model.reasoning;
          modelMaxTokens = cfg.settings.model.maxTokens;
          modelThinkingLevelMap = cfg.settings.model.thinking-level-map;

          llamaCpp =
            if patches == [ ] then
              pkgs.llama-cpp-vulkan
            else
              pkgs.llama-cpp-vulkan.overrideAttrs (old: {
                patches = (old.patches or [ ]) ++ map (p: pkgs.fetchpatch { inherit (p) url hash; }) patches;
              });

          # What a client dials: a wildcard bind is not an address, and IPv6 needs brackets in a URL.
          clientHost =
            if host == "0.0.0.0" then
              "127.0.0.1"
            else if host == "::" || host == "[::]" then
              "[::1]"
            else if lib.hasInfix ":" host && !(lib.hasPrefix "[" host) then
              "[${host}]"
            else
              host;

          hasPrimeAgent = icedosLib.hasModule {
            inherit config repoUrl;
            name = "prime-agent";
          };

          # Any before_provider_request handler disables prime-agent's Idempotency-Key reuse,
          # so an extension that can never start a server would make every auto-retry billable.
          lifecycleEnabled = lifecycle && hasPrimeAgent && model != "" && modelId != "";

          # Markers sit inside TypeScript string literals; escapeShellArg only protects the builder,
          # so values also need JS escaping.
          jsStr =
            v: lib.escapeShellArg (lib.replaceStrings [ "\\" "\"" "\n" "\r" ] [ "\\\\" "\\\"" "\\n" "\\r" ] v);

          lifecycleSrc = pkgs.runCommand "prime-agent-llamacpp-lifecycle.ts" { } ''
            cp ${./extensions/llamacpp-lifecycle.ts} $out
            chmod +w $out
            substituteInPlace $out \
              --replace-fail "@llamacppUrl@" ${jsStr "http://${clientHost}:${toString port}"} \
              --replace-fail "@llamacppIdleSeconds@" ${jsStr (toString lifecycleIdleSeconds)}

            if ${pkgs.gnugrep}/bin/grep -qE '@[a-zA-Z_][0-9A-Za-z_-]*@' $out; then
              echo "unsubstituted placeholder left in llamacpp-lifecycle.ts" >&2
              exit 1
            fi
          '';

          llamaServer = pkgs.writeShellScript "llamacpp-serve" ''
            ${lib.optionalString vkDisableHostVisibleVidmem "export GGML_VK_DISABLE_HOST_VISIBLE_VIDMEM=1"}
            ${lib.optionalString radvNoGttSpill "export RADV_PERFTEST=\"\${RADV_PERFTEST:+$RADV_PERFTEST,}nogttspill\""}
            RUNTIME="''${XDG_RUNTIME_DIR:?XDG_RUNTIME_DIR unset}"
            ${pkgs.coreutils}/bin/mkdir -p "$RUNTIME/icedos"

            # Atomic O_EXCL claim: racing callers both pass serve's guard. Nothing removes the
            # pidfile on exit, so the wrapper must also recognise stale ones.
            PIDFILE="$RUNTIME/icedos/llamacpp.pid"
            claim() { (set -C; echo "$$" >"$PIDFILE") 2>/dev/null; }

            if ! claim; then
              OWNER=$(${pkgs.coreutils}/bin/cat "$PIDFILE" 2>/dev/null || true)
              if [[ "$OWNER" =~ ^[1-9][0-9]*$ ]] &&
                 kill -0 "$OWNER" 2>/dev/null &&
                 ${pkgs.gnugrep}/bin/grep -qa 'llama-server\|llamacpp-serve' \
                   "/proc/$OWNER/cmdline" 2>/dev/null; then
                echo "llamacpp is already running (PID $OWNER)" >&2
                exit 1
              fi

              # Stale pidfile. mkdir is atomic, so only one wrapper removes it; a bare rm
              # could delete a claim another wrapper just won.
              if ${pkgs.coreutils}/bin/mkdir "$PIDFILE.recover" 2>/dev/null; then
                ${pkgs.coreutils}/bin/rm -f "$PIDFILE"
                claim || true
                ${pkgs.coreutils}/bin/rmdir "$PIDFILE.recover" 2>/dev/null || true
              else
                # Another wrapper is recovering.
                ${pkgs.coreutils}/bin/sleep 0.3
              fi

              if [[ "$(${pkgs.coreutils}/bin/cat "$PIDFILE" 2>/dev/null)" != "$$" ]]; then
                echo "llamacpp is already starting" >&2
                exit 1
              fi
            fi

            # Wait for dmemcg-booster to enable dmem on this scope.
            DMEM_DEVICE=$(${pkgs.gawk}/bin/awk '{print $1}' /sys/fs/cgroup/dmem.capacity 2>/dev/null) || true
            if [ -n "$DMEM_DEVICE" ]; then
              CGROUP_PATH=$(${pkgs.gnused}/bin/sed 's/0:://' /proc/$$/cgroup 2>/dev/null) || true
              dmem_path="/sys/fs/cgroup/''${CGROUP_PATH}"
              for _ in $(${pkgs.coreutils}/bin/seq 1 10); do
                [ -f "$dmem_path/dmem.low" ] && break
                ${pkgs.coreutils}/bin/sleep 1
              done
              if [ -f "$dmem_path/dmem.low" ]; then
                echo "''${DMEM_DEVICE} max" > "$dmem_path/dmem.low" 2>/dev/null || true
                ( while ${pkgs.coreutils}/bin/sleep 5; do
                    kill -0 "$$" 2>/dev/null || exit 0
                    CURRENT=$(${pkgs.gawk}/bin/awk -v dev="''${DMEM_DEVICE}" '$1==dev {print $2}' "$dmem_path/dmem.current" 2>/dev/null)
                    [ -n "$CURRENT" ] && echo "''${DMEM_DEVICE} $CURRENT" > "$dmem_path/dmem.min" 2>/dev/null
                  done ) &
              fi
            fi

            exec ${pkgs.util-linux}/bin/chrt --other 0 ${llamaCpp}/bin/llama-server \
              -m ${lib.escapeShellArg model} \
              ${lib.optionalString (modelId != "") "--alias ${lib.escapeShellArg modelId}"} \
              --host ${lib.escapeShellArg host} \
              --port ${toString port} \
              -ngl ${toString gpuLayers} \
              -t ${toString threads} \
              -b ${toString batchSize} \
              -ub ${toString ubatchSize} \
              ${lib.optionalString (parallel > 0) "--parallel ${toString parallel}"} \
              --prio ${toString prio} \
              --prio-batch ${toString prioBatch} \
              --ctx-size ${toString contextSize} \
              --reasoning-budget ${
                toString (lib.max 1 (builtins.floor (contextSize / reasoningBudgetDivider)))
              } \
              ${lib.optionalString (cacheTypeK != "") "--cache-type-k ${lib.escapeShellArg cacheTypeK}"} \
              ${lib.optionalString (cacheTypeV != "") "--cache-type-v ${lib.escapeShellArg cacheTypeV}"} \
              ${lib.optionalString (specType != "") "--spec-type ${lib.escapeShellArg specType}"} \
              ${lib.optionalString (specDraftNMax > 0) "--spec-draft-n-max ${toString specDraftNMax}"} \
              ${lib.optionalString (specDraftPMin > 0) "--spec-draft-p-min ${toString specDraftPMin}"} \
              ${
                lib.optionalString (
                  specDraftSampling != ""
                ) "--spec-draft-sampling ${lib.escapeShellArg specDraftSampling}"
              } \
              --flash-attn ${if flashAttn then "on" else "off"} \
              ${lib.optionalString (sleepIdleSeconds > 0) "--sleep-idle-seconds ${toString sleepIdleSeconds}"} \
              ${lib.optionalString reasoningPreserve "--reasoning-preserve"} \
              ${
                lib.optionalString (mmproj != "") "--mmproj ${lib.escapeShellArg mmproj} --image-min-tokens 1024"
              } \
              ${lib.optionalString (mmproj != "" && !mmprojOffload) "--no-mmproj-offload"} \
              "$@"
          '';
        in
        {
          environment.systemPackages = [
            llamaCpp
          ];

          icedos.system.toolset.commands = [
            {
              command = "llamacpp";
              help = "print llamacpp related commands";

              commands = [
                {
                  command = "serve";
                  help = "Run configured model with optional --flags";

                  script = ''
                    ${icedosLib.bash.mkFlags {
                      prefix = "LLAMACPP";
                      passthroughUnknown = true;
                      flags = [
                        {
                          name = "host";
                          short = "H";
                          type = "string";
                          default = host;
                          description = "Listen address";
                        }
                        {
                          name = "port";
                          short = "p";
                          type = "int";
                          default = port;
                          description = "Listen port";
                        }
                        {
                          name = "model";
                          short = "m";
                          type = "string";
                          default = model;
                          description = "Model path";
                        }
                        {
                          name = "gpu-layers";
                          short = "ngl";
                          type = "int";
                          default = gpuLayers;
                          description = "GPU layers";
                        }
                        {
                          name = "threads";
                          short = "t";
                          type = "int";
                          default = threads;
                          description = "CPU threads";
                        }
                        {
                          name = "ctx-size";
                          short = "c";
                          type = "int";
                          default = contextSize;
                          description = "Context size";
                        }
                        {
                          name = "batch-size";
                          short = "b";
                          type = "int";
                          default = batchSize;
                          description = "Batch size";
                        }
                        {
                          name = "ubatch-size";
                          type = "int";
                          default = ubatchSize;
                          description = "Microbatch size";
                        }
                        {
                          name = "parallel";
                          short = "np";
                          type = "int";
                          default = parallel;
                          description = "Server slots (0 = llama.cpp auto)";
                        }
                        {
                          name = "cache-type-k";
                          short = "ctk";
                          type = "string";
                          default = cacheTypeK;
                          description = "KV cache type for K";
                        }
                        {
                          name = "cache-type-v";
                          short = "ctv";
                          type = "string";
                          default = cacheTypeV;
                          description = "KV cache type for V";
                        }
                        {
                          name = "spec-type";
                          type = "string";
                          default = specType;
                          description = "Speculative decoding types";
                        }
                        {
                          name = "spec-draft-n-max";
                          type = "int";
                          default = specDraftNMax;
                          description = "Draft tokens per speculative step (0 = llama.cpp default)";
                        }
                        {
                          # string: the flag parser has no float type, llama.cpp validates the value
                          name = "spec-draft-p-min";
                          type = "string";
                          default = toString specDraftPMin;
                          description = "Minimum draft probability (0 = llama.cpp default)";
                        }
                        {
                          name = "spec-draft-sampling";
                          type = "string";
                          default = specDraftSampling;
                          description = "Draft verification: greedy or probabilistic";
                        }
                        {
                          name = "mmproj";
                          type = "string";
                          default = mmproj;
                          description = "Multimodal projector path";
                        }
                        {
                          name = "prio";
                          type = "int";
                          default = prio;
                          description = "Process/thread priority level (-1 to 3)";
                        }
                        {
                          name = "prio-batch";
                          type = "int";
                          default = prioBatch;
                          description = "Batch thread priority level (0-3)";
                        }
                        {
                          name = "flash-attn";
                          type = "enum";
                          default = if flashAttn then "on" else "off";
                          description = "Flash attention";
                          choices = [
                            "on"
                            "off"
                            "auto"
                          ];
                        }
                        {
                          name = "reasoning-budget";
                          type = "int";
                          default = lib.max 1 (builtins.floor (contextSize / reasoningBudgetDivider));
                          description = "Reasoning budget tokens";
                        }
                        {
                          name = "reasoning-preserve";
                          type = "bool";
                          default = reasoningPreserve;
                          description = "Preserve reasoning trace";
                        }
                        {
                          name = "detached";
                          short = "d";
                          type = "bool";
                          default = false;
                          description = "Run in background";
                        }
                      ];
                    }}

                    # Courtesy check for a readable error only. It never removes a stale pidfile:
                    # that races the wrapper, whose O_EXCL claim is the sole authority.
                    PIDFILE="''${XDG_RUNTIME_DIR:?XDG_RUNTIME_DIR unset}/icedos/llamacpp.pid"
                    if [[ -f "$PIDFILE" ]]; then
                      PID=$(${pkgs.coreutils}/bin/cat "$PIDFILE" 2>/dev/null || true)
                      if [[ "$PID" =~ ^[1-9][0-9]*$ ]] &&
                         kill -0 "$PID" 2>/dev/null &&
                         ${pkgs.gnugrep}/bin/grep -qa 'llama-server\|llamacpp-serve' \
                           "/proc/$PID/cmdline" 2>/dev/null; then
                        die "llamacpp is already running (PID $PID)"
                      fi
                    fi

                    ARGS=()
                    if [[ "$LLAMACPP_HOST_SET" == "1" ]]; then ARGS+=(--host "$LLAMACPP_HOST"); fi
                    if [[ "$LLAMACPP_PORT_SET" == "1" ]]; then ARGS+=(--port "$LLAMACPP_PORT"); fi
                    if [[ "$LLAMACPP_MODEL_SET" == "1" ]]; then ARGS+=(--model "$LLAMACPP_MODEL"); fi
                    if [[ "$LLAMACPP_GPU_LAYERS_SET" == "1" ]]; then ARGS+=(--gpu-layers "$LLAMACPP_GPU_LAYERS"); fi
                    if [[ "$LLAMACPP_THREADS_SET" == "1" ]]; then ARGS+=(--threads "$LLAMACPP_THREADS"); fi
                    if [[ "$LLAMACPP_CTX_SIZE_SET" == "1" ]]; then ARGS+=(--ctx-size "$LLAMACPP_CTX_SIZE"); fi
                    if [[ "$LLAMACPP_BATCH_SIZE_SET" == "1" ]]; then ARGS+=(--batch-size "$LLAMACPP_BATCH_SIZE"); fi
                    if [[ "$LLAMACPP_UBATCH_SIZE_SET" == "1" ]]; then ARGS+=(--ubatch-size "$LLAMACPP_UBATCH_SIZE"); fi
                    if [[ "$LLAMACPP_PARALLEL_SET" == "1" && "$LLAMACPP_PARALLEL" != "0" ]]; then ARGS+=(--parallel "$LLAMACPP_PARALLEL"); fi
                    if [[ "$LLAMACPP_CACHE_TYPE_K_SET" == "1" ]]; then ARGS+=(--cache-type-k "$LLAMACPP_CACHE_TYPE_K"); fi
                    if [[ "$LLAMACPP_CACHE_TYPE_V_SET" == "1" ]]; then ARGS+=(--cache-type-v "$LLAMACPP_CACHE_TYPE_V"); fi
                    if [[ "$LLAMACPP_SPEC_TYPE_SET" == "1" ]]; then ARGS+=(--spec-type "$LLAMACPP_SPEC_TYPE"); fi
                    if [[ "$LLAMACPP_SPEC_DRAFT_N_MAX_SET" == "1" ]]; then ARGS+=(--spec-draft-n-max "$LLAMACPP_SPEC_DRAFT_N_MAX"); fi
                    if [[ "$LLAMACPP_SPEC_DRAFT_P_MIN_SET" == "1" ]]; then ARGS+=(--spec-draft-p-min "$LLAMACPP_SPEC_DRAFT_P_MIN"); fi
                    if [[ "$LLAMACPP_SPEC_DRAFT_SAMPLING_SET" == "1" ]]; then ARGS+=(--spec-draft-sampling "$LLAMACPP_SPEC_DRAFT_SAMPLING"); fi
                    if [[ "$LLAMACPP_MMPROJ_SET" == "1" ]]; then ARGS+=(--mmproj "$LLAMACPP_MMPROJ" --image-min-tokens 1024); fi
                    if [[ "$LLAMACPP_PRIO_SET" == "1" ]]; then ARGS+=(--prio "$LLAMACPP_PRIO"); fi
                    if [[ "$LLAMACPP_PRIO_BATCH_SET" == "1" ]]; then ARGS+=(--prio-batch "$LLAMACPP_PRIO_BATCH"); fi
                    if [[ "$LLAMACPP_FLASH_ATTN_SET" == "1" ]]; then ARGS+=(--flash-attn "$LLAMACPP_FLASH_ATTN"); fi
                    if [[ "$LLAMACPP_REASONING_BUDGET_SET" == "1" ]]; then ARGS+=(--reasoning-budget "$LLAMACPP_REASONING_BUDGET"); fi
                    if [[ "$LLAMACPP_REASONING_PRESERVE_SET" == "1" ]]; then
                      if [[ "$LLAMACPP_REASONING_PRESERVE" == "true" ]]; then
                        ARGS+=(--reasoning-preserve)
                      else
                        ARGS+=(--no-reasoning-preserve)
                      fi
                    fi
                    ARGS+=("$@")

                    if [[ "$LLAMACPP_DETACHED" == "true" ]]; then
                      # RUNTIME belongs to the wrapper, and the redirect runs before it can mkdir.
                      ${pkgs.coreutils}/bin/mkdir -p "$XDG_RUNTIME_DIR/icedos"
                      nohup systemd-run --user --scope ${llamaServer} "''${ARGS[@]}" >"$XDG_RUNTIME_DIR/icedos/llamacpp.log" 2>&1 &
                    else
                      exec systemd-run --user --scope ${llamaServer} "''${ARGS[@]}"
                    fi
                  '';
                }

                {
                  command = "stop";
                  help = "Stop the running server and free its VRAM";

                  script = ''
                    SLEEP=${pkgs.coreutils}/bin/sleep
                    SYSTEMCTL=${pkgs.systemd}/bin/systemctl
                    GREP=${pkgs.gnugrep}/bin/grep
                    CAT=${pkgs.coreutils}/bin/cat
                    RM=${pkgs.coreutils}/bin/rm

                    # is_help_flag is true for "", so only consult it once an argument was given.
                    if [[ $# -gt 0 ]]; then
                      if [[ -n "$1" ]] && is_help_flag "$1"; then
                        echo "Usage: icedos llamacpp stop"
                        exit 0
                      fi
                      die "stop takes no arguments"
                    fi

                    PIDFILE="''${XDG_RUNTIME_DIR:?XDG_RUNTIME_DIR unset}/icedos/llamacpp.pid"

                    # Before reporting "not running", check nothing still serves; a server
                    # with a lost pidfile would otherwise keep its ~13 GiB unnoticed.
                    orphaned() {
                      ${pkgs.curl}/bin/curl -sf -m 2 -o /dev/null \
                        http://${clientHost}:${toString port}/health
                    }

                    # Stop through systemd, or Restart= revives the server. Match live states positively:
                    # failed, inactive and a systemctl error must fall through to the pidfile path.
                    STATE=$("$SYSTEMCTL" --user show -P ActiveState llamacpp 2>/dev/null) || STATE=""
                    case "$STATE" in
                      active | activating | reloading | refreshing | deactivating | maintenance)
                        "$SYSTEMCTL" --user stop llamacpp || die "failed to stop the llamacpp user unit"
                        # Nothing else removes the unit's pidfile, and a leftover one confuses a later hand-run serve.
                        "$RM" -f "$PIDFILE"
                        echo "llamacpp stopped (systemd unit)"
                        exit 0
                        ;;
                    esac

                    if [[ ! -f "$PIDFILE" ]]; then
                      if orphaned; then
                        die "a llamacpp server is serving ${clientHost}:${toString port} but no pidfile identifies it; stop it by hand"
                      fi
                      echo "llamacpp is not running"
                      exit 0
                    fi

                    PID=$("$CAT" "$PIDFILE" 2>/dev/null || true)
                    # Unvalidated, `kill -TERM -1` would signal every process the user owns, then KILL them.
                    if [[ ! "$PID" =~ ^[1-9][0-9]*$ ]]; then
                      "$RM" -f "$PIDFILE"
                      if orphaned; then
                        die "a llamacpp server is serving ${host}:${toString port} but no pidfile identifies it; stop it by hand"
                      fi
                      echo "llamacpp is not running (invalid pidfile removed)"
                      exit 0
                    fi

                    # Whether the pid is still our server, since a stale pid can be recycled. cmdline, not comm:
                    # comm is the wrapper's truncated name until exec, and cmdline is empty for zombies.
                    identity() {
                      kill -0 "$PID" 2>/dev/null &&
                        "$GREP" -qa 'llama-server\|llamacpp-serve' "/proc/$PID/cmdline" 2>/dev/null
                    }

                    # /proc/PID lasts until reaping, past exit_files() where VRAM is freed. cmdline empties
                    # earlier, at exit_mm(), which would let the next serve race a half-freed GPU context.
                    running() {
                      [[ -e "/proc/$PID" ]] &&
                        [[ "$("$CAT" "/proc/$PID/stat" 2>/dev/null)" != *') Z '* ]]
                    }

                    if ! identity; then
                      "$RM" -f "$PIDFILE"
                      if orphaned; then
                        die "a llamacpp server is serving ${host}:${toString port} but no pidfile identifies it; stop it by hand"
                      fi
                      echo "llamacpp is not running (stale pidfile removed)"
                      exit 0
                    fi

                    kill -TERM "$PID" 2>/dev/null || true

                    # Unmapping took 32.5 s with a request in flight. ~95 s budget (each turn is just
                    # over 0.1 s), sized against systemd's 90 s DefaultTimeoutStopSec.
                    i=0
                    while ((i++ < 900)); do
                      running || break
                      "$SLEEP" 0.1
                    done

                    if running; then
                      kill -KILL "$PID" 2>/dev/null || true
                      i=0
                      while ((i++ < 50)); do
                        running || break
                        "$SLEEP" 0.1
                      done
                    fi

                    # Keep the pidfile of a survivor, or serve's guard would start a second server.
                    if running; then
                      die "llamacpp (PID $PID) did not exit; VRAM is still held"
                    fi

                    "$RM" -f "$PIDFILE"
                    echo "llamacpp stopped (PID $PID)"
                  '';
                }
              ];
            }
          ];

          # optionalAttrs, not mkIf: mkIf still resolves the option path, which fails without prime-agent.
          icedos.ai-tools = {
            # Skills for this module, added to the shared icedos.ai-tools.skills pool.
            skills = lib.mapAttrs (_: lib.mkDefault) config.icedos.ai-tools.llamacpp.skills;
          }
          // lib.optionalAttrs hasPrimeAgent {
            # Local GPU, so metered by power instead of per token. listOf merges with user-added providers.
            prime-agent.extensions.meters.power.providers = lib.optional (model != "") provider;

            # Zero cost: the power meter reports the real electricity instead.
            prime-agent.settings.providers = lib.optionalAttrs (model != "") {
              ${provider} = {
                api = "openai-completions";
                apiKey = "no-key";
                baseUrl = "http://${clientHost}:${toString port}/v1";

                models = lib.optional (modelId != "") {
                  id = modelId;
                  name = if modelName != "" then modelName else modelId;
                  reasoning = modelReasoning;
                  contextWindow = contextSize;
                  maxTokens = modelMaxTokens;
                  # The server only accepts images with a projector loaded.
                  input = [ "text" ] ++ lib.optional (mmproj != "") "image";

                  cost = {
                    input = 0;
                    output = 0;
                    cacheRead = 0;
                    cacheWrite = 0;
                  };

                  thinkingLevelMap = modelThinkingLevelMap;
                };
              };
            };
          };

          assertions = [
            {
              # llama.cpp rejects unknown types at startup, which lifecycle only shows as a server that never gets healthy.
              assertion =
                specType == ""
                || lib.all (
                  x:
                  builtins.elem x [
                    "none"
                    "draft-simple"
                    "draft-eagle3"
                    "draft-mtp"
                    "draft-dflash"
                    "draft-dspark"
                    "ngram-simple"
                    "ngram-map-k"
                    "ngram-map-k4v"
                    "ngram-mod"
                    "ngram-cache"
                  ]
                ) (lib.splitString "," specType);
              message = ''
                icedos.ai-tools.llamacpp.settings.spec.type must be a comma-separated
                list of llama.cpp --spec-type values: none, draft-simple,
                draft-eagle3, draft-mtp, draft-dflash, draft-dspark,
                ngram-simple, ngram-map-k, ngram-map-k4v, ngram-mod or
                ngram-cache.
              '';
            }
            {
              # After the extension stops the unit, serve --detached starts a scope outside it,
              # and systemd reports inactive for a running server.
              assertion = !(service && lifecycleEnabled);
              message = ''
                icedos.ai-tools.llamacpp.service cannot be combined with
                lifecycle.enable: the extension starts and stops the server itself, so
                the systemd unit ends up fighting it. Pick one owner.
              '';
            }
            {
              # /slots is not on llama.cpp's bypass_sleep list, so polling it every 15 s keeps waking the server.
              assertion = !lifecycleEnabled || sleepIdleSeconds <= 0;
              message = ''
                icedos.ai-tools.llamacpp.lifecycle.enable cannot be combined with
                settings.server.sleepIdleSeconds > 0: the extension polls /slots, which wakes a
                sleeping server. Pick one idle mechanism.
              '';
            }
            {
              # An allowlist, so a name nobody owns is a typo.
              assertion = priorityUsers == "all" || lib.all (n: users ? ${n}) priorityUsers;
              message = ''
                icedos.ai-tools.llamacpp.priorityUsers names users that
                icedos.users does not declare: ${
                  lib.concatStringsSep ", " (lib.filter (n: !(users ? ${n})) priorityUsers)
                }
              '';
            }
          ];

          # Lets --prio set a negative nice. RLIMIT_RTPRIO stays unraised on purpose: ggml's
          # SCHED_FIFO workers on every core can lock the desktop out of input handling.
          security.pam.loginLimits =
            let
              # ggml_sched_priority to nice, as mapped in llama.cpp's common.cpp.
              niceOf =
                p:
                if p >= 3 then
                  -20
                else if p == 2 then
                  -10
                else if p == 1 then
                  -5
                else
                  0;

              # Only --prio calls setpriority(); --prio-batch only affects the threadpool.
              nice = niceOf prio;

              priorityFor = n: priorityUsers == "all" || elem n priorityUsers;
              # System users have no interactive session to raise a limit for.
              selected = attrNames (filterAttrs (n: u: (u.isNormalUser or false) && priorityFor n) users);
            in
            optionals (nice < 0) (
              concatMap (user: [
                {
                  domain = user;
                  type = "hard";
                  item = "nice";
                  value = toString nice;
                }

                {
                  domain = user;
                  type = "soft";
                  item = "nice";
                  value = toString nice;
                }
              ]) selected
            );

          systemd.user.services.llamacpp = mkIf (service && model != "") {
            unitConfig = {
              Description = "llama.cpp server (Vulkan)";
              After = "graphical-session.target";
            };

            wantedBy = [ "graphical-session.target" ];

            serviceConfig = {
              ExecStart = "${llamaServer}";
              Restart = "on-failure";
              RestartSec = 5;
            };
          };

          home-manager.sharedModules = [
            (
              { config, lib, ... }:
              let
                # Resolved by prime-agent. Not settings.dataDir: `config` here is home-manager's,
                # and that option is still unexpanded.
                dataDir = config.home.sessionVariables.PRIME_AGENT_CODING_AGENT_DIR;
                relDataDir = lib.removePrefix (config.home.homeDirectory + "/") dataDir;
              in
              # sharedModules runs for every user; only prime-agent users have this variable.
              lib.mkIf (lifecycleEnabled && config.home.sessionVariables ? PRIME_AGENT_CODING_AGENT_DIR) {
                home.file."${relDataDir}/extensions/llamacpp-lifecycle.ts".source = lifecycleSrc;
              }
            )
            {
              programs.opencode.settings.provider.llamacpp = {
                npm = "@ai-sdk/openai-compatible";
                name = "llama.cpp (Vulkan)";
                options = {
                  baseURL = "http://${clientHost}:${toString port}/v1";
                  apiKey = "no-key";
                };

                models = lib.optionalAttrs (model != "" && modelId != "") {
                  ${modelId} = {
                    name = if modelName != "" then modelName else modelId;
                    reasoning = modelReasoning;
                    tool_call = true;
                    images = mmproj != "";
                    limit = {
                      context = contextSize;
                      output = modelMaxTokens;
                    };
                  };
                };
              };
            }
          ];

          icedos.system.tips.list = [
            "icedos llamacpp serve runs an AI model on your own machine."
            "icedos llamacpp stop shuts the model down and frees your graphics memory."
          ]
          ++ optionals service [
            "Your local AI model server starts on its own at boot."
          ];
        }
      )
    ];

  meta.name = "llamacpp";
}
