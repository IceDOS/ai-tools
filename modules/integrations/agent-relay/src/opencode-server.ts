import { tool } from "@opencode-ai/plugin";
import { Relay } from "./core.ts";
import { serverHooks } from "./opencode-hooks.ts";

export const AgentRelay = async () => serverHooks(new Relay(), tool);
export default { id: "agent-relay", setup: AgentRelay };
