import { execFile } from "node:child_process";
import { readFileSync } from "node:fs";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

// Substituted at build time; the literal keeps the unbuilt source runnable.
const CLIENT_RAW = "@vpnRotate@";
const CLIENT = CLIENT_RAW.startsWith("@") ? "vpn-rotate" : CLIENT_RAW;
const ACTIONS = ["status", "cancel", "clear"];

const statePath = (): string => `${process.env.XDG_RUNTIME_DIR || "/tmp"}/vpn-rotate.json`;

// A flagged host IP while direct means the next call will likely be rate-limited.
const hostIpFlagged = (s: any): boolean =>
  typeof s.hostIpFlaggedUntil === "number" && s.hostIpFlaggedUntil > Date.now() / 1000;

// The state file is the only source; nmcli would report the host tunnel, not this one.
export function rowText(): string | undefined {
  let s: any;
  try {
    s = JSON.parse(readFileSync(statePath(), "utf8"));
  } catch {
    return undefined;
  }
  if (!s || typeof s !== "object") return undefined;
  if (s.rotating) return `vpn rotating${s.model ? ` for ${s.model}` : ""}…`;
  // refreshIn counts from the file's write time (ms); once it runs out the row shows the live mode again.
  const left = Math.ceil((s.refreshIn ?? 0) - (Date.now() / 1000 - (s.at ?? 0) / 1000));
  if (s.allRateLimited && left > 0) return `vpn all rate-limited${s.model ? ` for ${s.model}` : ""} (refresh in ${left}s)`;
  if (s.mode === "tunnel") {
    const where = [s.server, s.country].filter((part) => typeof part === "string" && part !== "").join(" ");
    const ip = typeof s.egressIp === "string" && s.egressIp !== "" ? ` ${s.egressIp}` : "";
    return `vpn tunnel${where ? ` ${where}` : ""}${ip}`;
  }
  return hostIpFlagged(s) ? "vpn direct (host ip flagged)" : "vpn direct";
}

function refresh(ctx: any) {
  try {
    if (ctx.hasUI) ctx.ui.setStatus("vpn", rowText());
  } catch {}
}

export default function vpnRow(pi: ExtensionAPI) {
  pi.registerCommand("vpn-rotate", {
    description: "VPN rotation service: /vpn-rotate [status|cancel|clear]",
    handler: async (args: any, ctx: any) => {
      const action = String(args ?? "").trim() || "status";
      if (!ACTIONS.includes(action)) return ctx.ui.notify(`Usage: /vpn-rotate [${ACTIONS.join("|")}]`, "warning");
      execFile(CLIENT, [action], { timeout: 10_000 }, (err, stdout, stderr) => {
        ctx.ui.notify(`${stdout}${stderr}`.trim() || (err ? err.message : "done"), err ? "error" : "info");
        refresh(ctx);
      });
    },
  });
  pi.on("session_start", (_event, ctx) => refresh(ctx));
  pi.on("turn_start", (_event, ctx) => refresh(ctx));
  pi.on("agent_settled", (_event, ctx) => refresh(ctx));
  // Loads after rate-limit-hook.ts, so this sees the rotation it just made.
  pi.on("message_end", (event, ctx) => {
    if ((event.message as any).stopReason === "error") refresh(ctx);
  });
}
