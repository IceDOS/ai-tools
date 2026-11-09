{ lib }:

let
  resolveBuiltin =
    {
      examplesDir,
      name,
      exists,
    }:
    if exists "${examplesDir}/${name}/index.ts" then
      "${examplesDir}/${name}"
    else if exists "${examplesDir}/${name}.ts" then
      "${examplesDir}/${name}.ts"
    else
      null;
in
{
  inherit resolveBuiltin;

  missingBuiltins =
    {
      examplesDir,
      names,
      exists,
    }:
    builtins.filter (name: resolveBuiltin { inherit examplesDir name exists; } == null) names;

  order =
    {
      builtin,
      shipped,
      inline,
    }:
    builtin ++ shipped ++ map (name: inline.${name}) (lib.attrNames inline);

  invalidInlineNames = builtins.filter (n: builtins.match "[A-Za-z0-9_-]+" n == null);

  invalidWindows = builtins.filter (w: builtins.match "[1-9][0-9]*[smhdwM]" w == null);
}
