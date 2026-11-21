// Terminal title: "<glyph> <title>", an idle glyph and a working spinner, like Claude Code.

import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { composeToolTimer } from "./tool-format.ts";

// Substituted at build time; the literals keep the unbuilt source runnable.
const IDLE_RAW = "@idleGlyph@";
const IDLE = IDLE_RAW.startsWith("@") ? "◍" : IDLE_RAW;
const WORKING_RAW = "@workingGlyphs@";
const WORKING: string[] = WORKING_RAW.startsWith("@") ? ["◴", "◷", "◶", "◵"] : JSON.parse(WORKING_RAW);

const TICK_MS = 250;
const MAX_CHARS = 80;
const PROMPT_CHARS = 2000;
const TOPIC_TOKENS = 256;
const TOPIC_TRIES = 2;
const TOPIC_SYSTEM =
  "Title the user's request in 3 to 6 words. Answer with the title only, no quotes and no final punctuation.";

type TextBlock = { type: string; text?: string };
type Content = string | readonly TextBlock[] | undefined;
type StartEvent = { messageType?: string; prompt?: string; message?: { content?: Content } };
// Session entries: a user turn is `message`, a relay or extension message is `custom_message`.
type HistoryEntry = { type?: string; message?: { role?: string; content?: Content }; content?: Content };
type Costed = { provider: string; id: string; cost: { input: number; output: number }; reasoning?: boolean };

function clip(text: string, max = MAX_CHARS): string {
  const flat = text.replace(/\s+/g, " ").trim();
  if (flat.length <= max) return flat;
  const cut = flat.slice(0, max - 1);
  const space = cut.lastIndexOf(" ");
  return `${(space > max / 2 ? cut.slice(0, space) : cut).trimEnd()}…`;
}

function normalizeTopic(raw: string): string | undefined {
  const line = raw.split("\n").find((l) => l.trim());
  if (!line) return undefined;
  const text = line
    .replace(/^[\s"'`#*_-]+/, "")
    .replace(/[\s"'`.,:;!]+$/, "")
    .replace(/\s+/g, " ")
    .trim();
  return text ? clip(text) : undefined;
}

// Cost 0 means free, then models that do not think, then provider and id.
function costOrder(a: Costed, b: Costed): number {
  return (
    a.cost.input + a.cost.output - (b.cost.input + b.cost.output) ||
    Number(a.reasoning === true) - Number(b.reasoning === true) ||
    a.provider.localeCompare(b.provider) ||
    a.id.localeCompare(b.id)
  );
}

// The topic sees the user's first prompt, so it never leaves the provider the user chose.
// Free models belong to other providers and can keep what they are sent.
function topicModels(models: readonly Costed[], current?: Costed): Costed[] {
  if (!current) return [];
  const same = models.filter((model) => model.provider === current.provider);
  return same.length > 0 ? [...same].sort(costOrder).slice(0, TOPIC_TRIES) : [current];
}

function textOf(content: readonly TextBlock[]): string {
  return content
    .filter((block) => block.type === "text")
    .map((block) => block.text ?? "")
    .join(" ")
    .trim();
}

function contentText(content: Content): string {
  if (typeof content === "string") return content;
  return content ? textOf(content) : "";
}

// A typed prompt arrives as `prompt`; a relay or extension message as `message.content`.
function promptText(event: StartEvent): string {
  return contentText(event.message?.content) || (event.prompt ?? "");
}

// A resumed session already has its first prompt in its history.
function firstUserPrompt(entries: readonly HistoryEntry[]): string | undefined {
  for (const entry of entries) {
    const custom = entry.type === "custom_message" ? entry.content : undefined;
    const user = entry.message?.role === "user" ? entry.message.content : undefined;
    const text = contentText(custom ?? user).trim();
    if (text) return text;
  }
  return undefined;
}

function compose(glyph: string, title: string, timer?: string): string {
  const suffix = timer ? ` · ${timer}` : "";
  const prefix = glyph ? `${glyph} ` : "";
  const room = MAX_CHARS - prefix.length - suffix.length;
  if (room <= 0) return `${prefix}${title}`.trim();
  const text = clip(title, room);
  return `${prefix}${text}${suffix}`.trim();
}

export const lib = { clip, compose, firstUserPrompt, normalizeTopic, promptText, textOf, topicModels };

export default function terminalTitle(pi: ExtensionAPI) {
  let ctx: any;
  let running = false;
  let prompts = 0;
  let tick = 0;
  let firstPrompt = "";
  let topic: string | undefined;
  // Bumps per session so a slow topic from a previous session cannot land in this one.
  let generation = 0;
  // The spinner needs a timer; an idle title only changes on events.
  let timer: ReturnType<typeof setInterval> | undefined;
  let shown: string | undefined;

  const working = () => running && prompts === 0;

  function title(): string {
    let name: string | undefined;
    try {
      name = pi.getSessionName();
    } catch {}
    return clip(name || topic || firstPrompt || "pi");
  }

  // pi's own title writer is blanked at build time, so this is the only caller of setTitle.
  const runningTools = new Map<string, { tool: string; start: number }>();

  function activeTimer(): string | undefined {
    let oldest: { tool: string; start: number } | undefined;
    for (const entry of runningTools.values()) {
      if (!oldest || entry.start < oldest.start) oldest = entry;
    }
    return oldest ? composeToolTimer(oldest.tool, Math.max(0, Date.now() - oldest.start)) : undefined;
  }

  function render() {
    if (!ctx || ctx.mode !== "tui") return;
    const frame = working() ? (WORKING[tick % WORKING.length] || IDLE) : IDLE;
    const timerText = working() ? activeTimer() : undefined;
    const next = compose(frame, title(), timerText);
    if (next === shown) return;
    try {
      ctx.ui.setTitle(next);
      shown = next;
    } catch {}
  }

  function stopTimer() {
    if (timer) clearInterval(timer);
    timer = undefined;
  }

  function sync() {
    if (working() && !timer) {
      timer = setInterval(() => {
        tick++;
        render();
      }, TICK_MS);
      timer.unref?.();
    } else if (!working()) {
      stopTimer();
    }
    render();
  }

  function reset(context: any) {
    ctx = context;
    running = false;
    prompts = 0;
    tick = 0;
    firstPrompt = "";
    topic = undefined;
    shown = undefined;
    runningTools.clear();
    generation++;
  }

  // A user session name wins over the generated topic, so skip the request entirely.
  function titleTopic(prompt: string) {
    try {
      if (pi.getSessionName()) return;
    } catch {}
    void generateTopic(prompt, generation);
  }

  async function generateTopic(prompt: string, gen: number) {
    try {
      const models = topicModels(ctx?.modelRegistry?.getAvailable?.() ?? [], ctx?.model);
      for (const model of models) {
        const stream = ctx.modelRegistry.streamSimple(
          model,
          {
            systemPrompt: TOPIC_SYSTEM,
            messages: [{ role: "user", content: prompt.slice(0, PROMPT_CHARS), timestamp: Date.now() }],
          },
          // A thinking model can spend a small budget on reasoning and return no text at all.
          { maxTokens: TOPIC_TOKENS, reasoning: "off" },
        );
        const reply = await stream.result();
        if (gen !== generation) return;
        const generated = reply.stopReason === "error" ? undefined : normalizeTopic(textOf(reply.content));
        if (!generated) continue;
        topic = generated;
        render();
        return;
      }
    } catch {
      // The first prompt is the fallback title.
    }
  }

  pi.on("session_start", (_event, context) => {
    stopTimer();
    reset(context);
    try {
      firstPrompt = firstUserPrompt(context?.sessionManager?.getBranch?.() ?? []) ?? "";
    } catch {
      firstPrompt = "";
    }
    sync();
    // The resumed title is the old prompt, so it gets the same topic call as a first turn.
    if (ctx.mode === "tui" && firstPrompt) titleTopic(firstPrompt);
  });

  pi.on("session_info_changed", (_event, context) => {
    ctx = context;
    sync();
  });

  pi.on("agent_start", (_event, context) => {
    ctx = context;
    running = true;
    tick = 0;
    sync();
  });

  pi.on("agent_settled", (_event, context) => {
    ctx = context;
    running = false;
    prompts = 0;
    runningTools.clear();
    sync();
  });

  pi.on("tool_execution_start", (event: any) => {
    runningTools.set(event.toolCallId, { tool: event.toolName, start: Date.now() });
    render();
  });

  pi.on("tool_execution_end", (event: any) => {
    runningTools.delete(event.toolCallId);
    render();
  });

  // A blocking prompt means pi is waiting on the user, so show the idle glyph.
  pi.on("ui_prompt_start", (_event, context) => {
    ctx = context;
    prompts++;
    sync();
  });

  pi.on("ui_prompt_end", (_event, context) => {
    ctx = context;
    prompts = Math.max(0, prompts - 1);
    sync();
  });

  // A relay or extension turn starts a run without an input event, so the run start is the hook.
  pi.on("before_agent_start", (event, context) => {
    ctx = context;
    // The title is terminal-only, so no provider call in print, json or rpc mode.
    if (ctx.mode !== "tui" || firstPrompt) return;
    const prompt = promptText(event).trim();
    if (!prompt) return;
    firstPrompt = prompt;
    titleTopic(prompt);
  });

  pi.on("session_shutdown", (event) => {
    stopTimer();
    generation++;
    if (event.reason !== "quit") return;
    try {
      ctx?.ui?.setTitle("");
      shown = "";
    } catch {}
  });
}
