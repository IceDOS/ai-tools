# settings.dataDir expansion: "" means $XDG_CONFIG_HOME/pi-agent.
{ lib }:

let
  rawOf = raw: if raw == "" then "$XDG_CONFIG_HOME/pi-agent" else raw;

  tilde =
    home: s:
    if s == "~" then
      home
    else if lib.hasPrefix "~/" s then
      home + lib.removePrefix "~" s
    else
      s;

  stripTrailing =
    s: if s != "/" && lib.hasSuffix "/" s then stripTrailing (lib.removeSuffix "/" s) else s;

  collapse =
    s:
    let
      s' = builtins.replaceStrings [ "//" ] [ "/" ] s;
    in
    if s' == s then s else collapse s';
in
{
  # Absolute per-user path, resolved at eval time.
  expand =
    {
      raw,
      home,
      configHome,
      dataHome,
    }:
    collapse (
      tilde home (
        stripTrailing (
          builtins.replaceStrings [ "$XDG_CONFIG_HOME" "$XDG_DATA_HOME" "$HOME" ] [ configHome dataHome home ]
            (rawOf raw)
        )
      )
    );

  # Shell string for the wrapper; XDG vars carry their spec defaults for an empty environment.
  shell =
    raw:
    tilde "$HOME" (
      builtins.replaceStrings
        [ "$XDG_CONFIG_HOME" "$XDG_DATA_HOME" ]
        [ "\${XDG_CONFIG_HOME:-$HOME/.config}" "\${XDG_DATA_HOME:-$HOME/.local/share}" ]
        (rawOf raw)
    );
}
