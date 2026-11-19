import { test } from "node:test";
import assert from "node:assert/strict";
import { BUSY_SETTLE_MS, PENDING_LEASE_MS, PROMPT_QUIET_MS, PTY_QUIET_MS, findAgy, makeDeliver, socketPty, type PasteResult, type Pty } from "../agy-watch.ts";
import { formatMessage, Relay } from "../core.ts";
import { tmpRelay } from "./helpers.ts";

const fs = process.getBuiltinModule("node:fs") as typeof import("node:fs");
const path = process.getBuiltinModule("node:path") as typeof import("node:path");

function setup(agent: Record<string, unknown> = { busy: false }) {
  const clock = { t: Date.parse("2026-11-11T10:00:00Z") };
  const now = () => new Date(clock.t);
  const { relay, root } = tmpRelay(new Set([100, 200, 300, 400]), now);
  const a = relay.register({ kind: "pi", pid: 100, cwd: "/a", id: "aaaa1" });
  const g = relay.register({ kind: "agy", pid: 300, cwd: "/c", id: "cccc3", agent });
  relay.link(a.id, g.id);
  return { relay, root, a, g, clock, now };
}

function fakePty(result: PasteResult = "ok", idle = false, silentMs?: number, onStatus?: "" | "throw") {
  const sent: string[] = [];
  let asks = 0;
  const pty: Pty = {
    paste: async (text) => {
      sent.push(text);
      return result;
    },
    status: async () => {
      asks++;
      if (onStatus === "throw") throw new Error("socket down");
      return { idle, dirty: false, silentMs: silentMs ?? 60_000 };
    },
  };
  return { sent, pty, asks: () => asks };
}

async function pass(relay: Relay, id: string, pty: Pty, now: () => Date, watcherPid = 1111): Promise<void> {
  const w = relay.watch(id, makeDeliver(relay, id, pty, now, watcherPid), 60_000);
  await w.drain();
  w.close();
}

const inbox = (root: string, id: string) => fs.readdirSync(path.join(root, "inbox", id)).filter((f) => f.endsWith(".json"));

test("findAgy matches the agy record whose pid descends from the launched pid", () => {
  const { relay } = tmpRelay();
  relay.register({ kind: "pi", pid: 100, cwd: "/a" });
  const g = relay.register({ kind: "agy", pid: 300, cwd: "/c" });
  const chain = (pid: number) => (pid === 300 ? [300, 250, 1] : [pid, 1]);
  assert.equal(findAgy(relay, 250, chain)?.id, g.id);
  assert.equal(findAgy(relay, 999, chain), undefined);
});

test("idle agy: pastes the formatted message, marks busy and empties the queue", async () => {
  const { relay, root, a, g, clock, now } = setup();
  relay.send(a.id, g.id, "wake up", 10);
  const m = relay.pending(g.id)[0];
  const { sent, pty, asks } = fakePty();
  await pass(relay, g.id, pty, now);
  assert.equal(sent.length, 1);
  assert.equal(sent[0], formatMessage(m));
  assert.equal(relay.get(g.id)!.agent?.busy, true);
  assert.equal(relay.get(g.id)!.agent?.busyAt, clock.t);
  assert.deepEqual((relay.get(g.id)!.agent?.awaitingRead as { id: string }[]).map((x) => x.id), [m.id]);
  assert.equal(inbox(root, g.id).length, 0);
  assert.equal(asks(), 1); // the prompt gate only
});

test("the watcher records its own pid so agy-stop can leave the batch to it", async () => {
  const { relay, a, g, now } = setup();
  relay.send(a.id, g.id, "who takes it", 10);
  const { pty } = fakePty();
  await pass(relay, g.id, pty, now, 4242);
  assert.equal(relay.get(g.id)!.agent?.watcher, 4242);
});

test("the batch is pasted only once the prompt has been quiet for 3s", async () => {
  const { relay, root, a, g, now } = setup();
  relay.send(a.id, g.id, "wait for the prompt", 10);
  const busy = fakePty("ok", false, PROMPT_QUIET_MS - 1);
  await pass(relay, g.id, busy.pty, now);
  assert.equal(busy.sent.length, 0);
  assert.equal(inbox(root, g.id).length, 1);
  const calm = fakePty("ok", false, PROMPT_QUIET_MS);
  await pass(relay, g.id, calm.pty, now);
  assert.equal(calm.sent.length, 1);
});

test("busy agy with a fresh lease: nothing is pasted and the message stays", async () => {
  const { relay, root, a, g, now } = setup({ busy: true, busyAt: Date.parse("2026-11-11T10:00:00Z") });
  relay.send(a.id, g.id, "later", 10);
  const { sent, pty } = fakePty("ok", false);
  await pass(relay, g.id, pty, now);
  assert.equal(sent.length, 0);
  assert.equal(inbox(root, g.id).length, 1);
});

test("fresh busy beats a quiet pty: the turn is live, so nothing is pasted and the pty is not asked", async () => {
  const { relay, a, g, now } = setup({ busy: true, busyAt: Date.parse("2026-11-11T10:00:00Z") });
  relay.send(a.id, g.id, "hello", 10);
  const { sent, pty, asks } = fakePty("ok", true, PTY_QUIET_MS * 2);
  await pass(relay, g.id, pty, now);
  assert.equal(sent.length, 0);
  assert.equal(asks(), 0);
});

test("an old busy flag plus a long quiet pty is an orphaned turn: lease dropped, message pasted", async () => {
  const { relay, a, g, now } = setup({ busy: true, busyAt: Date.parse("2026-11-11T10:00:00Z") - 15 * 60_000 });
  relay.send(a.id, g.id, "hello", 10);
  const { sent, pty } = fakePty("ok", true, PTY_QUIET_MS);
  await pass(relay, g.id, pty, now);
  assert.equal(sent.length, 1);
  assert.equal(relay.get(g.id)!.agent?.busy, true);
});

test("an old busy flag with only a 2s quiet pty is still a live turn, as measured on agy", async () => {
  const { relay, root, a, g, now } = setup({ busy: true, busyAt: Date.parse("2026-11-11T10:00:00Z") - 15 * 60_000 });
  relay.send(a.id, g.id, "hello", 10);
  const { sent, pty } = fakePty("ok", true, 2_000);
  await pass(relay, g.id, pty, now);
  assert.equal(sent.length, 0);
  assert.equal(inbox(root, g.id).length, 1);
});

test("a 15 minute old busy flag is asked about, not trusted: a spinning pty keeps the message", async () => {
  const { relay, root, a, g, now } = setup({ busy: true, busyAt: Date.parse("2026-11-11T10:00:00Z") - 15 * 60_000 });
  relay.send(a.id, g.id, "long build", 10);
  const { sent, pty, asks } = fakePty("ok", false, 100);
  await pass(relay, g.id, pty, now);
  assert.equal(asks(), 1);
  assert.equal(sent.length, 0);
  assert.equal(inbox(root, g.id).length, 1);
});

test("a pty that cannot be asked is never treated as quiet", async () => {
  const { relay, root, a, g, now } = setup({ busy: true, busyAt: Date.parse("2026-11-11T10:00:00Z") - 15 * 60_000 });
  relay.send(a.id, g.id, "unknown", 10);
  const { sent, pty } = fakePty("ok", true, undefined, "throw");
  await pass(relay, g.id, pty, now);
  assert.equal(sent.length, 0);
  assert.equal(inbox(root, g.id).length, 1);
});

test("a pty that only reports idle is not assumed to be quiet for long", async () => {
  const { relay, root, a, g, now } = setup({ busy: true, busyAt: Date.parse("2026-11-11T10:00:00Z") - 15 * 60_000 });
  relay.send(a.id, g.id, "idle only", 10);
  const { sent, pty } = fakePty("ok", true, 0);
  await pass(relay, g.id, pty, now);
  assert.equal(sent.length, 0);
  assert.equal(inbox(root, g.id).length, 1);
});

test("a draft in agy's input: the message stays and busy is unchanged", async () => {
  const { relay, root, a, g, now } = setup();
  relay.send(a.id, g.id, "x", 10);
  const { sent, pty } = fakePty("dirty");
  await pass(relay, g.id, pty, now);
  assert.equal(sent.length, 1);
  assert.equal(relay.get(g.id)!.agent?.busy, false);
  assert.equal(inbox(root, g.id).length, 1);
});

test("three queued messages are pasted as one batch, newest last", async () => {
  const { relay, root, a, g, now } = setup();
  for (const t of ["one", "two", "three"]) relay.send(a.id, g.id, t, 10);
  const { sent, pty } = fakePty();
  await pass(relay, g.id, pty, now);
  assert.equal(sent.length, 1);
  assert.ok(sent[0].startsWith("[relay from agent-relay, batch of 3 while you were busy, newest last]\n\n"));
  assert.ok(sent[0].indexOf("one") < sent[0].indexOf("two") && sent[0].indexOf("two") < sent[0].indexOf("three"));
  assert.equal(inbox(root, g.id).length, 0);
  assert.deepEqual(relay.status(a.id).map((r) => r.state), ["delivered", "delivered", "delivered"]);
});

test("messages handed to a continue are not pasted until that lease expires", async () => {
  const { relay, root, a, g, clock, now } = setup({ busy: false, pendingIds: ["x"], pendingAt: Date.parse("2026-11-11T10:00:00Z") });
  relay.send(a.id, g.id, "in flight", 10);
  const { sent, pty } = fakePty();
  await pass(relay, g.id, pty, now);
  assert.equal(sent.length, 0);
  assert.equal(inbox(root, g.id).length, 1);
  clock.t += PENDING_LEASE_MS + 1;
  await pass(relay, g.id, pty, now);
  assert.equal(sent.length, 1);
});

test("socketPty keeps the silence agy_pty measured, so a quiet queue is delivered", async () => {
  const net = process.getBuiltinModule("node:net") as typeof import("node:net");
  const os = process.getBuiltinModule("node:os") as typeof import("node:os");
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "relay-sock-"));
  const sock = path.join(dir, "pty.sock");
  const pasted: string[] = [];
  const server = net.createServer((c) => {
    let buf = "";
    c.on("data", (d) => {
      buf += d;
      const nl = buf.indexOf("\n");
      if (nl < 0) return;
      const req = JSON.parse(buf.slice(0, nl)) as { paste?: string };
      if (typeof req.paste === "string") {
        pasted.push(req.paste);
        c.end("ok\n");
      } else {
        c.end(JSON.stringify({ idle: true, dirty: false, silentMs: 9_000 }) + "\n");
      }
      buf = buf.slice(nl + 1);
    });
    c.on("error", () => {});
  });
  await new Promise<void>((r) => server.listen(sock, r));
  try {
    const { relay, root, a, g, now } = setup();
    relay.send(a.id, g.id, "queued while busy", 10);
    await pass(relay, g.id, socketPty(sock), now);
    assert.equal(pasted.length, 1);
    assert.ok(pasted[0].includes("queued while busy"));
    assert.equal(inbox(root, g.id).length, 0);
  } finally {
    server.close();
  }
});
