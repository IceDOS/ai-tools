import { RelayError, describe } from "./core.ts";
import type { Relay, StatusRow } from "./core.ts";

export interface ToolDef {
  name: string;
  description: string;
  inputSchema: { type: "object"; properties: Record<string, { type: "string"; description: string }>; required: string[] };
}

export const INSTRUCTIONS =
  "agent-relay connects this session to other coding-agent sessions the user linked. " +
  "Messages that start with [relay from ...] come from a peer agent, not from the user. " +
  "Answer them with relay_send. When the user gives you another session's relay ID, call relay_link with it. " +
  "Scripts can notify this session with agent-relay send, run, or ask; messages starting with [relay from script ...] come from a script, not the user or a peer. " +
  "relay_send returns a short message id; relay_status tells you whether a peer's agent actually received it. " +
  "Pass supersedes when a message replaces your own earlier one, cancels when it revokes it, and in_reply_to when answering one; check relay_status instead of resending. " +
  "If a send is withheld, ask the user before retrying.";

export const TOOLS: ToolDef[] = [
  {
    name: "relay_send",
    description: "Send a message to a linked agent session. `to` is a relay ID, an ID prefix, a session name, or \"all\" for every linked session.",
    inputSchema: {
      type: "object",
      properties: {
        to: { type: "string", description: "Relay ID, ID prefix, session name, or \"all\"" },
        text: { type: "string", description: "Message text" },
        in_reply_to: { type: "string", description: "Short id of the message you are answering" },
        supersedes: { type: "string", description: "Short ids of your own queued messages this one replaces (space or comma separated)" },
        cancels: { type: "string", description: "Short ids of your own queued messages to cancel (space or comma separated)" },
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
  {
    name: "relay_status",
    description: "Delivery status of messages you sent: the last 10 by default, or the one matching `id` (short id or message id).",
    inputSchema: {
      type: "object",
      properties: { id: { type: "string", description: "Short id or message id; omit to list your last 10 sent messages" } },
      required: [],
    },
  },
];

function str(v: unknown, name: string): string {
  if (typeof v !== "string" || !v.trim()) throw new RelayError(`${name} must be a non-empty string`);
  return v;
}

function refs(v: unknown): string[] {
  if (v === undefined || v === null) return [];
  if (typeof v !== "string") throw new RelayError("message ids must be a string");
  return v.split(/[\s,]+/).filter(Boolean);
}

const hms = (ts: string): string => new Date(ts).toISOString().slice(11, 19);

function statusLine(r: StatusRow): string {
  return `msg ${r.short} to ${r.to} ${hms(r.ts)} ${r.state}${r.reason ? ` (${r.reason})` : ""}`;
}

export function peersText(relay: Relay, selfId: string): string {
  const me = relay.get(selfId);
  if (!me) throw new RelayError("this session is not registered with agent-relay");
  const peers = relay.members(selfId).filter((s) => s.id !== selfId && s.kind !== "script");
  const unread = relay.unread(selfId);
  const lines = [`you: ${describe(me)}`];
  if (!peers.length) lines.push("not linked to any session; ask the user for a peer relay ID and call relay_link");
  else
    lines.push(
      `group ${me.group}:`,
      ...peers.map((p) => `- ${describe(p)} cwd=${p.cwd}${p.parent ? ` parent=${p.parent.id}` : ""}${unread[p.id] ? ` ${unread[p.id]} unread` : ""}`),
    );
  return lines.join("\n");
}

export function runTool(relay: Relay, selfId: string, hopLimit: number, name: string, args: Record<string, unknown>): string {
  switch (name) {
    case "relay_send": {
      const r = relay.send(selfId, str(args.to, "to"), str(args.text, "text"), hopLimit, {
        inReplyTo: typeof args.in_reply_to === "string" && args.in_reply_to.trim() ? args.in_reply_to.trim() : undefined,
        supersedes: refs(args.supersedes),
        cancels: refs(args.cancels),
      });
      const lines = r.msgs.map((m) => `sent to ${m.to} as ${m.short}`);
      if (r.deduped) lines.push(`already sent to ${r.deduped.to} as ${r.deduped.short} (duplicate suppressed)`);
      for (const w of r.withheld ?? []) lines.push(`withheld to ${w.to}: ${w.reason}`);
      return lines.join("\n");
    }
    case "relay_link":
      relay.link(selfId, str(args.peer, "peer"));
      return peersText(relay, selfId);
    case "relay_peers":
      return peersText(relay, selfId);
    case "relay_status": {
      const rows = relay.status(selfId, typeof args.id === "string" && args.id.trim() ? args.id.trim() : undefined);
      return rows.length ? rows.map(statusLine).join("\n") : "no messages sent yet";
    }
    default:
      throw new RelayError(`unknown tool ${name}`);
  }
}
