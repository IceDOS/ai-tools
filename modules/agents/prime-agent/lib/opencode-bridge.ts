// OpenCode inference forwarder: forwards requests through Bun
// to pass client verification for free-tier inference.

const portEnv = process.env.OPENCODE_BRIDGE_PORT;
if (!portEnv) {
  throw new Error("OPENCODE_BRIDGE_PORT environment variable is required");
}
const PORT = Number(portEnv);

Bun.serve({
  port: PORT,
  hostname: "127.0.0.1",
  idleTimeout: 0,
  async fetch(req) {
    const url = new URL(req.url);
    if (url.pathname === "/health") {
      return new Response("OK", { status: 200 });
    }

    const targetUrl = new URL(url.pathname + url.search, "https://opencode.ai");
    const headers = new Headers(req.headers);
    headers.delete("host");
    headers.delete("connection");

    try {
      const res = await fetch(targetUrl.toString(), {
        method: req.method,
        headers,
        body: req.method !== "GET" && req.method !== "HEAD" ? req.body : undefined,
        signal: req.signal,
      });

      const resHeaders = new Headers(res.headers);
      resHeaders.delete("content-encoding");
      resHeaders.delete("content-length");

      return new Response(res.body, {
        status: res.status,
        statusText: res.statusText,
        headers: resHeaders,
      });
    } catch (err: any) {
      if (err?.name === "AbortError") {
        return new Response(null, { status: 499 });
      }
      return new Response(JSON.stringify({ error: err?.message || String(err) }), {
        status: 502,
        headers: { "Content-Type": "application/json" },
      });
    }
  },
});
