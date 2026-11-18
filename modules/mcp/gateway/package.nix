{ pkgs }:

let
  # The daemon serves streamable HTTP with the MCP Python SDK 1.x.
  python = import ../lib/python-mcp1.nix { inherit pkgs; };

  isPackageFile =
    rel:
    rel == "icedos_mcp_gateway"
    || (
      pkgs.lib.hasPrefix "icedos_mcp_gateway/" rel
      && !(pkgs.lib.hasPrefix "icedos_mcp_gateway/__pycache__" rel)
    );
in
python.pkgs.buildPythonApplication {
  pname = "icedos-mcp-gateway";
  version = "0.1.0";
  pyproject = true;

  src = builtins.path {
    path = ./src;
    name = "icedos-mcp-gateway-src";
    # pyproject.toml needs README.md, and __pycache__ would only add bytecode.
    filter =
      path: _:
      let
        rel = pkgs.lib.removePrefix (toString ./src + "/") (toString path);
      in
      rel == "pyproject.toml" || rel == "README.md" || isPackageFile rel;
  };

  build-system = [ python.pkgs.hatchling ];

  dependencies = with python.pkgs; [
    mcp
    starlette
    uvicorn
  ];

  pythonImportsCheck = [ "icedos_mcp_gateway" ];

  meta.mainProgram = "icedos-mcp-gateway";
}
