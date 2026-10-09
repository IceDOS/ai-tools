import { complete, type Model, type UserMessage } from "@earendil-works/pi-ai";
import type { ExtensionAPI, ExtensionContext } from "@earendil-works/pi-coding-agent";

// "provider/model-id" substituted at build time; "" uses the session model.
const MODEL: string = "@suggestModel@";

const MAX_MESSAGES = 8;
const MAX_CHARS = 1200;

const SYSTEM_PROMPT = `You predict the next message a user will type into a coding agent, given the recent transcript.

Rules:
- Output only the predicted message, max 12 words, no quotes, no explanation.
- Write it as the user would: terse, imperative, lowercase is fine.
- Prefer the obvious next step the assistant's last reply points to (apply, run, test, commit, rebuild, continue, answer its question).
- If the assistant asked a yes/no question, predict the likely answer ("yes", "go ahead").
- If there is no clear next step, output exactly NONE.`;

function textOf(content: unknown): string {
  if (typeof content === "string") return content;
  if (!Array.isArray(content)) return "";
  return content
    .map((c: any) => (c.type === "text" ? c.text : c.type === "toolCall" ? `[tool: ${c.name}]` : ""))
    .filter(Boolean)
    .join("\n");
}

function transcript(ctx: ExtensionContext): string | undefined {
  const lines: string[] = [];
  const branch = ctx.sessionManager.getBranch();
  for (let i = branch.length - 1; i >= 0 && lines.length < MAX_MESSAGES; i--) {
    const entry = branch[i] as any;
    if (entry.type !== "message") continue;
    const role = entry.message?.role;
    if (role !== "user" && role !== "assistant") continue;
    if (lines.length === 0 && (role !== "assistant" || entry.message.stopReason !== "stop")) return undefined;
    let text = textOf(entry.message.content).trim();
    if (!text) continue;
    // The tail of a long reply is where the next step usually sits.
    if (text.length > MAX_CHARS) text = "…" + text.slice(-MAX_CHARS);
    lines.unshift(`${role.toUpperCase()}: ${text}`);
  }
  return lines.length > 0 ? lines.join("\n\n") : undefined;
}

function clean(raw: string): string | undefined {
  const line = raw.trim().split("\n")[0]?.trim().replace(/^["'`]+|["'`]+$/g, "") ?? "";
  if (!line || /^none\.?$/i.test(line) || line.length > 120) return undefined;
  return line;
}

export default function promptSuggest(pi: ExtensionAPI) {
  let enabled = true;
  let suggestion: string | undefined;
  let inflight: AbortController | undefined;
  let warnedModel = false;

  // The patched TUI routes this widget key into the editor: ghost text in the
  // empty prompt, Tab inserts it. String widgets are all the daemon forwards.
  function show(ctx: ExtensionContext, text: string | undefined): void {
    suggestion = text;
    ctx.ui.setWidget("prompt-suggest", text ? [text] : undefined);
  }

  function clear(ctx: ExtensionContext): void {
    inflight?.abort();
    inflight = undefined;
    if (suggestion !== undefined) show(ctx, undefined);
  }

  function resolveModel(ctx: ExtensionContext): Model<any> | undefined {
    if (!MODEL) return ctx.model;
    const slash = MODEL.indexOf("/");
    const found = ctx.modelRegistry.find(MODEL.slice(0, slash), MODEL.slice(slash + 1));
    if (!found && !warnedModel) {
      warnedModel = true;
      ctx.ui.notify(`prompt-suggest: model ${MODEL} not found, using the session model`, "warning");
    }
    return found ?? ctx.model;
  }

  async function suggest(ctx: ExtensionContext): Promise<void> {
    const text = transcript(ctx);
    const model = resolveModel(ctx);
    if (!text || !model) return;
    const auth = await ctx.modelRegistry.getApiKeyAndHeaders(model);
    if (!auth.ok) return;

    const controller = new AbortController();
    inflight = controller;
    const message: UserMessage = {
      role: "user",
      content: [{ type: "text", text: `<transcript>\n${text}\n</transcript>\n\nPredict my next message.` }],
      timestamp: Date.now(),
    };
    try {
      const res = await complete(
        auth.requestModel ?? model,
        { systemPrompt: SYSTEM_PROMPT, messages: [message] },
        {
          apiKey: auth.apiKey,
          headers: auth.headers,
          signal: controller.signal,
          // Room for a short thought on reasoning models that cannot switch it off.
          maxTokens: 1024,
          ...(model.reasoning ? { reasoning: "minimal" as const } : {}),
        },
      );
      if (controller.signal.aborted || inflight !== controller || res.stopReason === "error") return;
      const next = clean(textOf(res.content));
      if (next && ctx.isIdle()) show(ctx, next);
    } catch {
      // A failed suggestion is not worth surfacing.
    } finally {
      if (inflight === controller) inflight = undefined;
    }
  }

  pi.on("session_start", (_event, ctx) => clear(ctx));
  pi.on("session_tree", (_event, ctx) => clear(ctx));
  pi.on("agent_start", (_event, ctx) => clear(ctx));

  pi.on("agent_end", (_event, ctx) => {
    if (!enabled || !ctx.hasUI || ctx.hasPendingMessages()) return;
    void suggest(ctx);
  });

  pi.on("session_shutdown", () => {
    inflight?.abort();
  });

  pi.registerCommand("suggest", {
    description: "Toggle next-prompt suggestions (on|off)",
    handler: async (args, ctx) => {
      const arg = args.trim().toLowerCase();
      enabled = arg === "on" ? true : arg === "off" ? false : !enabled;
      if (!enabled) clear(ctx);
      ctx.ui.notify(`Prompt suggestions ${enabled ? "on" : "off"}`, "info");
    },
  });
}
