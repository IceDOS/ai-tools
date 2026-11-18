# icedos-mcp-gateway

One user service fronts the configured stdio MCP servers over streamable HTTP at
`http://<host>:<port>/<name>`.

* `global` servers run one shared child; `per-agent` servers run one child per
  `Mcp-Session-Id` (created on `initialize`, stopped on `DELETE` or the idle
  timeout).
* A child process stops after `idleTimeoutSec` without traffic. Reaping a child
  is transparent: the next request respawns it and replays that session's cached
  `initialize` plus `notifications/initialized`, so the client keeps its
  `Mcp-Session-Id`, its tools and a fresh pid. A child that crashes is treated
  the same way: requests in flight at the crash get a JSON-RPC error, the
  session survives, and the next request respawns and replays.
* A session expires after `max(idleTimeoutSec, 86400)` seconds without a request;
  only then does a later request get 404. The floor is deliberate: the official
  Python SDK turns a 404 into "Session terminated" instead of initializing
  again, so the gateway never relies on a client re-initializing. `DELETE` ends
  a session and its per-agent child immediately.
* A request waits at most `max(idleTimeoutSec, 3600)` seconds for the child; on
  timeout, or when the client disconnects, the client gets a JSON-RPC error and
  the pending child request is released so the child can be reaped.
* `GET /` reports every server's mode and its running children, attributed to
  the session that owns (or last revived) each one.
* Connections from a socket the daemon's uid does not own are rejected with 403.

Run it with `icedos-mcp-gateway --config <path-to-json-config>`.
