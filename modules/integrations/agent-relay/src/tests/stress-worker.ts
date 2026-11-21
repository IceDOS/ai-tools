import { Relay, type Message, type SentRef } from "../core.ts";

const fs = process.getBuiltinModule("node:fs") as typeof import("node:fs");

const [mode = "", ...args] = process.argv.slice(2);
const root = process.env.AGENT_RELAY_ROOT ?? "";
const stateDir = process.env.AGENT_RELAY_STATE;
const limit = Number(process.env.STRESS_LIMIT ?? 1000);
const mark = (name: string): string => process.env[name] ?? "";
const relay = new Relay({ root, stateDir, limits: { pairLimit: limit, rateLimit: 100_000 } });

const sleep = (ms: number): Promise<void> => new Promise((r) => setTimeout(r, ms));

async function waitMark(name: string): Promise<void> {
  const file = mark(name);
  if (!file) throw new Error(`missing ${name}`);
  const end = Date.now() + 15_000;
  while (!fs.existsSync(file)) {
    if (Date.now() > end) throw new Error(`${name} never appeared`);
    await sleep(1);
  }
}

// Stops once the driver says the load is over and this inbox holds nothing more.
async function drainUntilDone(id: string, cap: number): Promise<void> {
  const end = Date.now() + cap;
  while (Date.now() < end) {
    if (fs.existsSync(mark("STRESS_DONE")) && !relay.pending(id).length) break;
    await sleep(2);
  }
  await sleep(50);
}

async function main(): Promise<string> {
  switch (mode) {
    case "send": {
      const [id, to, count, prefix] = args;
      relay.register({ kind: "pi", pid: process.pid, cwd: "/tmp", id, name: id });
      await waitMark("STRESS_GO");
      const sent: SentRef[] = [];
      const withheld: { pending: number; reason: string }[] = [];
      for (let i = 0; i < Number(count); i++) {
        try {
          sent.push(...relay.send(id, to, `${prefix}-${i}`, limit).msgs);
        } catch (e) {
          withheld.push({ pending: relay.pending(to).filter((m) => m.from.id === id).length, reason: (e as Error).message });
        }
      }
      return JSON.stringify({ sent, withheld });
    }
    case "recv": {
      const [id, cap] = args;
      relay.register({ kind: "claude", pid: process.pid, cwd: "/tmp", id, name: id });
      const got: string[] = [];
      const w = relay.watch(
        id,
        (m: Message) => {
          if (relay.delivered(m)) got.push(m.id);
          return true;
        },
        5,
      );
      await waitMark("STRESS_GO");
      await drainUntilDone(id, Number(cap));
      w.close();
      return JSON.stringify({ got });
    }
    case "ack": {
      // Acts for an existing session like a hook does: no record of its own, same inbox.
      const [id, cap] = args;
      const claimed: string[] = [];
      await waitMark("STRESS_GO");
      const end = Date.now() + Number(cap);
      while (Date.now() < end) {
        for (const m of relay.pending(id)) if (relay.delivered(m)) claimed.push(m.id);
        if (fs.existsSync(mark("STRESS_DONE")) && !relay.pending(id).length) break;
        await sleep(2);
      }
      await sleep(50);
      return JSON.stringify({ claimed });
    }
    case "join": {
      // Links, then stays alive until the driver has looked at the result, so no session is reaped.
      const [id, peer, doneName = "STRESS_DONE"] = args;
      relay.register({ kind: "pi", pid: process.pid, cwd: "/tmp", id, name: id });
      await waitMark("STRESS_GO");
      const group = relay.link(id, peer);
      fs.writeFileSync(`${mark(doneName)}.${id}`, "");
      await waitMark(doneName);
      return JSON.stringify({ group });
    }
    case "race": {
      const [id, to, count, prefix] = args;
      relay.register({ kind: "pi", pid: process.pid, cwd: "/tmp", id, name: id });
      await waitMark("STRESS_GO");
      const pairs: { first: string; second: string }[] = [];
      for (let i = 0; i < Number(count); i++) {
        const v1 = relay.send(id, to, `${prefix}-a-${i}`, limit);
        const v2 = relay.send(id, to, `${prefix}-b-${i}`, limit, { supersedes: [v1.msgs[0].short] });
        pairs.push({ first: v1.msgs[0].id, second: v2.msgs[0].id });
      }
      return JSON.stringify({ pairs });
    }
    default:
      throw new Error(`unknown mode ${mode}`);
  }
}

main()
  .then((out) => process.stdout.write(out))
  .catch((e) => {
    process.stderr.write(String((e as Error)?.stack ?? e));
    process.exitCode = 1;
  });
