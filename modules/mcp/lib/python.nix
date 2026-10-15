# Python 3 with the MCP Python SDK 2.x, which nixpkgs does not ship yet (it has 1.x).
# extraOverrides adds a server's own packages on top of the same scope.
{
  pkgs,
  extraOverrides ? (_: _: { }),
}:

let
  inherit (pkgs) lib fetchurl;

  # Pinned wheels, refreshed by ./update-python-sdk.sh.
  sdk = lib.importJSON ./python-sdk.json;

  # Pure-Python wheel from PyPI.
  wheel =
    py:
    {
      pname,
      version,
      url,
      hash,
      dependencies ? [ ],
      ...
    }@args:
    py.buildPythonPackage (
      removeAttrs args [
        "url"
        "hash"
      ]
      // {
        format = "wheel";
        src = fetchurl { inherit url hash; };
        inherit dependencies;
        doCheck = false;
      }
    );

  sdkOverrides = final: prev: {
    mkWheel = wheel final;

    httpcore2 = final.mkWheel {
      pname = "httpcore2";
      inherit (sdk.httpcore2) version url hash;
      dependencies = with final; [
        anyio
        h11
        truststore
      ];
    };

    httpx2 = final.mkWheel {
      pname = "httpx2";
      inherit (sdk.httpx2) version url hash;
      dependencies = with final; [
        anyio
        httpcore2
        idna
        truststore
      ];
    };

    mcp-types = final.mkWheel {
      pname = "mcp-types";
      inherit (sdk.mcp-types) version url hash;
      dependencies = with final; [
        pydantic
        typing-extensions
      ];
    };

    mcp = final.mkWheel {
      pname = "mcp";
      inherit (sdk.mcp) version url hash;
      dependencies =
        with final;
        [
          anyio
          httpx2
          jsonschema
          mcp-types
          opentelemetry-api
          pydantic
          pyjwt
          python-multipart
          sse-starlette
          starlette
          typing-extensions
          typing-inspection
          uvicorn
        ]
        ++ pyjwt.optional-dependencies.crypto;
    };
  };
  python = pkgs.python3.override {
    self = python;
    packageOverrides = lib.composeExtensions sdkOverrides extraOverrides;
  };
in
python
