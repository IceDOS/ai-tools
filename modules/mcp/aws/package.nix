{ pkgs }:

let
  inherit (pkgs) lib;

  # Pins refreshed by ./update.sh.
  source = lib.importJSON ./source.json;

  # nixpkgs FastMCP 3.x needs the MCP SDK 1.x.
  python = import ../lib/python-mcp1.nix { inherit pkgs; };

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
      httpx
    ];

    pythonImportsCheck = [ "mcp_proxy_for_aws" ];
    doCheck = false;

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
  inherit proxy plugin;
}
