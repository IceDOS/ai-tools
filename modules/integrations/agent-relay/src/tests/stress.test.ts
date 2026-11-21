import { test } from "node:test";
import assert from "node:assert/strict";
import { spawn, type ChildProcess } from "node:child_process";
import { Relay } from "../core.ts";
import { tmpRelay } from "./helpers.ts";

const fs = process.getBuiltinModule("node:fs") as typeof import("node:fs");
const os = process.getBuiltinModule("node:os") as typeof import("node:os");
const path = process.getBuiltinModule("node:path") as typeof import("node:path");

// Real processes against one root: senders, a watcher, and an acker all touch the same files.
const worker = path.resolve(import.meta.dirname, "stress-worker.ts");

interface Sent {
  to: string;
  id: string;
  short: string;
}

interface Result {
  sent?: Sent[];
  withheld?: { pending: number; reason: string }[];
  got?: string[];
  claimed?: string[];
  pairs?: { first: string; second: string }[];
  group?: { id: string; members: string[] };
}

interface Root {
  root: string;
  go: string;
  done: string;
  done2: string;
  env: Record<string, string>;
  relay: Relay;
}

function sandbox(limit?: number): Root {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "relay-stress-"));
  const stateDir = path.join(root, "state");
  const env = {
    AGENT_RELAY_ROOT: root,
    AGENT_RELAY_STATE: stateDir,
    STRESS_GO: path.join(root, ".go"),
    STRESS_DONE: path.join(root, ".done"),
    STRESS_DONE2: path.join(root, ".done2"),
    ...(limit ? { STRESS_LIMIT: String(limit) } : {}),
  };
  return { root, go: env.STRESS_GO!, done: env.STRESS_DONE!, done2: env.STRESS_DONE2!, env, relay: new Relay({ root, stateDir }) };
}

function run(args: string[], env: Record<string, string>): { child: ChildProcess; done: Promise<Result> } {
  const child = spawn(process.execPath, [worker, ...args], { env: { ...process.env, ...env }, stdio: ["ignore", "pipe", "pipe"] });
  let out = "";
  let err = "";
  child.stdout!.on("data", (d: Buffer) => (out += d));
  child.stderr!.on("data", (d: Buffer) => (err += d));
  const done = new Promise<Result>((resolve, reject) => {
    child.on("exit", (code) => {
      if (code !== 0) reject(new Error(`worker ${args.join(" ")} exited ${code}: ${err.slice(0, 500)}`));
      else resolve(out ? (JSON.parse(out) as Result) : {});
    });
  });
  return { child, done };
}

async function waitFor(what: string, check: () => boolean, ms = 10_000): Promise<void> {
  const end = Date.now() + ms;
  while (!check()) {
    if (Date.now() > end) throw new Error(`timed out waiting for ${what}`);
    await new Promise((r) => setTimeout(r, 5));
  }
}

// Register the driver, wait for every child to announce itself, link them, then start the burst.
async function start(s: Root, ids: string[], workers: ReturnType<typeof run>[]): Promise<void> {
  s.relay.register({ kind: "pi", pid: process.pid, cwd: "/tmp", id: "dad01" });
  const file = (id: string) => path.join(s.root, "sessions", `${id}.json`);
  await waitFor("sessions", () => ids.every((id) => fs.existsSync(file(id))));
  for (const id of ids) s.relay.link("dad01", id);
  fs.writeFileSync(s.go, "");
}

const rows = (root: string): { from: string; row: { id: string; short: string; state: string; notice?: true } }[] => {
  const out: { from: string; row: { id: string; short: string; state: string; notice?: true } }[] = [];
  for (const from of fs.readdirSync(path.join(root, "outbox"))) {
    for (const f of fs.readdirSync(path.join(root, "outbox", from))) {
      if (!f.endsWith(".json")) continue;
      out.push({ from, row: JSON.parse(fs.readFileSync(path.join(root, "outbox", from, f), "utf8")) });
    }
  }
  return out;
};

const pair = (root: string, from: string, to: string): { n: number } =>
  JSON.parse(fs.readFileSync(path.join(root, "pairs", from, `${to}.json`), "utf8"));

const queued = (root: string, id: string): string[] => {
  try {
    return fs.readdirSync(path.join(root, "inbox", id)).filter((f) => f.endsWith(".json"));
  } catch {
    return [];
  }
};

test("four senders and a watcher keep every message exactly once", { timeout: 30_000 }, async () => {
  const s = sandbox(1000);
  const kids: ChildProcess[] = [];
  const go = (args: string[]) => {
    const j = run(args, s.env);
    kids.push(j.child);
    return j;
  };
  const senders = ["d0001", "d0002", "d0003", "d0004"];
  try {
  const jobs = senders.map((id) => go(["send", id, "bec01", "50", `m${id.slice(-2)}`]));
  const recv = go(["recv", "bec01", "20000"]);
  await start(s, [...senders, "bec01"], [...jobs, recv]);

  const results = await Promise.all(jobs.map((j) => j.done));
  fs.writeFileSync(s.done, "");
  const seen = (await recv.done).got ?? [];

  const sent = results.flatMap((r) => r.sent ?? []);
  assert.equal(sent.length, 200, "every send was accepted");
  assert.equal(results.flatMap((r) => r.withheld ?? []).length, 0, "nothing was withheld");
  assert.equal(new Set(sent.map((m) => m.id)).size, 200, "message ids are unique");
  assert.equal(new Set(sent.map((m) => m.short)).size, 200, "shorts are unique across processes");
  for (const m of sent) assert.ok(fs.existsSync(path.join(s.root, "shorts", m.short)), `short ${m.short} was claimed`);

  assert.deepEqual(queued(s.root, "bec01"), [], "the inbox is empty once the watcher is done");
  assert.equal(seen.length, 200, "the watcher delivered all 200");
  assert.equal(new Set(seen).size, 200, "no message was delivered twice");
  assert.deepEqual(new Set(seen), new Set(sent.map((m) => m.id)), "delivered set equals sent set");

  for (const id of senders) {
    const mine = rows(s.root).filter((r) => r.from === id);
    assert.equal(mine.length, 50, `${id} has one ledger row per send`);
    for (const r of mine) assert.equal(r.row.state, "delivered", `${r.row.short} is settled`);
    assert.equal(pair(s.root, id, "bec01").n, 0, `${id}'s pair counter reset on the last delivery`);
  }
  } finally {
    for (const c of kids) c.kill("SIGKILL");
  }
});

test("a watcher and an acking process never settle the same message twice", { timeout: 30_000 }, async () => {
  const s = sandbox(1000);
  const kids: ChildProcess[] = [];
  const go = (args: string[]) => {
    const j = run(args, s.env);
    kids.push(j.child);
    return j;
  };
  try {
  const send = go(["send", "d0005", "bec02", "60", "two"]);
  const recv = go(["recv", "bec02", "20000"]);
  const ack = go(["ack", "bec02", "20000"]);
  await start(s, ["d0005", "bec02"], [send, recv, ack]);

  const sent = (await send.done).sent ?? [];
  fs.writeFileSync(s.done, "");
  const [got, claimed] = [(await recv.done).got ?? [], (await ack.done).claimed ?? []];

  const total = got.length + claimed.length;
  assert.equal(sent.length, 60);
  assert.equal(total, 60, `two consumers settled ${total} of 60 messages, expected one claim each`);
  assert.equal(new Set([...got, ...claimed]).size, 60, "no message was lost between the two consumers");
  } finally {
    for (const c of kids) c.kill("SIGKILL");
  }
});

test("ledger states never move backwards", () => {
  const { relay } = tmpRelay();
  const a = relay.register({ kind: "pi", pid: 100, cwd: "/a", id: "aaaa1" });
  const b = relay.register({ kind: "claude", pid: 200, cwd: "/b", id: "bbbb2" });
  relay.link(a.id, b.id);
  const first = relay.send(a.id, "bbbb2", "hi", 10).msgs[0].short;
  const [m] = relay.pending(b.id);

  assert.equal(relay.delivered(m), true);
  assert.equal(relay.delivered(m), false, "the second ack loses the claim");
  relay.read(m);
  assert.equal(relay.status(a.id, first)[0].state, "read");
  assert.equal(relay.delivered(m), false);
  assert.equal(relay.status(a.id, first)[0].state, "read", "a late ack cannot undo read");

  relay.send(a.id, "bbbb2", "fixed", 10, { supersedes: [first] });
  assert.equal(relay.status(a.id, first)[0].state, "read", "a supersede cannot undo a delivered message");
  assert.deepEqual(relay.pending(b.id)[0].supersedes, [first], "but the newer header still says replaces");
});

test("a session write cannot drop a field another writer just set", () => {
  const { root, relay } = tmpRelay();
  const a = relay.register({ kind: "pi", pid: 100, cwd: "/a", id: "aaaa1" });
  const b = relay.register({ kind: "claude", pid: 200, cwd: "/b", id: "bbbb2" });
  // isAlive runs between update()'s read and its write, so this lands in exactly that window.
  let inject: (() => void) | undefined = () => {
    inject = undefined;
    relay.link(a.id, b.id);
  };
  const slow = new Relay({ root, stateDir: relay.stateDir, isAlive: () => (inject?.(), true), ancestorsOf: (p) => [p] });
  slow.update(a.id, { agent: { n: 1 } });

  const rec = relay.get(a.id)!;
  assert.ok(rec.group, "the group link landed before the write");
  assert.deepEqual(rec.agent, { n: 1 }, "and the update landed on top of it");
});

test("pair counters reset to zero once the queue is drained", { timeout: 30_000 }, async () => {
  const s = sandbox(4);
  const kids: ChildProcess[] = [];
  const go = (args: string[]) => {
    const j = run(args, s.env);
    kids.push(j.child);
    return j;
  };
  const senders = ["d0006", "d0007"];
  try {
  const jobs = senders.map((id) => go(["send", id, "bec03", "60", `p${id.slice(-2)}`]));
  const recv = go(["recv", "bec03", "20000"]);
  await start(s, [...senders, "bec03"], [...jobs, recv]);

  const results = await Promise.all(jobs.map((j) => j.done));
  fs.writeFileSync(s.done, "");
  await recv.done;

  const withheld = results.flatMap((r) => r.withheld ?? []);
  for (const w of withheld) assert.match(w.reason, /not yet delivered|in 60s/, "only a limit can withhold");
  for (const id of senders) assert.equal(pair(s.root, id, "bec03").n, 0, `${id}'s burst counter is back to zero`);
  } finally {
    for (const c of kids) c.kill("SIGKILL");
  }
});

const groupsIn = (root: string): { id: string; members: string[] }[] => {
  const dir = fs.existsSync(path.join(root, "state", "groups"))
    ? path.join(root, "state", "groups")
    : fs.existsSync(path.join(root, "groups"))
      ? path.join(root, "groups")
      : "";
  if (!dir) return [];
  return fs
    .readdirSync(dir)
    .filter((f) => f.endsWith(".json"))
    .map((f) => JSON.parse(fs.readFileSync(path.join(dir, f), "utf8")));
};

// Register the driver and wait for the children, but let them do their own linking.
async function meet(s: Root, ids: string[]): Promise<void> {
  s.relay.register({ kind: "pi", pid: process.pid, cwd: "/tmp", id: "dad01" });
  await waitFor("sessions", () => ids.every((id) => fs.existsSync(path.join(s.root, "sessions", `${id}.json`))));
  fs.writeFileSync(s.go, "");
}

// Every joiner writes its own marker once it is linked and then waits, so nothing is reaped early.
async function linked(s: Root, ids: string[], done: string): Promise<void> {
  await waitFor("links", () => ids.every((id) => fs.existsSync(`${done}.${id}`)));
}

test("six processes joining at once end up in one group", { timeout: 30_000 }, async () => {
  const s = sandbox(1000);
  const kids: ChildProcess[] = [];
  const go = (args: string[]) => {
    const j = run(args, s.env);
    kids.push(j.child);
    return j;
  };
  const ids = ["e0001", "e0002", "e0003", "e0004", "e0005", "e0006"];
  try {
    const jobs = ids.map((id) => go(["join", id, "dad01"]));
    await meet(s, ids);
    await linked(s, ids, s.done);

    assert.equal(groupsIn(s.root).length, 1, "one group file, not one per racer");
    const g = groupsIn(s.root)[0];
    assert.deepEqual(new Set(g.members), new Set(["dad01", ...ids]), "nobody was lost");
    for (const id of ["dad01", ...ids]) assert.equal(s.relay.get(id)?.group, g.id, `${id} points at that group`);

    fs.writeFileSync(s.done, "");
    const groups = (await Promise.all(jobs.map((j) => j.done))).map((r) => r.group!);
    assert.equal(new Set(groups.map((x) => x.id)).size, 1, "every joiner reported the same group");
  } finally {
    for (const c of kids) c.kill("SIGKILL");
  }
});

test("two groups merging from several processes at once keep every member", { timeout: 30_000 }, async () => {
  const s = sandbox(1000);
  const kids: ChildProcess[] = [];
  const go = (args: string[]) => {
    const j = run(args, s.env);
    kids.push(j.child);
    return j;
  };
  const left = ["e0001", "e0002", "e0003"];
  const right = ["e0004", "e0005", "e0006"];
  const sides = [...left.map((id) => [id, id === "e0002" ? "e0003" : "e0002"]), ...right.map((id) => [id, id === "e0005" ? "e0006" : "e0005"])];
  try {
    // Two separate groups first, then three racers bridge them at once.
    const jobs = sides.map(([id, peer]) => go(["join", id, peer]));
    await meet(s, [...left, ...right]);
    await linked(s, [...left, ...right], s.done);
    assert.equal(groupsIn(s.root).length, 2, "two groups to start with");

    const bridges = ["e0002 e0005", "e0006 e0001", "e0003 e0004"].map((p, i) => {
      const [self, peer] = p.split(" ");
      return go(["join", self, peer, "STRESS_DONE2"]);
    });
    // A live id belongs to the first joiner, so the bridges get fresh ids of their own.
    await linked(s, ["e0002", "e0006", "e0003"], s.done2);

    const files = groupsIn(s.root);
    assert.equal(files.length, 1, "the merge leaves one group");
    assert.deepEqual(new Set(files[0].members), new Set([...left, ...right]), "every member survived the merge");
    for (const id of [...left, ...right]) assert.equal(s.relay.get(id)?.group, files[0].id, `${id} points at the merged group`);

    fs.writeFileSync(s.done2, "");
    await Promise.all(bridges.map((j) => j.done));
    fs.writeFileSync(s.done, "");
    await Promise.all(jobs.map((j) => j.done));
  } finally {
    for (const c of kids) c.kill("SIGKILL");
  }
});
