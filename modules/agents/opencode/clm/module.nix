{
  defaults,
  icedosLib,
  lib,
}:

let
  inherit (icedosLib) mkBoolOption mkStrOption;

  # Format non-empty plugin settings for the opencode.json plugin entry.
  mkPluginEntry =
    clm:
    let
      pkg = "opencode-clm" + lib.optionalString (clm.version != "") "@${clm.version}";
      # Omit compaction: OpenCode 2 manages it via settings.compaction.auto.
      rawOptions = {
        inherit (clm)
          budget
          reserve
          remindAt
          reminderCooldown
          gate
          guard
          observationCap
          mirrorDir
          steering
          compactPrompt
          estimateFactor
          mode
          oneTool
          trailer
          reasoning
          dumpRequests
          ;
      };
      options = lib.filterAttrs (_: v: v != "") rawOptions;
    in
    if options == { } then
      pkg
    else
      {
        package = pkg;
        inherit options;
      };
in
{
  options = {
    enable = mkBoolOption { default = defaults.enable; };
    version = mkStrOption { default = defaults.version; };
    budget = mkStrOption { default = defaults.budget; };
    reserve = mkStrOption { default = defaults.reserve; };
    remindAt = mkStrOption { default = defaults.remindAt; };
    reminderCooldown = mkStrOption { default = defaults.reminderCooldown; };
    gate = mkStrOption { default = defaults.gate; };
    guard = mkStrOption { default = defaults.guard; };
    observationCap = mkStrOption { default = defaults.observationCap; };
    mirrorDir = mkStrOption { default = defaults.mirrorDir; };
    steering = mkStrOption { default = defaults.steering; };
    compactPrompt = mkStrOption { default = defaults.compactPrompt; };
    estimateFactor = mkStrOption { default = defaults.estimateFactor; };
    compaction = mkStrOption { default = defaults.compaction; };
    mode = mkStrOption { default = defaults.mode; };
    oneTool = mkStrOption { default = defaults.oneTool; };
    trailer = mkStrOption { default = defaults.trailer; };
    reasoning = mkStrOption { default = defaults.reasoning; };
    dumpRequests = mkStrOption { default = defaults.dumpRequests; };
  };

  pluginEntry = clm: lib.optional clm.enable (mkPluginEntry clm);

  settings =
    clm:
    lib.optionalAttrs (clm.enable && clm.compaction == "off") {
      compaction.auto = false;
    };
}
