// alimit's shared usage bar below the editor, for the current model's provider.
// @alimit@ and @skip@ are substituted at build time.

import { execFile } from "node:child_process";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

const ALIMIT = "@alimit@";
// Providers another footer already shows (cost-footer's opencode-go plan rows).
const SKIP: string[] = JSON.parse('@skip@');
// pi provider id -> alimit provider
const PROVIDERS: Record<string, string> = {
  anthropic: "claude",
  "google-antigravity": "antigravity",
  "opencode-go": "opencode-go",
  ollama: "ollama",
  openrouter: "openrouter",
};
// alimit's own timer refreshes the DB; this only re-reads it.
const REFRESH_MS = 60_000;

export default function (pi: ExtensionAPI) {
  let lastCtx: any = null;
  let timer: ReturnType<typeof setInterval> | null = null;

  const refresh = (ctx: any, model?: any) => {
    if (!ctx?.hasUI) return;
    lastCtx = ctx;
    const provider = PROVIDERS[(model ?? ctx.model)?.provider ?? ""];
    if (!provider || SKIP.includes(provider)) {
      ctx.ui.setWidget("alimit", undefined);
      return;
    }
    execFile(ALIMIT, ["bar", "--provider", provider], { timeout: 5_000 }, (err, stdout) => {
      // An empty DB prints "alimit: no usage"; hide that instead of a box saying so.
      const text = err ? "" : stdout.trimEnd();
      const lines = text && !text.includes("alimit: no usage") ? text.split("\n") : undefined;
      ctx.ui.setWidget("alimit", lines, { placement: "belowEditor" });
    });
    if (!timer) {
      timer = setInterval(() => refresh(lastCtx), REFRESH_MS);
      timer.unref?.();
    }
  };

  pi.on("session_start", async (_e, ctx) => refresh(ctx));
  // ctx.model can still be the old one here; the event carries the new one.
  pi.on("model_select", async (e: any, ctx) => refresh(ctx, e.model));
  pi.on("turn_end", async (_e, ctx) => refresh(ctx));
  pi.on("session_shutdown", () => {
    if (timer) clearInterval(timer);
    timer = null;
  });
}
