# Python 3 with the MCP Python SDK 1.x, for servers capped at mcp<2 or built on nixpkgs FastMCP 3.x.
# nixpkgs ships mcp 2.x, which marks its FastMCP 3.x broken.
{ pkgs }:

let
  python = pkgs.python3.override {
    self = python;
    packageOverrides = final: prev: {
      mcp = final.buildPythonPackage {
        pname = "mcp";
        version = "1.30.0";
        format = "wheel";
        src = pkgs.fetchurl {
          url = "https://files.pythonhosted.org/packages/f5/f4/e58bc33317c92a0203664daaf00bf6f41166cc0149e5d6870a03f7cd004a/mcp-1.30.0-py3-none-any.whl";
          hash = "sha256-Zm7bUAlQPhBHydYDRqdW+UsmHwXMJiXyPUHHKP/EhNA=";
        };
        dependencies =
          with final;
          [
            anyio
            httpx
            httpx-sse
            jsonschema
            pydantic
            pydantic-settings
            pyjwt
            python-multipart
            sse-starlette
            starlette
            typing-extensions
            typing-inspection
            uvicorn
          ]
          ++ pyjwt.optional-dependencies.crypto;
        doCheck = false;
      };

      # Its test suite expects the mcp 2.x that nixpkgs pairs it with.
      fastmcp = prev.fastmcp.overridePythonAttrs { doCheck = false; };
    };
  };
in
python
