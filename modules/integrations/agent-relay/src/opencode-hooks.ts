import { RELAY_PREFIX, Relay, ancestors, readConfig, type SessionRecord } from "./core.ts";
import { TOOLS, runTool } from "./tools.ts";

export function serverHooks(relay: Relay, tool: any, self: () => SessionRecord | undefined = () => relay.findByPid(ancestors(process.pid))) {
  const make = (name: string) => {
    const def = TOOLS.find((t) => t.name === name)!;
    const args = Object.fromEntries(Object.entries(def.inputSchema.properties).map(([k, v]) => [k, tool.schema.string().describe(v.description)]));
    return tool({
      description: def.description,
      args,
      async execute(a: Record<string, unknown>) {
        const me = self();
        if (!me) throw new Error("agent-relay: this opencode session is not registered yet");
        return runTool(relay, me.id, readConfig().hopLimit, name, a);
      },
    });
  };
  return {
    tool: Object.fromEntries(TOOLS.map((t) => [t.name, make(t.name)])),
    "chat.message": async (_input: unknown, output: { parts?: { type: string; text?: string }[] }) => {
      const text = (output.parts ?? []).filter((p) => p.type === "text").map((p) => p.text ?? "").join("");
      if (!text || text.startsWith(RELAY_PREFIX)) return;
      const me = self();
      if (me) relay.markHuman(me.id);
    },
  };
}
