import { Relay, formatMessage, readConfig, type Watcher } from "./core.ts";
import { INSTRUCTIONS, TOOLS, peersText, runTool } from "./tools.ts";

export default function (pi: any, relayFactory: () => Relay = () => new Relay()) {
  let relay: Relay | undefined;
  let selfId = "";
  let watcher: Watcher | undefined;
  let lastCtx: any;

  const status = () => {
    if (!relay || !lastCtx?.hasUI) return;
    const n = relay.members(selfId).length;
    lastCtx.ui.setStatus("relay", `relay ${selfId}${n > 1 ? ` · ${n - 1} peer${n > 2 ? "s" : ""}` : ""}`);
  };

  const call = (name: string, args: Record<string, unknown>) => {
    if (!relay) throw new Error("agent-relay: session not started");
    const text = runTool(relay, selfId, readConfig().hopLimit, name, args);
    status();
    return { content: [{ type: "text", text }], details: {} };
  };

  for (const t of TOOLS) {
    pi.registerTool({
      name: t.name,
      label: t.name,
      description: t.description,
      parameters: t.inputSchema,
      promptGuidelines: t.name === "relay_send" ? [INSTRUCTIONS] : undefined,
      async execute(_id: string, params: Record<string, unknown>) {
        return call(t.name, params);
      },
    });
  }

  pi.registerCommand("relay", {
    description: "agent-relay: show ID and peers, `/relay link <id>`, `/relay unlink`",
    handler: async (args: string, ctx: any) => {
      if (!relay) return;
      const [cmd, ...rest] = args.trim().split(/\s+/);
      try {
        if (cmd === "link") relay.link(selfId, rest.join(" "));
        else if (cmd === "unlink") relay.unlink(selfId);
        ctx.ui.notify(peersText(relay, selfId), "info");
      } catch (e) {
        ctx.ui.notify(String((e as Error).message), "error");
      }
      status();
    },
  });

  pi.on("session_start", async (_e: any, ctx: any) => {
    lastCtx = ctx;
    relay = relayFactory();
    const rec = relay.register({
      kind: "pi",
      pid: process.pid,
      cwd: ctx.sessionManager?.getCwd?.() ?? ctx.cwd ?? process.cwd(),
      id: process.env.AGENT_RELAY_ID,
      name: ctx.sessionManager?.getSessionName?.() || undefined,
    });
    selfId = rec.id;
    watcher?.close();
    watcher = relay.watch(selfId, (m) => {
      pi.sendMessage({ customType: "relay", content: formatMessage(m), display: true }, { triggerTurn: true, deliverAs: "followUp" });
      relay!.markInjected(selfId, m.hop);
      status();
      return true;
    });
    status();
  });

  pi.on("input", async (e: any) => {
    if (relay && e.source === "interactive") relay.markHuman(selfId);
  });

  pi.on("session_shutdown", async () => {
    watcher?.close();
    watcher = undefined;
  });
}
