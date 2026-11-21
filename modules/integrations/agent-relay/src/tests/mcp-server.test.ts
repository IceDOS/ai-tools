import { test } from "node:test";
import assert from "node:assert/strict";
import { spawn, type ChildProcess } from "node:child_process";
import { Relay } from "../core.ts";

const fs = process.getBuiltinModule("node:fs") as typeof import("node:fs");
const os = process.getBuiltinModule("node:os") as typeof import("node:os");
const path = process.getBuiltinModule("node:path") as typeof import("node:path");

const serverPath = path.resolve(import.meta.dirname, "../mcp-server.ts");
// The server polls its inbox every 3s; every wait below outlives at least one poll.
const POLL_MS = 3200;

interface Line { [k: string]: any }
type Waiter = { pred: (m: Line) => boolean; resolve: (m: Line) => void; timer: ReturnType<typeof setTimeout> };

interface Server {
  proc: ChildProcess;
  queue: Line[];
  notify(pred: (m: Line) => boolean, timeoutMs?: number): Promise<Line>;
  request(id: number, method: string, params?: object): Promise<Line>;
  close(): void;
}

// Spawns the real stdio server, so the test sees exactly the lines Claude sees.
function startServer(root: string, kind: "claude" | "agy" | "opencode"): Server {
  const home = fs.mkdtempSync(path.join(os.tmpdir(), "relay-mcp-home-"));
  const env = { ...process.env, AGENT_RELAY_ROOT: root, HOME: home, XDG_CONFIG_HOME: path.join(home, ".config") };
  delete (env as Record<string, string | undefined>).AGENT_RELAY_ID;
  const proc = spawn(process.execPath, [serverPath, kind], { env, stdio: ["pipe", "pipe", "pipe"] });
  const queue: Line[] = [];
  const waiters: Waiter[] = [];
  let acc = "";
  proc.stdout!.on("data", (d: Buffer) => {
    acc += d.toString("utf8");
    for (let i = acc.indexOf("\n"); i >= 0; i = acc.indexOf("\n")) {
      const line = acc.slice(0, i).trim();
      acc = acc.slice(i + 1);
      if (!line) continue;
      let msg: Line;
      try {
        msg = JSON.parse(line);
      } catch {
        continue;
      }
      const w = waiters.findIndex((x) => x.pred(msg));
      if (w < 0) queue.push(msg);
      else {
        const [hit] = waiters.splice(w, 1);
        clearTimeout(hit.timer);
        hit.resolve(msg);
      }
    }
  });
  const wait = (pred: (m: Line) => boolean, what: string, timeoutMs: number) =>
    new Promise<Line>((resolve, reject) => {
      const i = queue.findIndex(pred);
      if (i >= 0) return resolve(queue.splice(i, 1)[0]);
      const timer = setTimeout(() => reject(new Error(`timed out waiting for ${what}`)), timeoutMs);
      waiters.push({ pred, resolve, timer });
    });
  return {
    proc,
    queue,
    notify: (pred, timeoutMs = 8000) => wait(pred, "a channel notification", timeoutMs),
    request: async (id, method, params = {}) => {
      proc.stdin!.write(JSON.stringify({ jsonrpc: "2.0", id, method, params }) + "\n");
      return wait((m) => m.id === id, method, 8000);
    },
    close: () => {
      proc.kill("SIGKILL");
      fs.rmSync(home, { recursive: true, force: true });
    },
  };
}

const channels = (s: Server) => s.queue.filter((m) => m.method === "notifications/claude/channel");
const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

// The notification is written before the ledger row flips, so wait for the row instead of assuming it.
async function eventually<T>(read: () => T, ok: (v: T) => boolean, ms = 2000): Promise<T> {
  const deadline = Date.now() + ms;
  for (;;) {
    const v = read();
    if (ok(v)) return v;
    if (Date.now() > deadline) throw new Error(`condition never held, last value ${JSON.stringify(v)}`);
    await sleep(25);
  }
}
const relayAt = (root: string) => new Relay({ root, isAlive: () => true, ancestorsOf: () => [] });


test("claude: one channel notification per message, ledger delivered, inbox empty, read on the next tool call", { timeout: 30_000 }, async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "relay-mcp-"));
  const relay = relayAt(root);
  const server = startServer(root, "claude");
  try {
    const init = await server.request(1, "initialize", { protocolVersion: "2025-06-18" });
    assert.deepEqual(init.result.capabilities.experimental, { "claude/channel": {} });
    server.proc.stdin!.write(JSON.stringify({ jsonrpc: "2.0", method: "notifications/initialized" }) + "\n");
    const id = /relay ID is ([0-9a-f]{5})/.exec(init.result.instructions)![1];

    const me = relay.register({ kind: "pi", pid: process.pid, cwd: root });
    relay.link(me.id, id);
    const first = relay.send(me.id, id, "first probe").msgs[0];
    const n1 = await server.notify((m) => m.method === "notifications/claude/channel");
    assert.ok(n1.params.content.startsWith(`[relay from pi ${me.id}, hop 1, msg ${first.short} `), n1.params.content);
    assert.ok(n1.params.content.includes("first probe\n"));
    assert.deepEqual(n1.params.meta, { from: me.id, kind: "pi", hop: "1" });

    // Delivered leaves the queue, so no second consumer and no second announcement.
    await eventually(() => relay.status(me.id, first.short)[0].state, (s) => s === "delivered");
    assert.deepEqual(relay.pending(id), []);
    await sleep(POLL_MS);
    assert.equal(relay.status(me.id, first.short)[0].state, "delivered");
    assert.deepEqual(channels(server), []);

    const second = relay.send(me.id, id, "second probe").msgs[0];
    const n2 = await server.notify((m) => m.method === "notifications/claude/channel");
    assert.ok(n2.params.content.includes(`msg ${second.short} `), n2.params.content);
    assert.ok(n2.params.content.includes("second probe\n"));
    await sleep(POLL_MS);
    assert.equal(relay.status(me.id, first.short)[0].state, "delivered");
    assert.equal(relay.status(me.id, second.short)[0].state, "delivered");
    assert.deepEqual(channels(server), []);

    // Read only after the model's next tool call: until then the text is unread.
    const call = await server.request(9, "tools/call", { name: "relay_peers", arguments: {} });
    assert.equal(call.result.isError, undefined);
    assert.deepEqual(relay.status(me.id).map((r) => r.state), ["read", "read"]);
    assert.deepEqual(relay.pending(id), []);
    assert.deepEqual(relay.unread(id), {});
  } finally {
    server.close();
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("agy: no channel capability and no delivery from the MCP server, agy-watch owns that", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "relay-mcp-"));
  const relay = relayAt(root);
  const server = startServer(root, "agy");
  try {
    const init = await server.request(1, "initialize");
    assert.equal(init.result.capabilities.experimental, undefined);
    server.proc.stdin!.write(JSON.stringify({ jsonrpc: "2.0", method: "notifications/initialized" }) + "\n");
    const id = /relay ID is ([0-9a-f]{5})/.exec(init.result.instructions)![1];
    const me = relay.register({ kind: "pi", pid: process.pid, cwd: root });
    relay.link(me.id, id);
    const msg = relay.send(me.id, id, "held for the watcher").msgs[0];
    await sleep(POLL_MS);
    assert.deepEqual(channels(server), []);
    assert.equal(relay.status(me.id, msg.short)[0].state, "queued");
    assert.equal(relay.pending(id).length, 1);
  } finally {
    server.close();
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("opencode: resolves session per tool call via _meta sessionID or root map, fails when missing", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "relay-mcp-"));
  const relay = relayAt(root);
  const rootRec = relay.register({ kind: "opencode", pid: process.pid, cwd: root, scope: "ses_root" });
  const server = startServer(root, "opencode");
  try {
    const init = await server.request(1, "initialize");
    assert.equal(init.result.capabilities.experimental, undefined);
    assert.equal(init.result.instructions.includes("Your relay ID is"), false);

    // Call without _meta fails clearly
    const failNoMeta = await server.request(2, "tools/call", { name: "relay_peers", arguments: {} });
    assert.equal(failNoMeta.result.isError, true);
    assert.match(failNoMeta.result.content[0].text, /Missing ai\.opencode\/sessionID/);

    // Call with unknown sessionID fails clearly
    const failUnknown = await server.request(3, "tools/call", {
      name: "relay_peers",
      arguments: {},
      _meta: { "ai.opencode/sessionID": "ses_unknown" },
    });
    assert.equal(failUnknown.result.isError, true);
    assert.match(failUnknown.result.content[0].text, /No registered relay session found/);

    // Call with root session succeeds
    const callRoot = await server.request(4, "tools/call", {
      name: "relay_peers",
      arguments: {},
      _meta: { "ai.opencode/sessionID": "ses_root" },
    });
    assert.equal(callRoot.result.isError, undefined);
    assert.match(callRoot.result.content[0].text, /not linked to any session/);

    // Call with subagent mapped in roots/ succeeds
    const rootsDir = path.join(root, "opencode", "roots");
    fs.mkdirSync(rootsDir, { recursive: true });
    fs.writeFileSync(path.join(rootsDir, "ses_sub.json"), JSON.stringify({ rootSessionID: "ses_root" }));
    const callSub = await server.request(5, "tools/call", {
      name: "relay_peers",
      arguments: {},
      _meta: { "ai.opencode/sessionID": "ses_sub" },
    });
    assert.equal(callSub.result.isError, undefined);
    assert.match(callSub.result.content[0].text, /not linked to any session/);
  } finally {
    server.close();
    fs.rmSync(root, { recursive: true, force: true });
  }
});
