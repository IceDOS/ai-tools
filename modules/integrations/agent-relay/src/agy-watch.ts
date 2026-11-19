import { RELAY_PREFIX, Relay, ancestors, formatMessage, pidAlive, type Message, type SessionRecord } from "./core.ts";

const net = process.getBuiltinModule("node:net") as typeof import("node:net");

export type PasteResult = "ok" | "dirty" | "error";
export type Paste = (text: string) => Promise<PasteResult>;
export interface PtyStatus {
  idle: boolean;
  dirty: boolean;
  silentMs?: number;
}
// The pty answers whether agy is quiet, because a turn can end without its Stop hook.
export interface Pty {
  paste: Paste;
  status: () => Promise<PtyStatus>;
}

// A turn whose Stop hook never ran (abort, crash, hook timeout) leaves busy set.
// Past this age the hook is no longer trusted alone, but a long build can leave it stale while agy works.
export const BUSY_SETTLE_MS = 60_000;
// Measured on agy 1.5.6 (run2/run3): a paste into a busy agy aborts its turn, and a working turn
// redraws ~14x/s with a worst silence of 3.1s, so only a pty quiet for 30s counts as orphaned.
export const PTY_QUIET_MS = 30_000;
// A finished turn leaves agy at its prompt, which then sits quiet for seconds (10s measured); 3s is
// above the busy 2.45s and lets the end-of-turn redraw settle before the batch becomes a new prompt.
export const PROMPT_QUIET_MS = 3_000;
// agy-stop asks agy to continue; with no PreInvocation the continuation never started.
export const PENDING_LEASE_MS = 60_000;

const aged = (at: unknown, ms: number, now: Date): boolean =>
  typeof at !== "number" || now.getTime() - at > ms;

// agy registers from its own hooks, so its pid is the launched pid or one of its descendants.
export function findAgy(relay: Relay, agyPid: number, chain: (pid: number) => number[] = ancestors): SessionRecord | undefined {
  return relay.sessions().find((s) => s.kind === "agy" && chain(s.pid).includes(agyPid));
}

export function makeDeliver(
  relay: Relay,
  id: string,
  pty: Pty,
  now: () => Date = () => new Date(),
  watcherPid: number = process.pid,
): (m: Message) => Promise<boolean> {
  return async (m) => {
    const rec = relay.get(id);
    if (!rec) return false;
    const agent = { ...(rec.agent ?? {}) };
    // agy-stop leaves the batch alone while a live watcher is registered here.
    if (agent.watcher !== watcherPid) {
      relay.updateAgent(id, { watcher: watcherPid });
      agent.watcher = watcherPid;
    }
    const pending = agent.pendingIds as string[] | undefined;
    if (pending?.length && !aged(agent.pendingAt, PENDING_LEASE_MS, now())) return false;
    if (agent.busy) {
      // A PreInvocation fires on every model step, so a fresh busy flag means a live turn: never paste.
      if (!aged(agent.busyAt, BUSY_SETTLE_MS, now())) return false;
      // Old hook state proves nothing (a long tool call writes nothing), so the pty decides at any age.
      const st = await pty.status().catch(() => undefined);
      if ((st?.silentMs ?? 0) < PTY_QUIET_MS) return false;
      relay.updateAgent(id, { busy: false, busyAt: now().getTime() });
    }
    // agy must be back at its prompt before the batch can become a new user turn.
    const st = await pty.status().catch(() => undefined);
    if ((st?.silentMs ?? 0) < PROMPT_QUIET_MS) return false;
    const batch = relay.pending(id);
    if (!batch.some((b) => b.id === m.id)) return false;
    const text =
      batch.length > 1
        ? `${RELAY_PREFIX.trim()} agent-relay, batch of ${batch.length} while you were busy, newest last]\n\n${batch.map(formatMessage).join("\n\n")}`
        : formatMessage(m);
    if ((await pty.paste(text)) !== "ok") return false;
    const seen = batch.map((b) => ({ ...b, text: "" }));
    // The paste took time, so read the list fresh: a hook may have changed it meanwhile.
    const prev = (relay.get(id)?.agent?.awaitingRead as Message[] | undefined) ?? [];
    // Delivered here, read once the next Stop proves the turn that saw them ended.
    relay.updateAgent(id, { busy: true, busyAt: now().getTime(), awaitingRead: [...prev, ...seen] });
    for (const b of batch) relay.delivered(b);
    return true;
  };
}

function ask(sockPath: string, req: unknown): Promise<string> {
  return new Promise((resolve) => {
    let buf = "";
    const c = net.createConnection(sockPath);
    c.setTimeout(5000, () => c.destroy());
    c.on("connect", () => c.write(JSON.stringify(req) + "\n"));
    c.on("data", (d) => (buf += d));
    c.on("error", () => {});
    c.on("close", () => resolve(buf.trim()));
  });
}

// agy_pty.py measures the pty silence and sends it with the status. Dropping the field here made every
// paste decision read silentMs 0, so a watcher never delivered for as long as it lived.
export function parsePtyStatus(raw: string): PtyStatus | undefined {
  let r: Partial<PtyStatus> | undefined;
  try {
    r = JSON.parse(raw) as Partial<PtyStatus>;
  } catch {
    return undefined;
  }
  if (!r || typeof r !== "object") return undefined;
  return {
    idle: r.idle === true,
    dirty: r.dirty === true,
    silentMs: typeof r.silentMs === "number" && Number.isFinite(r.silentMs) ? r.silentMs : undefined,
  };
}

export function socketPty(sockPath: string): Pty {
  return {
    async paste(text) {
      const r = await ask(sockPath, { paste: text });
      return r === "ok" || r === "dirty" ? r : "error";
    },
    async status() {
      return parsePtyStatus(await ask(sockPath, { status: true })) ?? { idle: false, dirty: false };
    },
  };
}

async function main(agyPid: number, sockPath: string): Promise<void> {
  const relay = new Relay();
  let rec = findAgy(relay, agyPid);
  while (!rec) {
    if (!pidAlive(agyPid)) return;
    await new Promise((r) => setTimeout(r, 1000));
    rec = findAgy(relay, agyPid);
  }
  relay.updateAgent(rec.id, { watcher: process.pid });
  relay.watch(rec.id, makeDeliver(relay, rec.id, socketPty(sockPath)), 1000);
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
