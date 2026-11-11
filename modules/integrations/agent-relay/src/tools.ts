import { RelayError, describe } from "./core.ts";
import type { Relay } from "./core.ts";

export interface ToolDef {
  name: string;
  description: string;
  inputSchema: { type: "object"; properties: Record<string, { type: "string"; description: string }>; required: string[] };
}

export const INSTRUCTIONS =
  "agent-relay connects this session to other coding-agent sessions the user linked. " +
  "Messages that start with [relay from ...] come from a peer agent, not from the user. " +
  "Answer them with relay_send. When the user gives you another session's relay ID, call relay_link with it.";

export const TOOLS: ToolDef[] = [
  {
    name: "relay_send",
    description: "Send a message to a linked agent session. `to` is a relay ID, an ID prefix, a session name, or \"all\" for every linked session.",
    inputSchema: {
      type: "object",
      properties: {
        to: { type: "string", description: "Relay ID, ID prefix, session name, or \"all\"" },
        text: { type: "string", description: "Message text" },
      },
      required: ["to", "text"],
    },
  },
  {
    name: "relay_link",
    description: "Link this session with another agent session by its relay ID (or prefix or name). Both sessions join one group.",
    inputSchema: {
      type: "object",
      properties: { peer: { type: "string", description: "Relay ID, ID prefix or session name of the other session" } },
      required: ["peer"],
    },
  },
  {
    name: "relay_peers",
    description: "Show this session's relay ID and the sessions linked with it.",
    inputSchema: { type: "object", properties: {}, required: [] },
  },
];

function str(v: unknown, name: string): string {
  if (typeof v !== "string" || !v.trim()) throw new RelayError(`${name} must be a non-empty string`);
  return v;
}

export function peersText(relay: Relay, selfId: string): string {
  const me = relay.get(selfId);
  if (!me) throw new RelayError("this session is not registered with agent-relay");
  const peers = relay.members(selfId).filter((s) => s.id !== selfId);
  const lines = [`you: ${describe(me)}`];
  if (!peers.length) lines.push("not linked to any session; ask the user for a peer relay ID and call relay_link");
  else lines.push(`group ${me.group}:`, ...peers.map((p) => `- ${describe(p)} cwd=${p.cwd}`));
  return lines.join("\n");
}

export function runTool(relay: Relay, selfId: string, hopLimit: number, name: string, args: Record<string, unknown>): string {
  switch (name) {
    case "relay_send":
      return `sent to ${relay.send(selfId, str(args.to, "to"), str(args.text, "text"), hopLimit).join(", ")}`;
    case "relay_link":
      relay.link(selfId, str(args.peer, "peer"));
      return peersText(relay, selfId);
    case "relay_peers":
      return peersText(relay, selfId);
    default:
      throw new RelayError(`unknown tool ${name}`);
  }
}
