import assert from "node:assert";
import * as fs from "node:fs";
import * as path from "node:path";
import opencodeZenBun, {
  resetModelTable,
  toThinkingLevelMap,
  parseSSEToCompletion,
  isGitRepo,
  buildHarnessPrompt,
  ensureShaped,
  bunFetch,
} from "../extensions/opencode-free.ts";
import {
  FREE_MODEL_POOLS,
  loadCatalog,
  parseServedModels,
  poolOf,
  refreshCatalog,
  resolveCatalog,
  stalePools,
  writeCatalogCache,
} from "../extensions/free-models.ts";
import { FREE_POOL_CAPS } from "../extensions/cost-footer/shared.ts";

function testMultibyteSse() {
  const greekAndEmoji = "Καλημέρα κόσμε! 🚀✨";
  const sseData = `data: {"id":"chat-123","choices":[{"index":0,"delta":{"content":"${greekAndEmoji}"}}]}\ndata: [DONE]\n`;
  const comp = parseSSEToCompletion(sseData, "big-pickle");
  assert.strictEqual(comp.choices[0].message.content, greekAndEmoji);
  assert.strictEqual(comp.id, "chat-123");
}

function testSseErrors() {
  let threw1 = false;
  try {
    parseSSEToCompletion(`data: {"error":{"message":"Mid-stream failure"}}\n`, "big-pickle");
  } catch (e: any) {
    threw1 = true;
    assert.ok(e.message.includes("Mid-stream failure"));
  }
  assert.ok(threw1, "should throw on chunk.error");

  let threw2 = false;
  try {
    parseSSEToCompletion(`data: {"type":"error","message":"Gateway limit error"}\n`, "big-pickle");
  } catch (e: any) {
    threw2 = true;
    assert.ok(e.message.includes("Gateway limit error"));
  }
  assert.ok(threw2, "should throw on chunk.type === 'error'");
}

function testIsGitRepo() {
  const tmpDir = fs.mkdtempSync(path.join("/tmp", "git-test-"));
  try {
    assert.strictEqual(isGitRepo(tmpDir), false);
    const nested = path.join(tmpDir, "a", "b", "c");
    fs.mkdirSync(nested, { recursive: true });
    fs.writeFileSync(path.join(tmpDir, ".git"), "gitdir: /fake");
    assert.strictEqual(isGitRepo(nested), true);
  } finally {
    fs.rmSync(tmpDir, { recursive: true, force: true });
  }
}

function testPromptHygiene() {
  const prompt = buildHarnessPrompt();
  assert.ok(!prompt.includes("execute"), "harness prompt should not mention execute");
  assert.ok(!prompt.includes("session_move"), "harness prompt should not mention session_move");
  assert.ok(!prompt.includes("/tmp/opencode"), "harness prompt should not mention /tmp/opencode");
  assert.ok(prompt.includes("Is directory a git repo:"), "harness prompt should report git repo status");
}

function testToolShaping() {
  // Case A: no tools -> tool_choice: none and placeholder read
  const shapeNoTools = ensureShaped("https://opencode.ai/inference/openai/v1/chat/completions", {
    body: JSON.stringify({ model: "big-pickle", messages: [{ role: "user", content: "hi" }] }),
  });
  const bodyNoTools = JSON.parse(shapeNoTools.init.body as string);
  assert.strictEqual(bodyNoTools.tool_choice, "none");
  assert.strictEqual(bodyNoTools.tools.length, 1);
  assert.strictEqual(bodyNoTools.tools[0].function.name, "read");
  assert.ok(!bodyNoTools.messages[0].content.includes("Notice: The 'read' tool is an internal placeholder"));

  // Case B: user tools include read -> preserved as-is without placeholder or notice
  const userRead = {
    type: "function",
    function: { name: "read", description: "User read", parameters: { type: "object", properties: {} } },
  };
  const userBash = {
    type: "function",
    function: { name: "bash", description: "User bash", parameters: { type: "object", properties: {} } },
  };
  const shapeWithRead = ensureShaped("https://opencode.ai/inference/openai/v1/chat/completions", {
    body: JSON.stringify({
      model: "big-pickle",
      messages: [{ role: "user", content: "hi" }],
      tools: [userRead, userBash],
    }),
  });
  const bodyWithRead = JSON.parse(shapeWithRead.init.body as string);
  assert.strictEqual(bodyWithRead.tools.length, 2);
  assert.strictEqual(bodyWithRead.tools[0].function.description, "User read");
  assert.ok(!bodyWithRead.messages[0].content.includes("Notice: The 'read' tool is an internal placeholder"));

  // Case C: user tools omit read -> placeholder read injected with warning notice
  const shapeWithoutRead = ensureShaped("https://opencode.ai/inference/openai/v1/chat/completions", {
    body: JSON.stringify({
      model: "big-pickle",
      messages: [{ role: "user", content: "hi" }],
      tools: [userBash],
    }),
  });
  const bodyWithoutRead = JSON.parse(shapeWithoutRead.init.body as string);
  assert.strictEqual(bodyWithoutRead.tools.length, 2);
  assert.strictEqual(bodyWithoutRead.tools[0].function.name, "read");
  assert.strictEqual(bodyWithoutRead.tools[0].function.description, "System placeholder. Do not invoke.");
  assert.strictEqual(bodyWithoutRead.tools[1].function.name, "bash");
  assert.ok(bodyWithoutRead.messages[0].content.includes("Notice: The 'read' tool is an internal placeholder and is unavailable. Do not call 'read'."));
}

async function testBunFetchSuite() {
  const tmpDir = fs.mkdtempSync(path.join("/tmp", "fake-bun-test-"));
  const fakeBunPath = path.join(tmpDir, "fake-bun.js");
  const scriptContent = `#!${process.execPath}
const fs = require("fs");
const mode = process.env.FAKE_BUN_MODE || "multibyte_split";

if (mode === "multibyte_split") {
  try {
    fs.writeSync(3, JSON.stringify({ status: 200, headers: { "content-type": "text/event-stream" } }));
  } catch {}
  const prefix = Buffer.from('data: {"id":"1","choices":[{"index":0,"delta":{"content":"');
  const greekByte1 = Buffer.from([0xce]);
  const greekByte2 = Buffer.from([0xa9]);
  const nl = String.fromCharCode(10);
  const suffix = Buffer.from('"}}]}' + nl + nl + 'data: [DONE]' + nl + nl);
  process.stdout.write(Buffer.concat([prefix, greekByte1]));
  setTimeout(() => {
    process.stdout.write(Buffer.concat([greekByte2, suffix]));
    setTimeout(() => {
      process.exit(0);
    }, 10);
  }, 20);
} else if (mode === "exit_error") {
  process.stderr.write("Fatal connection error");
  process.exit(1);
} else if (mode === "null_metadata") {
  process.stdout.write("orphan output");
  process.exit(0);
}
`;
  fs.writeFileSync(fakeBunPath, scriptContent);
  fs.chmodSync(fakeBunPath, 0o755);

  const prevEnv = process.env.PI_OPENCODE_FREE_BUN;
  process.env.PI_OPENCODE_FREE_BUN = fakeBunPath;

  try {
    // 1. Multibyte character split across two stdout chunks
    process.env.FAKE_BUN_MODE = "multibyte_split";
    const res = await bunFetch("https://opencode.ai/inference/openai/v1/chat/completions", "POST", {}, "{}", false, "big-pickle");
    assert.strictEqual(res.status, 200);
    const data = await res.json();
    assert.strictEqual(data.choices[0].message.content, "Ω");
    assert.ok(!data.choices[0].message.content.includes("\uFFFD"), "multibyte sequence should not be corrupted");

    // 2. Child exit with code 1
    process.env.FAKE_BUN_MODE = "exit_error";
    let exitErr: any = null;
    try {
      await bunFetch("https://opencode.ai/inference/openai/v1/chat/completions", "POST", {}, "{}", false, "big-pickle");
    } catch (e: any) {
      exitErr = e;
    }
    assert.ok(exitErr, "should reject on child non-zero exit");
    assert.ok(exitErr.message.includes("bun process exited with code 1"), "error message should mention exit code 1");
    assert.ok(exitErr.message.includes("Fatal connection error"), "error message should include stderr");

    // 3. Child exit 0 without metadata on fd 3
    process.env.FAKE_BUN_MODE = "null_metadata";
    let metaErr: any = null;
    try {
      await bunFetch("https://opencode.ai/inference/openai/v1/chat/completions", "POST", {}, "{}", false, "big-pickle");
    } catch (e: any) {
      metaErr = e;
    }
    assert.ok(metaErr, "should reject on exit 0 without metadata");
    assert.ok(metaErr.message.includes("bun exited 0 before response"), "error message should specify bun exited 0 before response");
  } finally {
    if (prevEnv !== undefined) {
      process.env.PI_OPENCODE_FREE_BUN = prevEnv;
    } else {
      delete process.env.PI_OPENCODE_FREE_BUN;
    }
    delete process.env.FAKE_BUN_MODE;
    fs.rmSync(tmpDir, { recursive: true, force: true });
  }
}

const EFFORT_LEVELS = ["minimal", "low", "medium", "high", "xhigh", "max"] as const;

function testThinkingLevelMap() {
  // No variants: nothing is mapped, so pi reports no reasoning at all.
  assert.deepStrictEqual(toThinkingLevelMap([]), {
    minimal: null,
    low: null,
    medium: null,
    high: null,
    xhigh: null,
    max: null,
  });

  // An explicit "none" becomes pi's off, everything else is either the effort itself or null.
  assert.deepStrictEqual(toThinkingLevelMap(["none", "high"]), {
    off: "none",
    minimal: null,
    low: null,
    medium: null,
    high: "high",
    xhigh: null,
    max: null,
  });

  // Without "none", off stays unmapped: picking it sends no reasoning params, like opencode's default.
  assert.ok(!("off" in toThinkingLevelMap(["low", "high", "max"])));
  assert.deepStrictEqual(toThinkingLevelMap(["minimal", "low", "medium", "high", "xhigh", "max"]), {
    minimal: "minimal",
    low: "low",
    medium: "medium",
    high: "high",
    xhigh: "xhigh",
    max: "max",
  });
}

const served = (models: unknown[]) => ({ object: "list", data: models });
const freeChat = (id: string, extra: Record<string, unknown> = {}) => ({
  id,
  object: "model",
  type: "chat",
  name: `${id} from catalog`,
  modalities: { input: ["text"], output: ["text"] },
  capabilities: {},
  limit: { context: 200000, output: 32000 },
  cost: { text: { input: { price: 0 }, output: { price: 0 } } },
  ...extra,
});

function testParseServedModels() {
  const parsed = parseServedModels(
    served([
      freeChat("brand-new-free"),
      // free chat model with efforts and image input
      freeChat("effort-free", {
        modalities: { input: ["text", "image/jpeg", "audio/wav"] },
        capabilities: { reasoning: { supported: true, effort: ["high", "none", "bogus"] } },
        limit: { context: 1000000, output: 65536 },
      }),
      // paid on either side -> not a free model
      freeChat("paid-input-free", { cost: { text: { input: { price: 0.5 }, output: { price: 0 } } } }),
      freeChat("paid-output-free", { cost: { text: { input: { price: 0 }, output: { price: 2 } } } }),
      // not a chat model, or missing limits -> unusable
      freeChat("embedding-not-chat", { type: "embedding" }),
      freeChat("no-output-limit", { limit: { context: 200000 } }),
      freeChat("no-context-limit", { limit: { context: 0, output: 32000 } }),
      // duplicate ids collapse
      freeChat("brand-new-free"),
      // the catalog name is the only name there is
      freeChat("big-pickle", { name: "Big Pickle" }),
    ]),
  );

  assert.deepStrictEqual(
    parsed.map((model) => model.id),
    ["big-pickle", "brand-new-free", "effort-free"],
  );
  const [pickle, brandNew, effort] = parsed;
  assert.strictEqual(pickle.name, "Big Pickle", "names come from the catalog");
  assert.deepStrictEqual(pickle.variants, [], "no efforts means no variants");
  assert.strictEqual(brandNew.name, "brand-new-free from catalog");
  assert.deepStrictEqual(brandNew.input, ["text"]);
  // Unknown efforts are dropped, "none" sorts first and pi's order is kept.
  assert.deepStrictEqual(effort.variants, ["none", "high"]);
  assert.deepStrictEqual(effort.input, ["text", "image"], "video/audio modalities are clamped away");
  assert.strictEqual(effort.contextWindow, 1000000);
  assert.strictEqual(effort.maxTokens, 65536);

  // A payload with no free chat model is not a usable catalog: it parses to nothing, which the
  // refresh treats as a failure instead of publishing an empty table.
  assert.deepStrictEqual(parseServedModels(served([freeChat("paid-free", { cost: { text: { input: { price: 1 }, output: { price: 0 } } } })])), []);
  assert.deepStrictEqual(parseServedModels(null), []);
}

function testPoolInvariants() {
  const live = [freeChat("big-pickle"), freeChat("nemotron-3.5-lightning-free"), freeChat("unpooled-free")];
  const models = parseServedModels(served(live));

  for (const [pool, members] of Object.entries(FREE_MODEL_POOLS)) {
    assert.ok(members.length > 0, `pool ${pool} is empty`);
    for (const model of members) assert.strictEqual(poolOf(model), pool, `${model} resolves to its pool`);
  }
  assert.strictEqual(poolOf("unpooled-free"), "unpooled-free", "unlisted models are their own pool");

  // A pool member the catalog no longer serves is reported, so a retired model cannot sit in a
  // bucket that nothing burns.
  assert.deepStrictEqual(stalePools(models), []);
  const retired = parseServedModels(served([freeChat("big-pickle")]));
  assert.deepStrictEqual(stalePools(retired), ["nemotron:nemotron-3.5-lightning-free"]);
  assert.deepStrictEqual(stalePools([]), ["shared:big-pickle", "nemotron:nemotron-3.5-lightning-free"]);

  // Caps are keyed by pool, and an unlisted model is its own pool, so a cap key must be one of
  // the measured pools or a model id - never an empty string left behind by a rename.
  for (const pool of Object.keys(FREE_POOL_CAPS)) {
    assert.ok(pool.length > 0, "cap keys are non-empty");
    assert.ok(pool in FREE_MODEL_POOLS || /^[a-z0-9][a-z0-9.-]*$/.test(pool), `suspect cap key ${pool}`);
  }
}

function testCatalogCache() {
  const dir = fs.mkdtempSync(path.join("/tmp", "free-catalog-test-"));
  const prevDir = process.env.PI_CODING_AGENT_DIR;
  const prevOff = process.env.PI_OPENCODE_FREE_CATALOG;
  const prevTtl = process.env.PI_OPENCODE_FREE_CATALOG_TTL_MS;
  try {
    process.env.PI_CODING_AGENT_DIR = dir;
    delete process.env.PI_OPENCODE_FREE_CATALOG;
    delete process.env.PI_OPENCODE_FREE_CATALOG_TTL_MS;

    // Nothing cached is nothing served: there is no compiled-in table to fall back to.
    const cold = loadCatalog({ now: 1000 });
    assert.strictEqual(cold.source, "none");
    assert.deepStrictEqual(cold.models, []);
    assert.strictEqual(cold.fetchedAt, null);
    assert.strictEqual(cold.expired, true, "an absent cache must be resolved");

    const cached = [{ id: "only-free", name: "Only Free", contextWindow: 1000, maxTokens: 100, input: ["text"] as ("text" | "image")[], variants: [] }];
    writeCatalogCache(cached, { now: 5_000 });
    assert.ok(fs.existsSync(path.join(dir, "opencode-free", "models.json")), "cache file lands in the agent dir");

    const warm = loadCatalog({ now: 6_000 });
    assert.strictEqual(warm.source, "cache");
    assert.strictEqual(warm.fetchedAt, 5_000);
    assert.strictEqual(warm.expired, false);
    assert.deepStrictEqual(warm.models, cached);

    // Past the TTL the cache is still the newest table we have, so it is served while the
    // refresh runs - discarding it would leave the user with no models at all.
    const stale = loadCatalog({ now: 5_000 + 12 * 3600 * 1000 + 1 });
    assert.strictEqual(stale.source, "cache");
    assert.strictEqual(stale.expired, true);
    assert.deepStrictEqual(stale.models, cached);
    // An explicit TTL is honoured, including 0 which means "always resolve".
    assert.strictEqual(loadCatalog({ now: 6_000, ttlMs: 0 }).expired, true);

    // A cache holding nothing usable is not served.
    writeCatalogCache([], { now: 7_000 });
    assert.strictEqual(loadCatalog({ now: 8_000 }).source, "none");
    assert.deepStrictEqual(loadCatalog({ now: 8_000 }).models, []);
    fs.writeFileSync(path.join(dir, "opencode-free", "models.json"), "{ not json");
    assert.strictEqual(loadCatalog({ now: 8_000 }).source, "none");

    // Entries missing fields (an older cache shape) are dropped instead of crashing the launch.
    fs.writeFileSync(
      path.join(dir, "opencode-free", "models.json"),
      JSON.stringify({ fetchedAt: 8_000, models: [{ id: "old-shape", name: "Old", contextWindow: 1000, maxTokens: 100, input: ["text"] }, ...cached] }),
    );
    assert.deepStrictEqual(loadCatalog({ now: 8_000 }).models.map((m) => m.id), ["only-free"]);

    // The env switch disables the catalog entirely.
    writeCatalogCache(cached, { now: 9_000 });
    process.env.PI_OPENCODE_FREE_CATALOG = "off";
    assert.strictEqual(loadCatalog({ now: 10_000 }).source, "none");
    assert.deepStrictEqual(loadCatalog({ now: 10_000 }).models, []);
  } finally {
    if (prevDir === undefined) delete process.env.PI_CODING_AGENT_DIR;
    else process.env.PI_CODING_AGENT_DIR = prevDir;
    if (prevOff === undefined) delete process.env.PI_OPENCODE_FREE_CATALOG;
    else process.env.PI_OPENCODE_FREE_CATALOG = prevOff;
    if (prevTtl === undefined) delete process.env.PI_OPENCODE_FREE_CATALOG_TTL_MS;
    else process.env.PI_OPENCODE_FREE_CATALOG_TTL_MS = prevTtl;
    fs.rmSync(dir, { recursive: true, force: true });
  }
}

async function testRefreshCatalog() {
  const dir = fs.mkdtempSync(path.join("/tmp", "free-refresh-test-"));
  try {
    const payload = served([freeChat("fresh-free")]);
    const ok = await refreshCatalog({
      dir,
      fetchImpl: (async () => new Response(JSON.stringify(payload), { status: 200 })) as unknown as typeof fetch,
    });
    assert.strictEqual(ok.ok, true);
    assert.deepStrictEqual(loadCatalog({ dir }).models.map((model) => model.id), ["fresh-free"]);

    // A failed request leaves the cached table in place: that is the whole point of the cache.
    const failed = await refreshCatalog({
      dir,
      fetchImpl: (async () => {
        throw new Error("offline");
      }) as unknown as typeof fetch,
    });
    assert.strictEqual(failed.ok, false);
    assert.deepStrictEqual(loadCatalog({ dir }).models.map((model) => model.id), ["fresh-free"]);

    const httpError = await refreshCatalog({
      dir,
      fetchImpl: (async () => new Response("nope", { status: 503 })) as unknown as typeof fetch,
    });
    assert.strictEqual(httpError.ok, false);

    // An empty or unusable payload must not overwrite a good cache either.
    const empty = await refreshCatalog({
      dir,
      fetchImpl: (async () => new Response(JSON.stringify(served([])), { status: 200 })) as unknown as typeof fetch,
    });
    assert.strictEqual(empty.ok, false);
    assert.deepStrictEqual(loadCatalog({ dir }).models.map((model) => model.id), ["fresh-free"]);
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
}


/** Publishes a table the way a successful launch does, so the shaping tests have a model. */
async function primeModelTable(ids: string[]): Promise<void> {
  resetModelTable();
  const dir = fs.mkdtempSync(path.join("/tmp", "free-prime-"));
  const prevDir = process.env.PI_CODING_AGENT_DIR;
  const prevUrl = process.env.PI_OPENCODE_FREE_MODELS_URL;
  const prevFetch = globalThis.fetch;
  const prevError = console.error;
  let registered = 0;
  try {
    process.env.PI_CODING_AGENT_DIR = dir;
    process.env.PI_OPENCODE_FREE_MODELS_URL = "https://catalog.test/models";
    globalThis.fetch = (async () =>
      new Response(JSON.stringify(served(ids.map((id) => freeChat(id)))), { status: 200 })) as unknown as typeof fetch;
    console.error = () => {};
    opencodeZenBun({ on: () => {}, registerProvider: () => { registered++; } } as any);
    await waitFor(() => registered > 0);
  } finally {
    globalThis.fetch = prevFetch;
    console.error = prevError;
    if (prevDir === undefined) delete process.env.PI_CODING_AGENT_DIR;
    else process.env.PI_CODING_AGENT_DIR = prevDir;
    if (prevUrl === undefined) delete process.env.PI_OPENCODE_FREE_MODELS_URL;
    else process.env.PI_OPENCODE_FREE_MODELS_URL = prevUrl;
    fs.rmSync(dir, { recursive: true, force: true });
  }
}

async function waitFor(predicate: () => boolean, timeoutMs = 4000): Promise<void> {
  const start = Date.now();
  while (!predicate()) {
    if (Date.now() - start > timeoutMs) throw new Error("timed out waiting for condition");
    await new Promise((resolve) => setTimeout(resolve, 10));
  }
}

async function testResolveCatalog() {
  const dir = fs.mkdtempSync(path.join("/tmp", "free-resolve-test-"));
  try {
    // No cache, nothing published, the fetch fails: no models, and the user is told why.
    const errors: string[] = [];
    const published: string[][] = [];
    const failed = await resolveCatalog({
      dir,
      current: [],
      fetchImpl: (async () => {
        throw new Error("catalog unreachable");
      }) as unknown as typeof fetch,
      onModels: (models) => published.push(models.map((model) => model.id)),
      onError: (message) => errors.push(message),
    });
    assert.strictEqual(failed.status, "failed");
    assert.deepStrictEqual(published, [], "nothing is published when the resolve fails");
    assert.strictEqual(errors.length, 1);
    assert.ok(errors[0].includes("catalog unreachable"), "the fetch error is in the message");
    assert.ok(errors[0].includes("No free models are available"), "the message says there are no models");
    assert.deepStrictEqual(loadCatalog({ dir }).models, [], "a failed resolve caches nothing");

    // Same failure, but a table is already published: it stays, and the message says so.
    const keptErrors: string[] = [];
    const kept = await resolveCatalog({
      dir,
      current: [{ id: "kept-free", name: "Kept", contextWindow: 1000, maxTokens: 100, input: ["text"], variants: [] }],
      fetchImpl: (async () => new Response("nope", { status: 503 })) as unknown as typeof fetch,
      onModels: (models) => published.push(models.map((model) => model.id)),
      onError: (message) => keptErrors.push(message),
    });
    assert.strictEqual(kept.status, "failed");
    assert.ok(keptErrors[0].includes("503"));
    assert.ok(keptErrors[0].includes("Keeping the last known model list"));
    assert.deepStrictEqual(published, [], "a failed resolve never republishes");

    // Success with nothing published yet: the table lands, and the cache is written.
    const ok = await resolveCatalog({
      dir,
      current: [],
      fetchImpl: (async () => new Response(JSON.stringify(served([freeChat("later-free")])), { status: 200 })) as unknown as typeof fetch,
      onModels: (models) => published.push(models.map((model) => model.id)),
      onError: (message) => errors.push(message),
    });
    assert.strictEqual(ok.status, "updated");
    assert.deepStrictEqual(published, [["later-free"]]);
    assert.deepStrictEqual(errors.length, 1, "a success reports no error");
    assert.deepStrictEqual(loadCatalog({ dir }).models.map((model) => model.id), ["later-free"]);

    // The same table again is not a change, so nothing is republished.
    const unchanged = await resolveCatalog({
      dir,
      current: loadCatalog({ dir }).models,
      fetchImpl: (async () => new Response(JSON.stringify(served([freeChat("later-free")])), { status: 200 })) as unknown as typeof fetch,
      onModels: (models) => published.push(models.map((model) => model.id)),
      onError: (message) => errors.push(message),
    });
    assert.strictEqual(unchanged.status, "unchanged");
    assert.deepStrictEqual(published, [["later-free"]]);

    // A different table is a change, and the note names the added model.
    const changed = await resolveCatalog({
      dir,
      current: loadCatalog({ dir }).models,
      fetchImpl: (async () => new Response(JSON.stringify(served([freeChat("later-free"), freeChat("extra-free")])), { status: 200 })) as unknown as typeof fetch,
      onModels: (models) => published.push(models.map((model) => model.id)),
      onError: (message) => errors.push(message),
    });
    assert.strictEqual(changed.status, "updated");
    assert.ok(changed.status === "updated" && changed.change.includes("added extra-free"));
    assert.deepStrictEqual(published, [["later-free"], ["extra-free", "later-free"]]);
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
}

/**
 * The launch path: with no cache and a failing fetch the provider is not registered at all, the
 * error is shown to the user, and the first session retries until the catalog resolves.
 */
async function testNoModelsUntilResolved() {
  resetModelTable();
  const dir = fs.mkdtempSync(path.join("/tmp", "free-launch-test-"));
  const prevDir = process.env.PI_CODING_AGENT_DIR;
  const prevUrl = process.env.PI_OPENCODE_FREE_MODELS_URL;
  const prevOff = process.env.PI_OPENCODE_FREE_CATALOG;
  const prevFetch = globalThis.fetch;
  const prevError = console.error;
  const errors: string[] = [];
  let serve = false;
  try {
    process.env.PI_CODING_AGENT_DIR = dir;
    delete process.env.PI_OPENCODE_FREE_CATALOG;
    process.env.PI_OPENCODE_FREE_MODELS_URL = "https://catalog.test/models";
    globalThis.fetch = (async () => {
      if (!serve) throw new Error("catalog unreachable");
      return new Response(JSON.stringify(served([freeChat("big-pickle"), freeChat("later-free")])), { status: 200 });
    }) as unknown as typeof fetch;
    console.error = (...args: unknown[]) => {
      errors.push(args.map((arg) => String(arg)).join(" "));
    };

    const providers: any[] = [];
    const notes: { text: string; level?: string }[] = [];
    let sessionStart: ((event: unknown, ctx: unknown) => void) | null = null;
    const pi = {
      on: (event: string, handler: (event: unknown, ctx: unknown) => void) => {
        if (event === "session_start") sessionStart = handler;
      },
      registerProvider: (provider: any) => providers.push(provider),
    } as any;

    opencodeZenBun(pi);

    // The launch path reports the failure and serves nothing.
    await waitFor(() => errors.length > 0);
    assert.deepStrictEqual(providers, [], "no provider table until the catalog resolves");
    assert.ok(errors[0].includes("could not fetch the free model catalog"));
    assert.ok(errors[0].includes("No free models are available"));

    const route = () =>
      ensureShaped("https://opencode.ai/inference/openai/v1/chat/completions", {
        method: "POST",
        body: JSON.stringify({ model: "big-pickle", messages: [] }),
      });
    assert.strictEqual(route().shaped, false, "an unresolved catalog shapes nothing");

    // The error is queued for the first session, which also retries the resolve.
    const ctx = { hasUI: true, ui: { notify: (text: string, level?: string) => notes.push({ text, level }) } };
    assert.ok(sessionStart, "the extension subscribes to session_start");
    sessionStart!({}, ctx);
    await waitFor(() => notes.length > 0);
    assert.strictEqual(notes[0].level, "error");
    assert.ok(notes[0].text.includes("could not fetch the free model catalog"));
    assert.deepStrictEqual(providers, [], "a failed retry still registers nothing");

    // Once the catalog is reachable the retry publishes the table and routes its models.
    serve = true;
    sessionStart!({}, ctx);
    await waitFor(() => providers.length > 0);
    assert.deepStrictEqual(providers.at(-1)!.getModels().map((model: any) => model.id), ["big-pickle", "later-free"]);
    assert.deepStrictEqual(loadCatalog({ dir }).models.map((model) => model.id), ["big-pickle", "later-free"]);
    assert.strictEqual(route().shaped, true, "resolved models are routed");
    assert.ok(
      notes.some((note) => note.text.includes("nemotron:nemotron-3.5-lightning-free")),
      "a pooled model the catalog no longer serves is reported",
    );
  } finally {
    globalThis.fetch = prevFetch;
    console.error = prevError;
    if (prevDir === undefined) delete process.env.PI_CODING_AGENT_DIR;
    else process.env.PI_CODING_AGENT_DIR = prevDir;
    if (prevUrl === undefined) delete process.env.PI_OPENCODE_FREE_MODELS_URL;
    else process.env.PI_OPENCODE_FREE_MODELS_URL = prevUrl;
    if (prevOff === undefined) delete process.env.PI_OPENCODE_FREE_CATALOG;
    else process.env.PI_OPENCODE_FREE_CATALOG = prevOff;
    fs.rmSync(dir, { recursive: true, force: true });
  }
}

function testCatalogOff() {
  resetModelTable();
  const prevOff = process.env.PI_OPENCODE_FREE_CATALOG;
  const prevFetch = globalThis.fetch;
  let fetches = 0;
  let hooks = 0;
  try {
    process.env.PI_OPENCODE_FREE_CATALOG = "off";
    globalThis.fetch = (async () => {
      fetches++;
      return new Response("{}");
    }) as unknown as typeof fetch;
    opencodeZenBun({ on: () => { hooks++; }, registerProvider: () => assert.fail("off registers nothing") } as any);
    assert.strictEqual(fetches, 0, "off never fetches the catalog");
    assert.strictEqual(hooks, 0, "off never retries on session_start");
  } finally {
    globalThis.fetch = prevFetch;
    if (prevOff === undefined) delete process.env.PI_OPENCODE_FREE_CATALOG;
    else process.env.PI_OPENCODE_FREE_CATALOG = prevOff;
  }
}

async function runAll() {
  await primeModelTable(["big-pickle"]);
  testMultibyteSse();
  testSseErrors();
  testIsGitRepo();
  testPromptHygiene();
  testToolShaping();
  testThinkingLevelMap();
  testParseServedModels();
  testPoolInvariants();
  testCatalogCache();
  await testBunFetchSuite();
  await testRefreshCatalog();
  await testResolveCatalog();
  await testNoModelsUntilResolved();
  testCatalogOff();
  console.log("All opencode-free tests passed successfully.");
}

runAll().catch((err) => {
  console.error("Test failed:", err);
  process.exit(1);
});
