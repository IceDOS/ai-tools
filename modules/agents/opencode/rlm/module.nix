{
  defaults,
  icedosLib,
  lib,
}:

let
  inherit (icedosLib)
    mkBoolOption
    mkIntBetweenOption
    mkStrOption
    ;

  pluginDir = ./plugin;

  getRlmBase = pkgs: import ../../../mcp/rlm/package.nix { inherit pkgs lib; };
in
{
  options = {
    enable = mkBoolOption { default = defaults.enable; };
    autoload = mkBoolOption { default = defaults.autoload; };
    maxDepth = mkIntBetweenOption {
      path = "icedos.ai-tools.opencode.extensions.recursive-language-models.maxDepth";
      source = ../config.toml;
      default = defaults.maxDepth;
    } 0 10;
    maxParallel = mkIntBetweenOption {
      path = "icedos.ai-tools.opencode.extensions.recursive-language-models.maxParallel";
      source = ../config.toml;
      default = defaults.maxParallel;
    } 1 64;
    childLog = mkBoolOption { default = defaults.childLog; };
    childTimeout = mkStrOption { default = defaults.childTimeout; };
    snapshots = mkBoolOption { default = defaults.snapshots; };
    includeInIcedosGc = mkBoolOption { default = defaults.includeInIcedosGc; };
    retentionDays = mkIntBetweenOption {
      path = "icedos.ai-tools.opencode.extensions.recursive-language-models.retentionDays";
      source = ../config.toml;
      default = defaults.retentionDays;
    } 1 3650;
  };

  pluginEntry =
    cfg: pkgs: dataHome:
    let
      bin = "${(getRlmBase pkgs).rlm-mcp}/bin/rlm-mcp";
    in
    lib.optional cfg.enable {
      package = "${dataHome}/icedos/agents/per-agent/opencode/plugins/rlm";
      options = {
        rlmMcp = bin;
      };
    };

  settings =
    cfg: pkgs: opencodePackage:
    let
      rlm-base = getRlmBase pkgs;
      bin = "${rlm-base.rlm-mcp}/bin/rlm-mcp";
      kernel = rlm-base.kernel;
    in
    lib.optionalAttrs cfg.enable (
      {
        mcp.kernel = {
          type = "local";
          command = [
            bin
            "serve"
          ];
          environment = {
            RLM_RUNNER = "opencode";
            RLM_OPENCODE = "${opencodePackage}/bin/opencode";
            RLM_KERNEL = "${kernel}/rlm_kernel.py";
            RLM_PYTHON = "${pkgs.python3}/bin/python3";
            RLM_SELF = bin;
            RLM_MAX_DEPTH = toString cfg.maxDepth;
            RLM_MAX_PARALLEL = toString cfg.maxParallel;
            RLM_CHILD_TIMEOUT = cfg.childTimeout;
            RLM_CHILD_LOG = if cfg.childLog then "1" else "0";
            RLM_SNAPSHOTS = if cfg.snapshots then "1" else "0";
          };
          timeout = 86400000;
        };
      }
      // lib.optionalAttrs cfg.autoload {
        default_agent = "rlm";
      }
    );

  agents =
    cfg: mcpServers:
    let
      sanitize =
        s:
        lib.concatMapStrings (c: if builtins.match "[a-zA-Z0-9_-]" c != null then c else "_") (
          lib.stringToCharacters s
        );
      servers = lib.filter (s: s != "kernel") (
        lib.sort (a: b: a < b) (lib.unique (map sanitize mcpServers))
      );
      mcpRules = lib.concatMapStrings (
        srv: "  - action: \"${srv}_*\"\n    resource: \"*\"\n    effect: allow\n"
      ) servers;
      target = "  - action: \"skill\"\n    resource: \"*\"\n    effect: allow\n";
      baseDoc = builtins.readFile ./agents/rlm.md;
    in
    assert lib.hasInfix target baseDoc;
    lib.optionalAttrs cfg.enable {
      rlm = lib.replaceStrings [ target ] [ "${target}${mcpRules}" ] baseDoc;
    };

  skills =
    cfg:
    lib.optionalAttrs cfg.enable {
      rlm = builtins.readFile ./skills/rlm/SKILL.md;
      refine = builtins.readFile ./skills/refine/SKILL.md;
      rlm-children = builtins.readFile ./skills/rlm-children/SKILL.md;
    };

  postGc =
    cfg:
    lib.optional (cfg.enable && cfg.includeInIcedosGc) ''
      D="''${XDG_STATE_HOME:-$HOME/.local/state}/icedos/rlm"
      [ -d "$D" ] || exit 0
      gc_section "$D" "path"
      gc_note "Keeping paths newer than ''${DIM_GREEN}${toString cfg.retentionDays}''${NC}d"
      gc_by_age ${toString cfg.retentionDays} "$D/snapshots" -maxdepth 1 -type f -name '*.pickle'
      gc_by_age ${toString cfg.retentionDays} "$D/children" -mindepth 2 -maxdepth 2 -type f -name '*.jsonl'
      gc_by_age ${toString cfg.retentionDays} "$D/harness" -maxdepth 1 -type f -name '*.jsonl'
      gc_by_age ${toString cfg.retentionDays} "$D/sessions" -maxdepth 1 -type f -name '*.json'
      gc_commit
    '';
}
