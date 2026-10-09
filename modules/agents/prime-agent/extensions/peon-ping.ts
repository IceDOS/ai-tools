// No terminal escapes: stdout is the TUI or the RPC/ACP pipe. @peonSh@ is substituted at build time.

import { spawn } from "node:child_process";
import type { StdioOptions } from "node:child_process";
import { existsSync } from "node:fs";
import type {
  ExtensionAPI,
  ExtensionContext,
} from "@earendil-works/pi-coding-agent";

// peon-ping's hm module always installs this; the wrapper has its own PATH.
const PEON_SH = "@peonSh@";

// Annotated, not inferred: an untyped array widens to string[], matching
// several spawn overloads at once and collapsing the return type to never.
const PEON_STDIO: StdioOptions = ["pipe", "ignore", "ignore"];

export default function (pi: ExtensionAPI) {
  // Not installed: stay silent, a warning would print on every session start.
  if (!existsSync(PEON_SH)) return;

  function firePeon(event: string, ctx: ExtensionContext): void {
    const payload = JSON.stringify({
      hook_event_name: event,
      notification_type: "",
      cwd: ctx.cwd,
      session_id: ctx.sessionManager.getSessionId(),
      permission_mode: "",
      source: "prime-agent",
    });

    try {
      const proc = spawn(PEON_SH, [], { stdio: PEON_STDIO, detached: true });
      // An unhandled 'error' on the child or its stdin would crash the host.
      proc.on("error", () => {});
      proc.stdin?.on("error", () => {});
      proc.stdin?.end(payload);
      proc.unref();
    } catch {
      // Never let a missing sound break a turn.
    }
  }

  pi.on("session_start", async (_event, ctx) => {
    firePeon("SessionStart", ctx);
  });

  pi.on("agent_start", async (_event, ctx) => {
    firePeon("UserPromptSubmit", ctx);
  });

  // No error variant: 0.7.4 fires no extension event on a failed turn.
  pi.on("agent_end", async (_event, ctx) => {
    firePeon("Stop", ctx);
  });
}
