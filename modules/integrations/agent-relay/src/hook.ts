import { RELAY_PREFIX, Relay, ancestors, formatMessage, pidAlive, type Message } from "./core.ts";
import { agentCwd, agentPid } from "./mcp-server.ts";

const fs = process.getBuiltinModule("node:fs") as typeof import("node:fs");

// A model reads a bare relay dump as an incident and investigates, so injected batches carry this.
export const CONTINUE_HINT =
  "New agent-relay messages from peer agents (not an error, not from the user). Read them and answer with relay_send:";

export function runHook(
  event: string,
  stdin: string,
  relay: Relay,
  pid: number,
  env: Record<string, string | undefined>,
  cwdOf: (pid: number) => string = agentCwd,
): string {
  // An agent can run inside another (claude in a pi bash tool), so only this hook's own kind counts.
  const rec = relay.findByPid(ancestors(pid), [event.startsWith("agy") ? "agy" : "claude"]);
  switch (event) {
    case "claude-prompt": {
      if (!rec) return "";
      let text = "";
      try {
        const payload = JSON.parse(stdin);
        text = typeof payload?.prompt === "string" ? payload.prompt : stdin;
      } catch {
        text = stdin;
      }
      const isRelay = text.includes(RELAY_PREFIX) || /<channel\b/i.test(text);
      if (!isRelay) relay.markHuman(rec.id);
      return "";
    }
    case "agy-session-start": {
      // A new session is idle by definition: the last turn may have ended without its Stop hook.
      relay.register({
        kind: "agy",
        pid,
        cwd: cwdOf(pid),
        id: env.AGENT_RELAY_ID,
        agent: { busy: false, busyAt: Date.now(), pendingIds: [], awaitingRead: [] },
      });
      return "{}";
    }
    case "agy-pre-invocation": {
      if (!rec) return "{}";
      let text = "";
      try {
        const payload = JSON.parse(stdin);
        text = typeof payload?.lastUserInput === "string" ? payload.lastUserInput : stdin;
      } catch {
        text = stdin;
      }
      // Patches merge under the record lock, so the watcher's own fields survive a hook write.
      const agent: Record<string, unknown> = { busy: true, busyAt: Date.now() };
      // PreInvocation proves agy took the continue agy-stop asked for, so those messages landed.
      const ack = (rec.agent?.pendingIds as string[] | undefined) ?? [];
      const seen: Message[] = ack.length ? [] : ((rec.agent?.awaitingRead as Message[] | undefined) ?? []);
      if (ack.length) {
        for (const m of relay.pending(rec.id)) if (ack.includes(m.id)) { relay.delivered(m); seen.push({ ...m, text: "" }); }
        agent.pendingIds = [];
      }
      // PreInvocation fires before every model step, so mail lands after each tool call, not after the turn.
      const fresh = relay.pending(rec.id).filter((m) => relay.delivered(m));
      for (const m of fresh) seen.push({ ...m, text: "" }); // read() only needs the refs; keep the record small
      // The watcher claims after its paste returns, so a batch it just pasted can still be pending here.
      const batch = fresh.filter((m) => !(m.short && text.includes(`msg ${m.short}`)));
      agent.awaitingRead = seen;
      // A relay batch header and a wrapped Stop reason both sit in the middle of the text, not the front.
      const isRelay = text.includes(RELAY_PREFIX) || /<channel\b/i.test(text);
      if (text !== rec.agent?.lastInput) {
        if (!isRelay) relay.markHuman(rec.id);
        agent.lastInput = text;
      }
      relay.updateAgent(rec.id, agent);
      if (!batch.length) return "{}";
      // agy 1.5.6 drops the whole hook result if systemMessage carries any metadata.
      return JSON.stringify({ injectSteps: [{ systemMessage: { systemMessage: `${CONTINUE_HINT}\n\n${batch.map(formatMessage).join("\n\n")}` } }] });
    }
    case "agy-stop": {
      if (!rec) return "{}";
      const seen = (rec.agent?.awaitingRead as Message[] | undefined) ?? [];
      for (const m of seen) relay.read(m); // the turn that saw them ended
      const batch = relay.pending(rec.id);
      const agent: Record<string, unknown> = { busy: false, busyAt: Date.now(), pendingIds: [], awaitingRead: [] };
      if (!batch.length) {
        relay.updateAgent(rec.id, agent);
        return "{}";
      }
      // A live watcher pastes the batch as a fresh prompt once agy is back at its prompt.
      const watcher = rec.agent?.watcher;
      if (typeof watcher === "number" && pidAlive(watcher)) {
        relay.updateAgent(rec.id, agent);
        return "{}";
      }
      // Keep the files until a PreInvocation acks them: a repeat beats a lost message.
      agent.pendingIds = batch.map((m) => m.id);
      agent.pendingAt = Date.now();
      relay.updateAgent(rec.id, agent);
      return JSON.stringify({ decision: "continue", reason: `${CONTINUE_HINT}\n\n${batch.map(formatMessage).join("\n\n")}` });
    }
    default:
      return "";
  }
}

if (import.meta.main) {
  const event = process.argv[2] ?? "";
  let stdin = "";
  try {
    stdin = fs.readFileSync(0, "utf8");
  } catch {}
  const kind = event.startsWith("agy") ? "agy" : "claude";
  try {
    process.stdout.write(runHook(event, stdin, new Relay(), agentPid(kind), process.env));
  } catch (e) {
    process.stderr.write(`agent-relay hook: ${(e as Error).message}\n`);
  }
}
