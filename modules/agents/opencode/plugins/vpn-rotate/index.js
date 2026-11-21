// Rotates the VPN tunnel when the opencode provider rate-limits, and routes only that provider through it.
import { execFile, spawn } from "node:child_process";
import { readFileSync } from "node:fs";

const statePath = () => `${process.env.XDG_RUNTIME_DIR || "/tmp"}/vpn-rotate.json`;

export const ERROR_KINDS = { "provider.rate-limit": "rate_limit", "provider.free-limit": "usage_limit" };

// Only the service's private tunnel gets a proxy; direct mode is the host IP as-is.
export function tunnelProxy(state) {
  return state?.mode === "tunnel" && typeof state.proxy === "string" && state.proxy !== "" ? state.proxy : undefined;
}

function readProxy() {
  try {
    return tunnelProxy(JSON.parse(readFileSync(statePath(), "utf8")));
  } catch {
    return undefined;
  }
}

export const VPN_ACTIONS = ["status", "cancel", "clear"];

// Runs the socket client for /vpn-rotate and returns its output; no argument means status.
export function control(command, args, run = execFileText) {
  const action = String(args ?? "").trim() || "status";
  if (!VPN_ACTIONS.includes(action)) return Promise.resolve(`Usage: /vpn-rotate [${VPN_ACTIONS.join("|")}]`);
  return run(command[0], [...command.slice(1), action]);
}

// The session moved on to another model, so nobody waits for the rotation started for its old one.
export function abandoned(rotations, event) {
  const model = rotations.get(event.sessionID);
  return model !== undefined && event.kind === "primary" && event.model?.id !== model;
}

// Milliseconds for a fallbackReturn duration such as "90s", "15m" or "2h"; undefined for "prompt", "stay" or junk.
export function returnAfter(value) {
  const m = /^(\d+)([smh])$/.exec(String(value ?? ""));
  return m ? Number(m[1]) * { s: 1_000, m: 60_000, h: 3_600_000 }[m[2]] : undefined;
}

// The next model after `current` in list order, wrapping around and skipping models this prompt already tried.
export function nextModel(list, current, tried) {
  const start = list.indexOf(current) + 1;
  for (let i = 0; i < list.length; i++) {
    const id = list[(start + i) % list.length];
    if (id !== current && !tried.has(id)) return id;
  }
}


// Free by price, not by the "-free" suffix: Zen also serves free models like big-pickle.
export const isFree = (m) => (m.cost ?? []).every((c) => !Number(c.input) && !Number(c.output));

export const stripProvider = (id) => String(id).replace(/^opencode\//, "");

function execFileText(file, args) {
  return new Promise((resolve) =>
    execFile(file, args, { timeout: 10_000 }, (err, stdout, stderr) =>
      resolve(`${stdout}${stderr}`.trim() || (err ? err.message : "done")),
    ),
  );
}

// The forwarder's first path segment carries the provider's real base URL.
export const wrapBaseURL = (port, baseURL) => `http://127.0.0.1:${port}/${encodeURIComponent(baseURL)}`;

export function unwrapURL(url) {
  const u = new URL(url);
  const [, head, ...rest] = u.pathname.split("/");
  const base = decodeURIComponent(head).replace(/\/$/, "");
  return base + (rest.length ? `/${rest.join("/")}` : "") + u.search;
}

function forward(req) {
  const headers = new Headers(req.headers);
  headers.delete("host");
  return fetch(unwrapURL(req.url), {
    method: req.method,
    headers,
    body: req.body,
    redirect: "manual",
    // Passes compressed bodies through unchanged, with their content-encoding.
    decompress: false,
    proxy: readProxy(),
  });
}

// Resolves to { ok, reason }; the client prints the failure reason on stderr.
function rotate(command, payload, timeoutMs) {
  return new Promise((resolve) => {
    let child;
    try {
      child = spawn(command[0], command.slice(1), { stdio: ["pipe", "ignore", "pipe"] });
    } catch {
      return resolve({ ok: false, reason: "spawn failed" });
    }
    let stderr = "";
    child.stderr.on("data", (d) => (stderr += d));
    const timer = setTimeout(() => child.kill("SIGKILL"), timeoutMs);
    child.on("error", () => (clearTimeout(timer), resolve({ ok: false, reason: "spawn failed" })));
    child.on("exit", (code) => (clearTimeout(timer), resolve({ ok: code === 0, reason: stderr.trim() })));
    child.stdin.on("error", () => {});
    child.stdin.end(JSON.stringify(payload));
  });
}

export default {
  id: "icedos.vpn-rotate",
  async setup(ctx) {
    const command = ctx.options?.command;
    if (!Array.isArray(command) || command.length === 0) return;
    const maxRetries = Number(ctx.options?.maxRetries ?? 0);
    const providerID = "opencode";
    const fallbackModels = (ctx.options?.fallbackModels ?? []).map(stripProvider);
    const resetMs = returnAfter(ctx.options?.fallbackReturn);
    const returnOnPrompt = ctx.options?.fallbackReturn !== "stay" && resetMs === undefined;

    // Bun.serve is only there when the server runs on Bun; without it requests stay direct.
    const server =
      typeof Bun === "undefined"
        ? undefined
        : Bun.serve({ hostname: "127.0.0.1", port: 0, idleTimeout: 0, fetch: forward });

    await ctx.session.hook(
      "model.request",
      (event) => {
        if (server && event.baseURL && readProxy()) event.baseURL = wrapBaseURL(server.port, event.baseURL);
      },
      { providerID },
    );

    // sessionID -> fallback model id, until its first request goes out; covers a retry that beats switchModel.
    const pending = new Map();
    // sessionID -> { original: Model.Ref, model, timer? }: what to switch back to.
    const returns = new Map();
    // sessionID -> models that failed during the current prompt.
    const tried = new Map();
    const freeModels = async () => {
      try {
        return (await ctx.model.list()).data
          .filter((m) => m.providerID === providerID && m.enabled && isFree(m))
          .map((m) => m.id);
      } catch {
        return [];
      }
    };

    // sessionID -> model of the rotation running for that session's retry.
    const rotations = new Map();
    await ctx.session.hook("model.request", (event) => {
      if (!abandoned(rotations, event)) return;
      rotations.delete(event.sessionID);
      void control(command, "cancel");
    });

    await ctx.session.hook(
      "retry",
      async (event) => {
        const errorKind = ERROR_KINDS[event.error.type];
        if (!errorKind) return;
        const retry = event.attempt - 1;
        if (maxRetries > 0 && retry > maxRetries) return;
        rotations.set(event.sessionID, event.model.id);
        let result;
        try {
          // provider "opencode-free" makes the service probe with this exact model.
          result = await rotate(
            command,
            {
              session_id: event.sessionID,
              provider: "opencode-free",
              model: event.model.id,
              errorKind,
              errorMessage: event.error.message,
              retry,
            },
            310_000,
          );
        } finally {
          rotations.delete(event.sessionID);
        }
        if (result.ok) return void (event.decision = { retry: true, delay: 0 });
        // A cancelled rotation means the session moved on or the user stopped it.
        if (/cancelled/.test(result.reason)) return;
        const sid = event.sessionID;
        const current = event.model.id;
        const seen = tried.get(sid) ?? new Set();
        seen.add(current);
        tried.set(sid, seen);
        const next = nextModel(fallbackModels.length ? fallbackModels : await freeModels(), current, seen);
        if (!next) return;
        const prev = returns.get(sid);
        clearTimeout(prev?.timer);
        const entry = { original: prev?.original ?? event.model, model: next };
        if (resetMs !== undefined) entry.timer = setTimeout(() => void restore(sid), resetMs);
        entry.timer?.unref?.();
        returns.set(sid, entry);
        pending.set(sid, next);
        // A real switch, so the input bar shows the fallback model.
        await ctx.session.switchModel({ sessionID: sid, model: { providerID, id: next } }).catch(() => {});
        event.decision = { retry: true, delay: 0 };
      },
      { providerID },
    );

    // Switches back only while the session still uses the fallback, so a model the user picked stays.
    async function restore(sid) {
      const entry = returns.get(sid);
      if (!entry) return;
      returns.delete(sid);
      clearTimeout(entry.timer);
      try {
        const session = await ctx.session.get({ sessionID: sid });
        if (session.model?.id === entry.model) await ctx.session.switchModel({ sessionID: sid, model: entry.original });
      } catch {}
    }

    await ctx.session.hook(
      "experimental.model.resolve",
      (event) => {
        const model = pending.get(event.sessionID);
        if (event.kind === "primary" && model) event.model = { providerID: event.model.providerID, id: model };
      },
      { providerID },
    );
    await ctx.session.hook("model.request", (event) => {
      if (event.kind === "primary") pending.delete(event.sessionID);
    });

    await ctx.session.hook("prompt", async (event) => {
      tried.delete(event.sessionID);
      if (returnOnPrompt) await restore(event.sessionID);
    });

    await ctx.command.transform((editor) => {
      editor.add({
        name: "vpn-rotate",
        description: "VPN rotation service: /vpn-rotate [status|cancel|clear]",
        execute: async (input) => {
          const text = await control(command, input.prompt?.text);
          await ctx.session.synthetic?.({ sessionID: input.sessionID, text });
        },
      });
    });

    return () => server?.stop(true);
  },
};
