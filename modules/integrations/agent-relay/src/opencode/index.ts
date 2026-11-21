import { CONTINUE_HINT } from "../hook.ts";
import { ID_RE, RELAY_PREFIX, Relay, formatMessage, writeAtomic, type Message, type SessionRecord, type Watcher } from "../core.ts";

const fs = process.getBuiltinModule("node:fs") as typeof import("node:fs");
const path = process.getBuiltinModule("node:path") as typeof import("node:path");

const PROBE_MS = 60_000;
const PROBE_MAX_MS = 600_000;

type Clock = { setTimeout: (fn: () => void, ms: number) => any; clearTimeout: (h: any) => void };

interface RootEntry {
  rootSessionID: string;
  relayId: string;
  rec: SessionRecord;
  watcher: Watcher;
  awaitingRead: Message[];
  inFlight: Message[];
}

export function createPlugin(
  relayFactory: () => Relay = () => new Relay(),
  probeMs = PROBE_MS,
  clock: Clock = { setTimeout, clearTimeout },
) {
  return {
    id: "agent-relay",
    setup: async (ctx: any) => {
      const relay = relayFactory();
      const rootsDir = path.join(relay.root, "opencode", "roots");
      const pinsDir = path.join(relay.root, "opencode", "pins");
      fs.mkdirSync(rootsDir, { recursive: true, mode: 0o700 });
      fs.mkdirSync(pinsDir, { recursive: true, mode: 0o700 });

      const rootSessions = new Map<string, RootEntry>();
      const rootCache = new Map<string, string>();

      let modelDown = false;
      let probing = false;
      let backoffMs = probeMs;
      let probeTimer: any;
      const abortCtrl = new AbortController();

      const clearProbe = () => {
        if (probeTimer !== undefined) clock.clearTimeout(probeTimer);
        probeTimer = undefined;
      };

      // Exponential backoff while the model or provider is failing.
      const holdModel = () => {
        if (modelDown) backoffMs = Math.min(backoffMs * 2, PROBE_MAX_MS);
        else backoffMs = probeMs;
        modelDown = true;
        probing = false;
        clearProbe();
        probeTimer = clock.setTimeout(() => {
          probeTimer = undefined;
          probing = true;
          for (const entry of rootSessions.values()) void entry.watcher.drain();
        }, backoffMs);
        probeTimer.unref?.();
      };

      const modelAlive = () => {
        modelDown = false;
        probing = false;
        backoffMs = probeMs;
        clearProbe();
      };

      // Walks parentID chain up to the root session.
      const resolveRoot = async (sessionID: string): Promise<string> => {
        if (rootCache.has(sessionID)) return rootCache.get(sessionID)!;
        const chain: string[] = [];
        let currId: string | undefined = sessionID;
        while (currId) {
          chain.push(currId);
          try {
            const s = await ctx.session.get(currId);
            if (!s?.parentID) {
              for (const id of chain) rootCache.set(id, currId);
              return currId;
            }
            if (rootCache.has(s.parentID)) {
              const root = rootCache.get(s.parentID)!;
              for (const id of chain) rootCache.set(id, root);
              return root;
            }
            currId = s.parentID;
          } catch {
            break;
          }
        }
        for (const id of chain) rootCache.set(id, sessionID);
        return sessionID;
      };

      const deliverForRoot = async (entry: RootEntry, _m: Message): Promise<boolean> => {
        if (entry.inFlight.length) return false;
        if (modelDown && !probing) return false;
        const batch = relay.pending(entry.relayId);
        if (!batch.length) return false;

        const head =
          batch.length > 1
            ? `${batch.length} relay messages arrived while you were busy; newest last, older ones may be outdated\n\n`
            : "";
        const text = `${CONTINUE_HINT}\n\n${head}${batch.map(formatMessage).join("\n\n")}`;
        entry.inFlight = batch;
        try {
          await ctx.session.synthetic({
            sessionID: entry.rootSessionID,
            text,
            description: "agent-relay",
            delivery: "steer",
          });
        } catch {
          entry.inFlight = [];
          return false;
        }

        for (const m of batch) relay.delivered(m);
        entry.awaitingRead = [...entry.awaitingRead, ...batch];
        entry.inFlight = [];
        if (probing) probing = false;
        relay.updateAgent(entry.relayId, { busy: true, busyAt: Date.now() });
        return false;
      };

      const ensureRootSession = async (rootSessionID: string): Promise<RootEntry> => {
        let entry = rootSessions.get(rootSessionID);
        if (entry) return entry;

        let sessionDir: string | undefined;
        try {
          const s = await ctx.session.get(rootSessionID);
          sessionDir = s?.location?.directory;
        } catch {}

        const pinFile = path.join(pinsDir, `${rootSessionID}.txt`);
        let pinnedId: string | undefined;
        try {
          const p = fs.readFileSync(pinFile, "utf8").trim();
          if (ID_RE.test(p)) pinnedId = p;
        } catch {}

        const rec = relay.register({
          kind: "opencode",
          pid: process.pid,
          cwd: sessionDir ?? ctx.location?.directory ?? process.cwd(),
          scope: rootSessionID,
          id: pinnedId,
        });

        if (pinnedId !== rec.id) {
          writeAtomic(pinFile, rec.id);
        }

        entry = {
          rootSessionID,
          relayId: rec.id,
          rec,
          watcher: undefined as any,
          awaitingRead: [],
          inFlight: [],
        };
        entry.watcher = relay.watch(rec.id, (m) => deliverForRoot(entry!, m));
        rootSessions.set(rootSessionID, entry);
        return entry;
      };

      // Startup sweep: remove stale pins, roots, and dead relay records.
      try {
        const list = await ctx.session.list();
        const validIds = new Set<string>(list.map((s: any) => s.id));
        if (fs.existsSync(pinsDir)) {
          for (const f of fs.readdirSync(pinsDir)) {
            if (!f.endsWith(".txt")) continue;
            const sid = f.slice(0, -4);
            if (!validIds.has(sid)) fs.rmSync(path.join(pinsDir, f), { force: true });
          }
        }
        if (fs.existsSync(rootsDir)) {
          for (const f of fs.readdirSync(rootsDir)) {
            if (!f.endsWith(".json")) continue;
            const sid = f.slice(0, -5);
            if (!validIds.has(sid)) fs.rmSync(path.join(rootsDir, f), { force: true });
          }
        }
        for (const s of relay.sessions()) {
          if (s.kind === "opencode" && s.pid === process.pid && s.scope && !validIds.has(s.scope)) {
            relay.unregister(s.id);
          }
        }
      } catch {}

      // Inject relay instructions into model requests for root sessions and subagents.
      const injectRelayId = async (event: any) => {
        if (!event?.sessionID) return;
        const rootId = await resolveRoot(event.sessionID);
        const entry = await ensureRootSession(rootId);
        const mapFile = path.join(rootsDir, `${event.sessionID}.json`);
        writeAtomic(mapFile, { rootSessionID: rootId, relayId: entry.relayId });
        if (event.system && Array.isArray(event.system)) {
          const text = `agent-relay is enabled. Your relay ID is ${entry.relayId}. Messages from peer agents or scripts start with [relay from ...]; reply with relay_send.`;
          if (!event.system.some((p: any) => typeof p?.text === "string" && p.text.includes("Your relay ID is"))) {
            event.system.splice(1, 0, { type: "text", text });
          }
        }
      };

      await ctx.session.hook("context", injectRelayId);
      await ctx.session.hook("compaction", injectRelayId);
      await ctx.session.hook("generate", injectRelayId);

      await ctx.session.hook("prompt", async (event: any) => {
        if (!event?.sessionID) return;
        const rootId = await resolveRoot(event.sessionID);
        if (event.sessionID !== rootId) return;
        const entry = await ensureRootSession(rootId);
        const text = event.prompt?.text ?? "";
        const isRelay = text.includes(RELAY_PREFIX) || /<channel\b/i.test(text);
        if (!isRelay) relay.markHuman(entry.relayId);
      });

      // Steer pending messages between tool calls.
      await ctx.tool.hook("execute.after", async (event: any) => {
        if (!event?.sessionID) return;
        const rootId = await resolveRoot(event.sessionID);
        const entry = rootSessions.get(rootId);
        if (entry) await entry.watcher.drain();
      });

      // Turn finished: mark read, and continue if new messages arrived.
      await ctx.session.hook("experimental.idle", async (event: any) => {
        if (!event?.sessionID) return;
        const rootId = await resolveRoot(event.sessionID);
        const entry = rootSessions.get(rootId);
        if (!entry) return;

        if (entry.awaitingRead.length) {
          for (const m of entry.awaitingRead) relay.read(m);
          entry.awaitingRead = [];
        }
        const batch = relay.pending(entry.relayId);
        if (batch.length && (!modelDown || probing)) {
          const head =
            batch.length > 1
              ? `${batch.length} relay messages arrived while you were busy; newest last, older ones may be outdated\n\n`
              : "";
          const text = `${CONTINUE_HINT}\n\n${head}${batch.map(formatMessage).join("\n\n")}`;
          try {
            await ctx.session.synthetic({
              sessionID: entry.rootSessionID,
              text,
              description: "agent-relay",
              delivery: "steer",
            });
            for (const m of batch) relay.delivered(m);
            entry.awaitingRead = [...batch];
            if (probing) probing = false;
            return;
          } catch {}
        }
        relay.updateAgent(entry.relayId, { busy: false, busyAt: Date.now() });
      });

      await ctx.session.hook("retry", async () => {
        holdModel();
      });

      const eventStream = ctx.event?.subscribe?.({
        types: ["session.created", "session.deleted", "session.execution.succeeded", "session.execution.failed"],
        signal: abortCtrl.signal,
      });
      if (eventStream && Symbol.asyncIterator in eventStream) {
        (async () => {
          try {
            for await (const event of eventStream) {
              if (event.type === "session.created") {
                const sid = (event as any).data?.sessionID;
                if (sid) {
                  const rootId = await resolveRoot(sid);
                  if (rootId === sid) {
                    const entry = await ensureRootSession(sid);
                    await entry.watcher.drain();
                  }
                }
              } else if (event.type === "session.deleted") {
                const sid = (event as any).data?.sessionID;
                if (sid) {
                  const entry = rootSessions.get(sid);
                  if (entry) {
                    entry.watcher.close();
                    relay.unregister(entry.relayId);
                    rootSessions.delete(sid);
                  }
                  rootCache.delete(sid);
                  fs.rmSync(path.join(pinsDir, `${sid}.txt`), { force: true });
                  fs.rmSync(path.join(rootsDir, `${sid}.json`), { force: true });
                  if (fs.existsSync(rootsDir)) {
                    for (const f of fs.readdirSync(rootsDir)) {
                      if (!f.endsWith(".json")) continue;
                      try {
                        const data = JSON.parse(fs.readFileSync(path.join(rootsDir, f), "utf8"));
                        if (data?.rootSessionID === sid) {
                          fs.rmSync(path.join(rootsDir, f), { force: true });
                        }
                      } catch {}
                    }
                  }
                }
              } else if (event.type === "session.execution.failed") {
                holdModel();
              } else if (event.type === "session.execution.succeeded") {
                modelAlive();
              }
            }
          } catch {}
        })();
      }

      return () => {
        abortCtrl.abort();
        for (const entry of rootSessions.values()) {
          entry.watcher.close();
        }
        rootSessions.clear();
        rootCache.clear();
        clearProbe();
        modelDown = false;
        probing = false;
      };
    },
  };
}

export default createPlugin();
