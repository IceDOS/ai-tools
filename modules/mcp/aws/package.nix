{ pkgs }:

let
  inherit (pkgs) lib;

  # Pins refreshed by ./update.sh.
  source = lib.importJSON ./source.json;

  # Temporary: proxy 2.x needs fastmcp 4; drop once NixOS/nixpkgs#563493 lands.
  # Each bump applies only while nixpkgs is older, so a newer nixpkgs is never downgraded.
  nixpkgsFastmcp = pkgs.python3Packages.fastmcp.version;
  bump =
    prev: name: version: owner: repo: tag: hash: extra:
    if lib.versionOlder prev.${name}.version version then
      prev.${name}.overridePythonAttrs (
        old:
        {
          inherit version;
          src = pkgs.fetchFromGitHub {
            inherit
              owner
              repo
              tag
              hash
              ;
          };
        }
        // extra old
      )
    else
      prev.${name};
  none = _: { };

  python = import ../lib/python.nix {
    inherit pkgs;
    extraOverrides = final: prev: {
      # fastmcp 4's floors; the opentelemetry packages share api's src, so they follow it.
      authlib =
        bump prev "authlib" "1.9.5" "authlib" "authlib" "v1.9.5"
          "sha256-ReO3hDSj85KDsUF0HIjBQuSnez2YiNDx1A/MuAaTkFU="
          none;
      joserfc =
        bump prev "joserfc" "1.7.6" "authlib" "joserfc" "1.7.6"
          "sha256-+JaCwGYyU1Yoy962y+7OCRybrx10rrJbxBsSCCti3uU="
          none;
      openapi-pydantic =
        bump prev "openapi-pydantic" "0.6.0" "mike-oakley" "openapi-pydantic" "v0.6.0"
          "sha256-8vjZ2Sy5LY3h5PacLyUIga/xorGM27WmWxAHI6i7wUs="
          (_: {
            # 0.6 moved from poetry to hatchling.
            build-system = [ final.hatchling ];
          });
      opentelemetry-api =
        bump prev "opentelemetry-api" "1.45.2" "open-telemetry" "opentelemetry-python" "v1.45.2"
          "sha256-Ikcm5BvmMwL8GvaNwZlen55oTesFx9qOyJvEAnT2/r0="
          none;
      # semantic-conventions takes its version from here; 0.66b2 pairs with api 1.45.2.
      opentelemetry-instrumentation =
        bump prev "opentelemetry-instrumentation" "0.66b2" "open-telemetry" "opentelemetry-python-contrib"
          "v0.66b2"
          "sha256-HGFhu+OcdFIYtrsv7ChGovLimaG+a1WM6rUtB+kxOUo="
          none;
      py-key-value-aio =
        bump prev "py-key-value-aio" "0.4.6" "strawgate" "py-key-value" "0.4.6"
          "sha256-R++5cuFjr5HYZhzzYwE4PZNI2ZUmzZ8JHzSfJNqLQk4="
          (old: {
            # The 0.4.6 filetree tests import the encryption wrapper.
            nativeCheckInputs = old.nativeCheckInputs ++ [ final.cryptography ];
          });

      fastmcp =
        bump prev "fastmcp" "4.2.2" "PrefectHQ" "fastmcp" "v4.2.2"
          "sha256-FV5bkBzJ7d7KfhoPpF586tvkW2P6rvB6fNhUlwjl/0A="
          (old: {
            patches = [ ];
            disabledTestPaths = (old.disabledTestPaths or [ ]) ++ [
              # Spawned child Pythons can't import fastmcp in the sandbox.
              "tests/client/test_stdio_session_kwargs.py"
              # Optional extras: pydantic-monty mismatch, prefab-ui missing, anthropic typing.Union bug.
              "tests/experimental/transforms/test_code_mode.py"
              "tests/experimental/transforms/test_monty_callback_lifecycle.py"
              "tests/cli/test_apps_dev_server.py"
              "tests/server/test_session_scope.py"
              "tests/client/sampling/handlers/test_anthropic_handler.py"
              # Repo triage scripts that need jq and network.
              "tests/test_label_triage.py"
              "tests/test_public_triage.py"
            ];
            disabledTests = old.disabledTests ++ [
              "test_code_mode_returns_task_tool_result"
              "test_single_server_config_transform_added_tags_filtering"
            ];
          });

      # Inherits fastmcp's version and src.
      fastmcp-slim =
        if lib.versionOlder nixpkgsFastmcp "4" then
          prev.fastmcp-slim.overridePythonAttrs (old: {
            patches = [ ];
            dependencies = old.dependencies ++ [ final.mcp-types ];
            optional-dependencies = old.optional-dependencies // {
              mcp = map (d: if d == final.httpx then final.httpx2 else d) old.optional-dependencies.mcp;
            };
            # Its install check probes a 3.x-only helper.
            doInstallCheck = false;
            # Marked broken for 3.x on mcp 2; 4.x targets mcp 2.
            meta = old.meta // {
              broken = false;
            };
          })
        else
          prev.fastmcp-slim;
    };
  };

  proxy = python.pkgs.buildPythonApplication {
    pname = "mcp-proxy-for-aws";
    inherit (source.proxy) version;
    pyproject = true;

    src = pkgs.fetchFromGitHub {
      owner = "aws";
      repo = "mcp-proxy-for-aws";
      inherit (source.proxy) rev hash;
    };

    build-system = [ python.pkgs.hatchling ];

    dependencies = with python.pkgs; [
      awscrt
      boto3
      botocore
      fastmcp
      httpx2
    ];

    pythonImportsCheck = [ "mcp_proxy_for_aws" ];

    # Its own suite stays off; this fails the build on any unmet requirement in the closure.
    doCheck = true;
    checkPhase = ''
      runHook preCheck
      PYTHONPATH=$out/${python.sitePackages}:$PYTHONPATH ${python.interpreter} ${./check-deps.py} mcp-proxy-for-aws \
        botocore:awscrt # botocore pins awscrt exactly; nixpkgs ships the same 0.36 series to all botocore users
      runHook postCheck
    '';

    meta.mainProgram = "mcp-proxy-for-aws";
  };

  toolkit = pkgs.fetchFromGitHub {
    owner = "aws";
    repo = "agent-toolkit-for-aws";
    inherit (source.toolkit) rev hash;
  };

  # The proxy is a shared MCP server here, so plugins drop their uvx copy of it.
  # Hooks call a bare python3, which is not on PATH on NixOS.
  plugin =
    name:
    pkgs.runCommand "claude-plugin-${name}" { nativeBuildInputs = [ pkgs.jq ]; } ''
      cp -rL ${toolkit}/plugins/${name} $out
      chmod -R u+w $out
      rm -f $out/mcp.json
      if [ -f $out/.mcp.json ]; then
        jq 'del(.mcpServers."aws-mcp")' $out/.mcp.json > mcp.json
        mv mcp.json $out/.mcp.json
      fi
      if [ -d $out/com.anthropic.claude-code ]; then
        find $out/com.anthropic.claude-code -name hooks.json -exec \
          sed -i 's|python3 |${lib.getExe pkgs.python3} |g' {} +
      fi
    '';
in
{
  proxy = lib.warnIf (
    !lib.versionOlder nixpkgsFastmcp "4"
  ) "aws-mcp: nixpkgs has fastmcp ${nixpkgsFastmcp}; drop the fastmcp override in package.nix" proxy;
  inherit plugin;
}
