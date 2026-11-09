# programs.mcp.servers -> pi's mcp.json ({ mcpServers = ...; }).
{ lib }:

let
  # Mirrors home-manager's enabled/disabled resolution, which it does not export.
  isEnabled =
    s:
    if (s.enabled or null) != null then
      s.enabled
    else if (s.disabled or null) != null then
      !s.disabled
    else
      true;

  nonEmpty = name: v: lib.optionalAttrs (v != null && v != [ ] && v != { }) { ${name} = v; };

  entry =
    timeout: s:
    let
      bearer = s.bearerTokenEnvVar or null;
      headers =
        lib.optionalAttrs (bearer != null) { Authorization = "Bearer \${${bearer}}"; }
        // (s.headers or { });
      transport =
        if (s.command or null) != null then
          {
            inherit (s) command;
          }
          // nonEmpty "args" (s.args or null)
          // nonEmpty "env" (s.env or null)
          // nonEmpty "cwd" (s.cwd or null)
        else
          { inherit (s) url; } // nonEmpty "headers" headers;
    in
    transport // { inherit timeout; } // lib.optionalAttrs (!isEnabled s) { enabled = false; };
in
{
  render =
    { servers, timeout }:
    {
      mcpServers = lib.mapAttrs (_: entry timeout) servers;
    };

  # pi accepts only letters, digits, _ and - in server names.
  invalidNames =
    servers: lib.filter (n: builtins.match "[A-Za-z0-9_-]+" n == null) (lib.attrNames servers);
}
