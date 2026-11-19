import { readFileSync } from "node:fs";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

const statePath = (): string => `${process.env.XDG_RUNTIME_DIR || "/tmp"}/pi-vpn-rotate.json`;

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
  if (s.rotating) return "vpn rotating…";
  if (s.allRateLimited) return `vpn all rate-limited${s.refreshIn ? ` (refresh in ${s.refreshIn}s)` : ""}`;
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
  pi.on("session_start", (_event, ctx) => refresh(ctx));
  pi.on("turn_start", (_event, ctx) => refresh(ctx));
  pi.on("agent_settled", (_event, ctx) => refresh(ctx));
  // Loads after rate-limit-hook.ts, so this sees the rotation it just made.
  pi.on("message_end", (event, ctx) => {
    if ((event.message as any).stopReason === "error") refresh(ctx);
  });
}
