# Provider submodule options for models.json.
{ lib }:

{
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
            description = "Maps the agent's thinking levels to the reasoning_effort the model accepts. Keys must be one of off, minimal, low, medium, high, xhigh or max; the agent's schema is non-strict, so a misspelled level is accepted here and then silently ignored at runtime. A level absent from the map is sent through unmapped, so a model that rejects it errors; null hides the level entirely, and xhigh/max are only offered when present as keys.";
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
            description = "Maps the agent's thinking levels to the reasoning_effort the model accepts. Keys must be one of off, minimal, low, medium, high, xhigh or max; the agent's schema is non-strict, so a misspelled level is accepted here and then silently ignored at runtime. A level absent from the map is sent through unmapped, so a model that rejects it errors; null hides the level entirely, and xhigh/max are only offered when present as keys.";
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
            description = "Per-token cost (USD per 1M tokens). The agent requires input, output, cacheRead, cacheWrite.";
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
}
