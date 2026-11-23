import test from "node:test";
import assert from "node:assert/strict";
import plugin, { abandoned, control, isFree, maskFlaggedPaths, nextModel, returnAfter, tunnelProxy, unwrapURL, wrapBaseURL } from "./index.js";

test("tunnelProxy only returns the proxy in tunnel mode", () => {
  assert.equal(tunnelProxy({ mode: "tunnel", proxy: "http://127.0.0.1:25345" }), "http://127.0.0.1:25345");
  assert.equal(tunnelProxy({ mode: "direct", proxy: "http://127.0.0.1:25345" }), undefined);
  assert.equal(tunnelProxy({ mode: "tunnel", proxy: "" }), undefined);
  assert.equal(tunnelProxy(null), undefined);
});

test("wrapped base URL round-trips with the appended path and query", () => {
  const wrapped = wrapBaseURL(4321, "https://opencode.ai/zen/v1/");
  assert.equal(unwrapURL(`${wrapped}/chat/completions?x=1`), "https://opencode.ai/zen/v1/chat/completions?x=1");
  assert.equal(unwrapURL(wrapped), "https://opencode.ai/zen/v1");
});

test("control runs the client with the action and rejects unknown ones", async () => {
  const run = async (file, args) => `${file} ${args.join(" ")}`;
  assert.equal(await control(["/bin/vpn-rotate"], "", run), "/bin/vpn-rotate status");
  assert.equal(await control(["/bin/vpn-rotate"], " clear ", run), "/bin/vpn-rotate clear");
  assert.equal(await control(["/bin/vpn-rotate"], "connect", run), "Usage: /vpn-rotate [status|cancel|clear]");
});

test("abandoned only when the rotating session's next primary request uses another model", () => {
  const rotations = new Map([["ses_a", "deepseek-free"]]);
  const req = (sessionID, id, kind = "primary") => ({ sessionID, kind, model: { id, providerID: "opencode" } });
  assert.equal(abandoned(rotations, req("ses_a", "longcat-free")), true);
  assert.equal(abandoned(rotations, req("ses_a", "deepseek-free")), false);
  assert.equal(abandoned(rotations, req("ses_a", "small-free", "title")), false);
  assert.equal(abandoned(rotations, req("ses_b", "longcat-free")), false);
});

test("nextModel follows list order from the current model and skips tried ones", () => {
  const list = ["a", "b", "c"];
  assert.equal(nextModel(list, "a", new Set(["a"])), "b");
  assert.equal(nextModel(list, "c", new Set(["c"])), "a");
  assert.equal(nextModel(list, "x", new Set(["x"])), "a");
  assert.equal(nextModel(list, "b", new Set(["a", "b"])), "c");
  assert.equal(nextModel(list, "c", new Set(["a", "b", "c"])), undefined);
});

async function load(options, stderr) {
  const hooks = {};
  const run = (name, event) => Promise.all((hooks[name] ?? []).map((cb) => cb(event)));
  // The session's selected model, as the input bar shows it.
  let selected = { id: "deepseek-free", providerID: "opencode", variant: "max" };
  await plugin.setup({
    options: { command: ["sh", "-c", `echo '${stderr}' >&2; exit 1`], ...options },
    session: {
      hook: async (name, cb) => void (hooks[name] ??= []).push(cb),
      get: async () => ({ model: selected }),
      switchModel: async ({ model }) => void (selected = model),
    },
    command: { transform: async () => {} },
    model: {
      list: async () => ({
        data: [
          { id: "deepseek-free", providerID: "opencode", enabled: true },
          { id: "paid", providerID: "opencode", enabled: true, cost: [{ input: 0.3, output: 1.2 }] },
          { id: "big-pickle", providerID: "opencode", enabled: true, cost: [{ input: 0, output: 0 }] },
          { id: "minimax-free", providerID: "opencode", enabled: true },
        ],
      }),
    },
  });
  // One model request: resolve, then send; returns the model it went to.
  const request = async () => {
    const event = { sessionID: "s", kind: "primary", model: { ...selected } };
    await run("experimental.model.resolve", event);
    await run("model.request", event);
    return event.model.id;
  };
  const retry = async () => {
    const model = await request();
    const event = { sessionID: "s", attempt: 1, model: model === selected.id ? selected : { id: model, providerID: "opencode" }, error: { type: "provider.rate-limit", message: "429" } };
    await run("retry", event);
    return event.decision;
  };
  return {
    retry,
    request,
    prompt: () => run("prompt", { sessionID: "s" }),
    selected: () => selected,
    pick: (id) => void (selected = { id, providerID: "opencode" }),
  };
}

test("failed rotation switches to the next free model and back on the next prompt", async () => {
  const p = await load({}, "VPN rotation failed: all servers failed");
  assert.deepEqual(await p.retry(), { retry: true, delay: 0 });
  assert.equal(p.selected().id, "big-pickle");
  assert.equal(await p.request(), "big-pickle");
  await p.retry();
  assert.equal(p.selected().id, "minimax-free");
  assert.equal(await p.retry(), undefined);
  await p.prompt();
  assert.deepEqual(p.selected(), { id: "deepseek-free", providerID: "opencode", variant: "max" });
});

test("a model the user picked is not switched back", async () => {
  const p = await load({}, "VPN rotation failed: all failed");
  await p.retry();
  p.pick("other");
  await p.prompt();
  assert.equal(p.selected().id, "other");
});

test("user list, stay mode and cancelled rotations", async () => {
  const p = await load({ fallbackModels: ["opencode/x", "y"], fallbackReturn: "stay" }, "VPN rotation failed: all failed");
  await p.retry();
  assert.equal(p.selected().id, "x");
  await p.prompt();
  assert.equal(p.selected().id, "x");
  const c = await load({}, "VPN rotation failed: cancelled");
  assert.equal(await c.retry(), undefined);
  assert.equal(c.selected().id, "deepseek-free");
});

test("returnAfter parses s/m/h durations only", () => {
  assert.equal(returnAfter("90s"), 90_000);
  assert.equal(returnAfter("15m"), 900_000);
  assert.equal(returnAfter("2h"), 7_200_000);
  assert.equal(returnAfter("stay"), undefined);
  assert.equal(returnAfter("15"), undefined);
  assert.equal(returnAfter(undefined), undefined);
});

test("a duration keeps the fallback across prompts and switches back when it runs out", async () => {
  const p = await load({ fallbackModels: ["x"], fallbackReturn: "1s" }, "VPN rotation failed: all failed");
  await p.retry();
  await p.prompt();
  assert.equal(p.selected().id, "x");
  await new Promise((r) => setTimeout(r, 1_100));
  assert.equal(p.selected().id, "deepseek-free");
});

test("isFree goes by price", () => {
  assert.equal(isFree({ cost: [] }), true);
  assert.equal(isFree({ cost: [{ input: 0, output: 0 }] }), true);
  assert.equal(isFree({ cost: [{ input: 0, output: 0 }, { input: 0.5, output: 2 }] }), false);
});

test("maskFlaggedPaths masks paths deterministically with word boundaries", () => {
  const envBlock = `<env>\n  Working directory: /mnt/games/3/test/home/ice\n  Workspace root folder: /home/icecream/proj\n</env>`;
  const flagged = { "/home/ice": "/home/bob_1234", "/mnt/games": "/mnt/drive_5678" };
  const masked = maskFlaggedPaths(envBlock, flagged);
  assert.equal(masked.includes("/home/icecream/proj"), true);
  assert.equal(masked.includes("/home/bob_1234"), true);
  assert.equal(masked.includes("/mnt/drive_5678/3/test/home/bob_1234"), true);
});

test("maskFlaggedPaths masks cwd equal to flagged path inside JSON.stringify body", () => {
  const flagged = { "/home/ice/proj": "/home/bob_proj" };
  const rawBody = JSON.stringify({
    messages: [{
      role: "system",
      content: "You are an agent.\n<env>\n  Working directory: /home/ice/proj\n  Workspace root folder: /home/ice/proj\n</env>"
    }]
  });
  const masked = maskFlaggedPaths(rawBody, flagged);
  assert.equal(masked.includes("/home/ice/proj"), false);
  assert.equal(masked.includes("/home/bob_proj"), true);
  const parsed = JSON.parse(masked);
  assert.equal(parsed.messages[0].content.includes("Working directory: /home/bob_proj"), true);
});
