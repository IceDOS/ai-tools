import { Relay, ancestors, formatMessage, pidAlive, type Message, type SessionRecord } from "./core.ts";

const net = process.getBuiltinModule("node:net") as typeof import("node:net");

export type PasteResult = "ok" | "dirty" | "error";
export type Paste = (text: string) => Promise<PasteResult>;

// agy registers from its own hooks, so its pid is the launched pid or one of its descendants.
export function findAgy(relay: Relay, agyPid: number, chain: (pid: number) => number[] = ancestors): SessionRecord | undefined {
  return relay.sessions().find((s) => s.kind === "agy" && chain(s.pid).includes(agyPid));
}

export function makeDeliver(relay: Relay, id: string, paste: Paste): (m: Message) => Promise<boolean> {
  return async (m) => {
    const rec = relay.get(id);
    if (!rec || rec.agent?.busy) return false;
    if ((await paste(formatMessage(m))) !== "ok") return false;
    relay.update(id, { agent: { ...relay.get(id)?.agent, busy: true } });
    relay.markInjected(id, m.hop);
    return true;
  };
}

export function socketPaste(sockPath: string): Paste {
  return (text) =>
    new Promise((resolve) => {
      let buf = "";
      const c = net.createConnection(sockPath);
      c.setTimeout(5000, () => c.destroy());
      c.on("connect", () => c.write(JSON.stringify({ paste: text }) + "\n"));
      c.on("data", (d) => (buf += d));
      c.on("error", () => {});
      c.on("close", () => {
        const r = buf.trim();
        resolve(r === "ok" || r === "dirty" ? r : "error");
      });
    });
}

async function main(agyPid: number, sockPath: string): Promise<void> {
  const relay = new Relay();
  let rec = findAgy(relay, agyPid);
  while (!rec) {
    if (!pidAlive(agyPid)) return;
    await new Promise((r) => setTimeout(r, 1000));
    rec = findAgy(relay, agyPid);
  }
  relay.watch(rec.id, makeDeliver(relay, rec.id, socketPaste(sockPath)), 1000);
  setInterval(() => {
    if (!pidAlive(agyPid)) process.exit(0);
  }, 2000);
}

if (import.meta.main) {
  const [pid, sock] = process.argv.slice(2);
  if (!Number(pid) || !sock) {
    process.stderr.write("usage: agent-relay-agy-watch <agy-pid> <socket>\n");
    process.exit(2);
  }
  void main(Number(pid), sock);
}
