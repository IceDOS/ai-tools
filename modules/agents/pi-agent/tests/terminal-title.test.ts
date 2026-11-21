import { mock, test } from "node:test";
import assert from "node:assert/strict";
import terminalTitle, { lib } from "../extensions/terminal-title.ts";

test("clip flattens whitespace and cuts on a word boundary", () => {
  assert.equal(lib.clip("  pi   agent\n"), "pi agent");
  assert.equal(lib.clip("a".repeat(80)), "a".repeat(80));
  const long = lib.clip("word ".repeat(40));
  assert.ok(long.length <= 80, long);
  assert.ok(long.endsWith("…"), long);
  assert.ok(!long.includes("… "), long);
});

test("normalizeTopic keeps the first line as a bare title", () => {
  assert.equal(lib.normalizeTopic('"Fix the terminal title."'), "Fix the terminal title");
  assert.equal(lib.normalizeTopic("Title here\nwith explanation"), "Title here");
  assert.equal(lib.normalizeTopic("  \n\n"), undefined);
  assert.equal(lib.normalizeTopic("---"), undefined);
});

const model = (provider: string, id: string, input: number, output: number) => ({
  provider,
  id,
  cost: { input, output },
});

test("the topic models stay on the provider the user picked, cheapest first", () => {
  const current = model("b", "big", 5, 15);
  const small = model("b", "small", 0, 0);
  const mid = model("b", "mid", 1, 1);
  assert.deepEqual(lib.topicModels([], undefined), []);
  assert.deepEqual(lib.topicModels([model("a", "free", 0, 0)], undefined), []);
  // Nothing else on that provider: the session model itself, never a cheaper one elsewhere.
  assert.deepEqual(lib.topicModels([model("a", "free", 0, 0)], current).map((m) => m.id), ["big"]);
  assert.deepEqual(lib.topicModels([model("a", "free", 0, 0), mid, small, current], current).map((m) => m.id), [
    "small",
    "mid",
  ]);
  // A thinking model can spend the title budget on reasoning and return no text.
  const thinking = { ...model("b", "think", 0, 0), reasoning: true };
  assert.deepEqual(lib.topicModels([thinking, small], current).map((m) => m.id), ["small", "think"]);
});

test("compose puts one space between glyph and title", () => {
  assert.equal(lib.compose("◍", "pi"), "◍ pi");
  assert.equal(lib.compose("", "pi"), "pi");
});

test("textOf joins only text blocks", () => {
  assert.equal(
    lib.textOf([{ type: "text", text: "Hello" }, { type: "thinking", text: "hmm" }, { type: "text", text: "There" }]),
    "Hello There",
  );
});

type Handler = (event: any, ctx: any) => unknown;

function harness(options: { name?: string; models?: any[]; model?: any; reply?: string; history?: any[] } = {}) {
  const handlers = new Map<string, Handler[]>();
  const titles: string[] = [];
  const streamed: any[] = [];
  let name = options.name;
  const pi = {
    on(event: string, handler: Handler) {
      handlers.set(event, [...(handlers.get(event) ?? []), handler]);
      return () => {};
    },
    getSessionName: () => name,
    setSessionName: (next: string) => {
      name = next;
    },
  };
  const ctx = {
    mode: "tui",
    model: options.model,
    sessionManager: { getBranch: () => options.history ?? [] },
    ui: { setTitle: (title: string) => titles.push(title) },
    modelRegistry: {
      getAvailable: () => options.models ?? [],
      streamSimple: (chosen: any, _context: any, requestOptions: any) => {
        streamed.push(chosen);
        streamed.at(-1).requestOptions = requestOptions;
        return { result: async () => ({ stopReason: "end", content: [{ type: "text", text: options.reply ?? "" }] }) };
      },
    },
  };
  const emit = (event: string, payload: any = {}) => {
    for (const handler of handlers.get(event) ?? []) handler({ type: event, ...payload }, ctx);
  };
  terminalTitle(pi as any);
  return { emit, pi, streamed, titles };
}

// Counts the intervals the extension keeps alive, which mocks alone cannot show.
function liveTimers() {
  const realSet = globalThis.setInterval;
  const realClear = globalThis.clearInterval;
  const live = new Set<any>();
  let created = 0;
  globalThis.setInterval = ((fn: any, ms: number) => {
    created++;
    const id = realSet(fn, ms);
    live.add(id);
    return id;
  }) as any;
  globalThis.clearInterval = ((id: any) => {
    realClear(id);
    live.delete(id);
  }) as any;
  return {
    created: () => created,
    live: () => live.size,
    restore() {
      globalThis.setInterval = realSet;
      globalThis.clearInterval = realClear;
    },
  };
}

test("the title follows the agent state and the spinner frames", () => {
  mock.timers.enable({ apis: ["setInterval"] });
  const timers = liveTimers();
  try {
    const { emit, titles } = harness();
    emit("session_start", { reason: "startup" });
    assert.equal(titles.at(-1), "◍ pi");
    assert.equal(timers.live(), 0, "an idle title needs no timer");

    emit("agent_start");
    assert.equal(titles.at(-1), "◴ pi");
    assert.equal(timers.live(), 1);
    mock.timers.tick(250);
    assert.equal(titles.at(-1), "◷ pi");

    // A blocking prompt means pi waits on the user, so the spinner stops.
    emit("ui_prompt_start", { kind: "confirm" });
    assert.equal(titles.at(-1), "◍ pi");
    assert.equal(timers.live(), 0);
    mock.timers.tick(1000);
    assert.equal(titles.at(-1), "◍ pi");

    emit("ui_prompt_end", { kind: "confirm" });
    assert.equal(timers.live(), 1);
    mock.timers.tick(250);
    assert.equal(titles.at(-1), "◶ pi");

    emit("agent_settled", { aborted: false });
    assert.equal(titles.at(-1), "◍ pi");
    assert.equal(timers.live(), 0);

    emit("session_shutdown", { reason: "quit" });
    assert.equal(titles.at(-1), "");
    mock.timers.tick(1000);
    assert.equal(titles.at(-1), "");
  } finally {
    timers.restore();
    mock.timers.reset();
  }
});

test("an unchanged idle title is not written again", () => {
  mock.timers.enable({ apis: ["setInterval"] });
  const timers = liveTimers();
  try {
    const { emit, titles } = harness();
    emit("session_start", { reason: "startup" });
    const written = titles.length;

    emit("session_info_changed", {});
    emit("session_info_changed", {});
    mock.timers.tick(4000);
    assert.equal(titles.length, written);
    assert.equal(titles.filter((title) => title === "◍ pi").length, 1);

    emit("agent_settled", { aborted: false });
    assert.equal(titles.length, written);
    assert.equal(timers.created(), 0);
  } finally {
    timers.restore();
    mock.timers.reset();
  }
});

test("a session name wins over a generated topic and the first prompt", async () => {
  const free = model("a", "free", 0, 0);
  const paid = model("b", "paid", 5, 15);
  const { emit, streamed, titles } = harness({ models: [free, paid], model: paid, reply: '"Name the session."' });
  emit("session_start", { reason: "startup" });
  emit("before_agent_start", { messageType: "user", prompt: "please fix the terminal title" });
  await new Promise((resolve) => setTimeout(resolve, 0));
  assert.equal(streamed.at(-1)?.provider, "b");
  assert.equal(titles.at(-1), "◍ Name the session");

  const renamed = harness({ models: [free], model: free, reply: "ignored" });
  renamed.emit("session_start", { reason: "startup" });
  renamed.emit("before_agent_start", { messageType: "user", prompt: "first prompt" });
  renamed.pi.setSessionName("my session");
  renamed.emit("session_info_changed", { name: "my session" });
  await new Promise((resolve) => setTimeout(resolve, 0));
  assert.equal(renamed.titles.at(-1), "◍ my session");
});

test("promptText reads typed prompts and extension messages", () => {
  assert.equal(lib.promptText({ messageType: "user", prompt: "fix the title" }), "fix the title");
  assert.equal(lib.promptText({ messageType: "custom", message: { content: "[relay] hello" } }), "[relay] hello");
  assert.equal(
    lib.promptText({ messageType: "custom", message: { content: [{ type: "text", text: "hi" }, { type: "image" }] } }),
    "hi",
  );
  assert.equal(lib.promptText({}), "");
});

test("a relay turn titles the session even though it never emits an input event", async () => {
  const free = model("a", "free", 0, 0);
  const { emit, streamed, titles } = harness({ models: [free], model: free, reply: "Die Wurzel des Titels" });
  emit("session_start", { reason: "startup" });
  // pi starts a relay or extension turn straight from sendMessage(..., { triggerTurn: true }).
  emit("before_agent_start", { messageType: "custom", message: { content: "[relay from planner] fix the title" } });
  emit("agent_start");
  assert.equal(titles.at(-1), "◴ [relay from planner] fix the title");
  await new Promise((resolve) => setTimeout(resolve, 0));
  assert.equal(streamed.length, 1);
  assert.ok(titles.at(-1)?.includes("Die Wurzel des Titels"), titles.at(-1));
  emit("agent_settled", { aborted: false });
  assert.equal(titles.at(-1), "◍ Die Wurzel des Titels");
});

test("the topic request turns reasoning off so the reply holds text", async () => {
  const free = model("a", "free", 0, 0);
  const { emit, streamed } = harness({ models: [free], model: free, reply: "Name" });
  emit("session_start", { reason: "startup" });
  emit("before_agent_start", { messageType: "user", prompt: "first prompt" });
  await new Promise((resolve) => setTimeout(resolve, 0));
  assert.equal(streamed.at(-1)?.requestOptions?.reasoning, "off");
});

test("firstUserPrompt takes the first user turn or extension message in history", () => {
  assert.equal(lib.firstUserPrompt([]), undefined);
  assert.equal(
    lib.firstUserPrompt([
      { type: "model_change", provider: "a", modelId: "b" },
      { type: "message", message: { role: "assistant", content: [{ type: "text", text: "hello" }] } },
      { type: "custom_message", content: "[relay] first real ask" },
      { type: "message", message: { role: "user", content: "second ask" } },
    ]),
    "[relay] first real ask",
  );
  assert.equal(
    lib.firstUserPrompt([{ type: "message", message: { role: "user", content: [{ type: "text", text: "  typed ask  " }] } }]),
    "typed ask",
  );
});

test("a resumed session titles itself from the history before any turn", async () => {
  const free = model("a", "free", 0, 0);
  const history = [
    { type: "message", message: { role: "user", content: "why does the title stay stuck" } },
    { type: "message", message: { role: "assistant", content: [{ type: "text", text: "because" }] } },
  ];
  const { emit, streamed, titles } = harness({ models: [free], model: free, reply: "Stuck Terminal Title", history });
  emit("session_start", { reason: "resume" });
  // Seeded right away, before pi starts a run.
  assert.equal(titles.at(-1), "◍ why does the title stay stuck");
  await new Promise((resolve) => setTimeout(resolve, 0));
  assert.equal(streamed.length, 1, "a resume gets one topic call");
  assert.equal(titles.at(-1), "◍ Stuck Terminal Title");
});

test("a resumed session with a name keeps the name and skips the topic call", async () => {
  const free = model("a", "free", 0, 0);
  const history = [{ type: "message", message: { role: "user", content: "old prompt" } }];
  const { emit, streamed, titles } = harness({ name: "my session", models: [free], model: free, history });
  emit("session_start", { reason: "resume" });
  await new Promise((resolve) => setTimeout(resolve, 0));
  assert.equal(titles.at(-1), "◍ my session");
  assert.equal(streamed.length, 0);
});

test("compose includes tool timer and clips to MAX_CHARS keeping suffix", () => {
  assert.equal(lib.compose("◍", "pi", "⏱ bash 1.2s"), "◍ pi · ⏱ bash 1.2s");
  assert.equal(lib.compose("", "pi", "⏱ bash 1.2s"), "pi · ⏱ bash 1.2s");
  const longTitle = "a".repeat(100);
  const titled = lib.compose("◍", longTitle, "⏱ bash 1.2s");
  assert.equal(titled.length, 80);
  assert.ok(titled.endsWith("… · ⏱ bash 1.2s"));
});

test("the title shows tool timer while a tool runs and reverts when done", () => {
  mock.timers.enable({ apis: ["setInterval", "Date"] });
  const timers = liveTimers();
  try {
    const { emit, titles } = harness();
    emit("session_start", { reason: "startup" });
    assert.equal(titles.at(-1), "◍ pi");

    emit("agent_start");
    assert.equal(titles.at(-1), "◴ pi");

    emit("tool_execution_start", { toolCallId: "call-1", toolName: "bash" });
    assert.equal(titles.at(-1), "◴ pi · ⏱ bash 0.0s");

    mock.timers.tick(250);
    assert.equal(titles.at(-1), "◷ pi · ⏱ bash 0.3s");

    mock.timers.tick(1000);
    assert.equal(titles.at(-1), "◷ pi · ⏱ bash 1.3s");

    emit("tool_execution_end", { toolCallId: "call-1", toolName: "bash" });
    assert.equal(titles.at(-1), "◷ pi");

    emit("agent_settled");
    assert.equal(titles.at(-1), "◍ pi");
  } finally {
    timers.restore();
    mock.timers.reset();
  }
});
