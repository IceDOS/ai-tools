{ lib }:

{
  render =
    {
      providers,
      opencodeFree,
    }:
    let
      # pi only provisions providers keyed in models.json; without a key here opencode-free
      # stays unconfigured until its async auth pass, so startup can fall back to llamacpp.
      keyed =
        if !opencodeFree then
          providers
        else
          let
            existing = providers."opencode-free" or { };
          in
          providers
          // {
            opencode-free =
              if (existing.apiKey or null) == null then
                existing // { apiKey = "\${PI_OPENCODE_FREE_API_KEY}"; }
              else
                existing;
          };
    in
    lib.mapAttrs (
      _: p:
      let
        attrs =
          lib.filterAttrs (_: v: v != null) {
            api = p.api or null;
            apiKey = p.apiKey or null;
            baseUrl = p.baseUrl or null;
          }
          // lib.optionalAttrs ((p.headers or { }) != { }) { headers = p.headers; };
        overrides = lib.mapAttrs (_: o: lib.filterAttrs (_: v: v != null) o) (p.modelOverrides or { });
        models = map (m: lib.filterAttrs (_: v: v != null) m) (p.models or [ ]);
      in
      attrs
      // lib.optionalAttrs (overrides != { }) { modelOverrides = overrides; }
      // lib.optionalAttrs (models != [ ]) { inherit models; }
    ) keyed;
}
