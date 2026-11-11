import { Relay, formatMessage, type Watcher } from "./core.ts";

export function startTui(api: any, relay: Relay = new Relay()): Watcher {
  const rec = relay.register({ kind: "opencode", pid: process.pid, cwd: process.cwd(), id: process.env.AGENT_RELAY_ID });
  const toasted = new Set<string>();
  api.ui?.toast?.({ message: `agent-relay ID ${rec.id}` });
  const w = relay.watch(rec.id, async (m) => {
    const r = api.route?.current ?? api.ui?.router?.current?.();
    const isSession = r?.name === "session" || r?.type === "session";
    const sessionID = r?.params?.sessionID ?? r?.sessionID;
    if (!isSession || !sessionID) {
      if (!toasted.has(m.id)) {
        toasted.add(m.id);
        api.ui?.toast?.({ message: `agent-relay: message from ${m.from.kind} ${m.from.id} waiting, open a session` });
      }
      return false;
    }
    const text = formatMessage(m);
    if (api.client?.session?.promptAsync) {
      await api.client.session.promptAsync({ sessionID, parts: [{ type: "text", text }] });
    } else if (api.client?.session?.prompt) {
      await api.client.session.prompt({ sessionID, parts: [{ type: "text", text }] });
    }
    relay.markInjected(rec.id, m.hop);
    return true;
  });
  api.lifecycle?.onDispose?.(() => w.close());
  return w;
}

const tui = async (api: any) => {
  startTui(api);
};

export default { id: "agent-relay", tui };
