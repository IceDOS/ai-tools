# Reads skills shipped in a package source: name = SKILL.md text with the skill's
# references/*.md appended, since icedos.ai-tools.skills holds plain text.
# `dir` is inside a package's src, so reading it is IFD that only needs the fetch.
{ lib }:

dir: names:

let
  inherit (builtins) readDir readFile;

  skill =
    name:
    let
      path = "${dir}/${name}";
      refsDir = "${path}/references";
      refs = lib.optionals (readDir path ? references) (
        lib.sort lib.lessThan (
          lib.attrNames (
            lib.filterAttrs (n: type: type == "regular" && lib.hasSuffix ".md" n) (readDir refsDir)
          )
        )
      );

      inlined = lib.concatMapStrings (
        ref:
        "\n## Reference: references/${ref}\n\n${lib.removeSuffix "\n" (readFile "${refsDir}/${ref}")}\n"
      ) refs;
    in
    readFile "${path}/SKILL.md"
    + lib.optionalString (refs != [ ]) (
      "\n---\n\nThe reference files this skill links to are inlined below.\n" + inlined
    );
in
lib.genAttrs names skill
