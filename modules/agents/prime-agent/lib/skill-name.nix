# Skill-name rule shared by module + package; mirrors the loader's validateName.
{ lib }:

let
  validSkillName =
    n:
    builtins.match "[a-z0-9-]+" n != null
    && !lib.hasPrefix "-" n
    && !lib.hasSuffix "-" n
    && !lib.hasInfix "--" n
    && lib.stringLength n <= 64;
in
{
  inherit validSkillName;
  # Names violating the rule, for assertions (e.g. attrNames of extraBuiltinSkills).
  invalidSkillNames = names: lib.filter (n: !validSkillName n) names;
}
