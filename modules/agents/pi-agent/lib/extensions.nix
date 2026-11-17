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

  # Shipped extensions have to share ONE directory: they import each other by relative path
  # (cost-footer/shared.ts -> ../free-models.ts) and pi resolves those imports from the file's
  # own location, so a per-file store path would break them. entries is an ordered list of
  # { name, path, load ? true } where name is the destination basename; load = false copies a
  # shared module pi must not load as an extension.
  bundleScript =
    dir: entries:
    lib.concatStringsSep "\n" (
      # dir stays unescaped so a $out in it still expands; only the basename is quoted.
      map (e: "cp -r ${e.path} ${dir}/${lib.escapeShellArg e.name}") entries
    );

  bundlePaths =
    dir: entries: map (e: "${dir}/${e.name}") (builtins.filter (e: e.load or true) entries);

  invalidInlineNames = builtins.filter (n: builtins.match "[A-Za-z0-9_-]+" n == null);

  invalidWindows = builtins.filter (w: builtins.match "[1-9][0-9]*[smhdwM]" w == null);
}
