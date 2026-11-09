import { existsSync, readFileSync } from "node:fs";
import { execFileSync } from "node:child_process";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

const STATE = `${process.env.XDG_RUNTIME_DIR || "/tmp"}/pi-vpn-rotate.json`;
const CACHE_TTL_MS = 5000;
const NMCLI = existsSync("/run/current-system/sw/bin/nmcli") ? "/run/current-system/sw/bin/nmcli" : "nmcli";
let cachedServer: string | undefined;
let lastServerCheck = 0;

function isTunnelUp(): boolean {
  return existsSync("/sys/class/net/proton0");
}

function currentTunnelServer(): string | undefined {
  const now = Date.now();
  if (now - lastServerCheck < CACHE_TTL_MS) {
    return cachedServer;
  }
  lastServerCheck = now;
  try {
    const out = execFileSync(NMCLI, ["-t", "-f", "NAME,DEVICE", "connection", "show", "--active"], {
      encoding: "utf8",
      timeout: 500,
    });
    for (const line of out.split("\n")) {
      const [name, dev] = line.split(":");
      if (dev === "proton0") {
        const match = name.match(/ProtonVPN\s+(\S+)/i);
        cachedServer = match ? match[1] : name;
        return cachedServer;
      }
    }
  } catch {}
  cachedServer = undefined;
  return undefined;
}

function rowText(): string | undefined {
  if (!isTunnelUp()) {
    cachedServer = undefined;
    lastServerCheck = 0;
    return undefined;
  }
  let s: any = null;
  try {
    s = JSON.parse(readFileSync(STATE, "utf8"));
  } catch {}
  if (s?.at && s.at > lastServerCheck) {
    lastServerCheck = 0;
  }
  const active = currentTunnelServer();
  const stateServer = s?.server || s?.country;
  if (active && stateServer && active !== stateServer) {
    return `vpn ${active}`;
  }
  if (stateServer) {
    return `vpn ${stateServer}${s.location ? ` (${s.location})` : ""}${s.egressIp ? ` ${s.egressIp}` : ""}`;
  }
  return active ? `vpn ${active}` : "vpn connected";
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
