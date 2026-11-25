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
  tunnelProxyEnv,
  rotateZenSessions,
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
  assert.strictEqual(new Headers(shapeNoTools.init.headers).get("authorization"), "Bearer public");

  // An account key pi resolved is forwarded, not replaced by the anonymous lane.
  const shapeKeyed = ensureShaped("https://opencode.ai/inference/openai/v1/chat/completions", {
    headers: { Authorization: "Bearer sk-mine" },
    body: JSON.stringify({ model: "big-pickle", messages: [{ role: "user", content: "hi" }] }),
  });
  assert.strictEqual(new Headers(shapeKeyed.init.headers).get("authorization"), "Bearer sk-mine");

  // Headers and user content look like opencode's: no SDK headers or accept, a trace, a plain-string user turn.
  const shapeSdk = ensureShaped("https://opencode.ai/inference/openai/v1/chat/completions", {
    headers: { accept: "application/json", "x-stainless-os": "Linux", "X-Stainless-Lang": "js" },
    body: JSON.stringify({
      model: "big-pickle",
      messages: [
        { role: "user", content: [{ type: "text", text: "hi" }] },
        { role: "user", content: [{ type: "text", text: "a" }, { type: "image_url", image_url: { url: "x" } }] },
      ],
    }),
  });
  const sdkHeaders = new Headers(shapeSdk.init.headers);
  assert.deepStrictEqual([...sdkHeaders.keys()].filter((k) => k.startsWith("x-stainless")), []);
  assert.strictEqual(sdkHeaders.has("accept"), false, "Bun adds accept: */* itself");
  const tp = /^00-([0-9a-f]{32})-([0-9a-f]{16})-01$/.exec(sdkHeaders.get("traceparent") ?? "");
  assert.ok(tp, "traceparent");
  assert.match(sdkHeaders.get("b3") ?? "", new RegExp(`^${tp![1]}-${tp![2]}-1-[0-9a-f]{16}$`));
  assert.match(sdkHeaders.get("user-agent") ?? "", /^opencode\/latest\/[^/]+\/cli$/);
  const sdkBody = JSON.parse(shapeSdk.init.body as string);
  assert.strictEqual(sdkBody.messages[1].content, "hi");
  assert.ok(Array.isArray(sdkBody.messages[2].content), "mixed content stays a list");

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

function testTunnelProxyEnv() {
  const tunnel = { HTTPS_PROXY: "http://127.0.0.1:25345", HTTP_PROXY: "http://127.0.0.1:25345" };
  assert.deepStrictEqual(tunnelProxyEnv({ mode: "tunnel", proxy: "http://127.0.0.1:25345" }), tunnel);
  // Direct mode is the host IP, so a proxy left in the file by an earlier tunnel is ignored.
  assert.deepStrictEqual(tunnelProxyEnv({ mode: "direct", proxy: "http://127.0.0.1:25345" }), {});
  assert.deepStrictEqual(tunnelProxyEnv({ mode: "tunnel", proxy: null }), {});
  assert.deepStrictEqual(tunnelProxyEnv({ mode: "tunnel" }), {});
  assert.deepStrictEqual(tunnelProxyEnv({ mode: "tunnel", proxy: "" }), {});
  // An older state file, or none at all, keeps the child on the host network.
  assert.deepStrictEqual(tunnelProxyEnv({ server: "nl-1", egressIp: "1.2.3.4" }), {});
  assert.deepStrictEqual(tunnelProxyEnv(null), {});
  assert.deepStrictEqual(tunnelProxyEnv(undefined), {});
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
} else if (mode === "dump_env") {
  const seen = {};
  for (const key of ["HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY"]) seen[key] = process.env[key] ?? null;
  fs.writeFileSync(process.env.FAKE_BUN_ENV_OUT, JSON.stringify(seen));
  try {
    fs.writeSync(3, JSON.stringify({ status: 200, headers: {} }));
  } catch {}
  process.stdout.write('data: {"id":"1","choices":[{"index":0,"delta":{"content":"ok"}}]}' + String.fromCharCode(10));
  setTimeout(() => process.exit(0), 10);
}
`;
  fs.writeFileSync(fakeBunPath, scriptContent);
  fs.chmodSync(fakeBunPath, 0o755);

  const envOut = path.join(tmpDir, "child-env.json");
  const vpnStateFile = path.join(tmpDir, "vpn-rotate.json");
  const writeVpnState = (state: unknown) => fs.writeFileSync(vpnStateFile, JSON.stringify(state));

  const prevEnv = process.env.PI_OPENCODE_FREE_BUN;
  const prevRuntime = process.env.XDG_RUNTIME_DIR;
  const prevProxy = { HTTPS_PROXY: process.env.HTTPS_PROXY, HTTP_PROXY: process.env.HTTP_PROXY, ALL_PROXY: process.env.ALL_PROXY };
  process.env.PI_OPENCODE_FREE_BUN = fakeBunPath;
  process.env.XDG_RUNTIME_DIR = tmpDir;
  process.env.FAKE_BUN_ENV_OUT = envOut;
  delete process.env.HTTPS_PROXY;
  delete process.env.HTTP_PROXY;
  delete process.env.ALL_PROXY;

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

    // 4. The only provider that rides the tunnel: the child's env follows the state file.
    process.env.FAKE_BUN_MODE = "dump_env";
    const childEnv = async () => {
      await bunFetch("https://opencode.ai/inference/openai/v1/chat/completions", "POST", {}, "{}", false, "big-pickle");
      return JSON.parse(fs.readFileSync(envOut, "utf8"));
    };
    const proxy = "http://127.0.0.1:25345";
    writeVpnState({ mode: "tunnel", proxy });
    assert.deepStrictEqual(await childEnv(), { HTTPS_PROXY: proxy, HTTP_PROXY: proxy, ALL_PROXY: null });
    // Direct mode without rate limits must not carry a stale proxy.
    writeVpnState({ mode: "direct", proxy });
    assert.deepStrictEqual(await childEnv(), { HTTPS_PROXY: null, HTTP_PROXY: null, ALL_PROXY: null });
    writeVpnState({ mode: "tunnel", proxy: null });
    assert.deepStrictEqual(await childEnv(), { HTTPS_PROXY: null, HTTP_PROXY: null, ALL_PROXY: null });
    fs.rmSync(vpnStateFile, { force: true });
    assert.deepStrictEqual(await childEnv(), { HTTPS_PROXY: null, HTTP_PROXY: null, ALL_PROXY: null });
  } finally {
    if (prevEnv !== undefined) {
      process.env.PI_OPENCODE_FREE_BUN = prevEnv;
    } else {
      delete process.env.PI_OPENCODE_FREE_BUN;
    }
    delete process.env.FAKE_BUN_MODE;
    delete process.env.FAKE_BUN_ENV_OUT;
    if (prevRuntime === undefined) delete process.env.XDG_RUNTIME_DIR;
    else process.env.XDG_RUNTIME_DIR = prevRuntime;
    for (const [key, value] of Object.entries(prevProxy)) {
      if (value === undefined) delete process.env[key];
      else process.env[key] = value;
    }
    fs.rmSync(tmpDir, { recursive: true, force: true });
  }
}

const EFFORT_LEVELS = ["minimal", "low", "medium", "high", "xhigh", "max"] as const;

function testOpencodeMimic() {
  const zen = "https://opencode.ai/inference/openai/v1/chat/completions";
  const shape = (messages: any[], session = "pi-a") =>
    ensureShaped(zen, {
      headers: { "x-opencode-session": session, "x-stainless-os": "Linux" },
      body: JSON.stringify({ messages, reasoning_effort: "high", model: "big-pickle", tools: [], stream: false }),
    });
  const hdr = (r: any) => r.init.headers as Record<string, string>;
  const trace = (r: any) => hdr(r).traceparent.split("-")[1];
  const user = { role: "user", content: [{ type: "text", text: "Fix the typo in README" }] };
  const call = { id: "c1", type: "function", function: { name: "glob", arguments: "{}" } };

  const first = shape([user]);
  assert.deepStrictEqual(Object.keys(hdr(first)), [
    "authorization", "content-type", "user-agent", "b3", "traceparent", "x-opencode-client",
    "x-opencode-project", "x-opencode-session", "x-opencode-session-id", "x-session-affinity", "x-session-id",
  ]);
  assert.deepStrictEqual(Object.keys(JSON.parse(first.init.body as string)), [
    "model", "messages", "tools", "stream", "stream_options", "max_completion_tokens", "reasoning_effort", "tool_choice",
  ]);

  // A retry and the tool-loop steps of one prompt share its trace, each with its own span.
  const retry = shape([user]);
  assert.strictEqual(trace(retry), trace(first));
  assert.notStrictEqual(hdr(retry).traceparent, hdr(first).traceparent);
  const step = shape([
    user,
    { role: "assistant", content: null, tool_calls: [call] },
    { role: "tool", content: "README.md", tool_call_id: "c1" },
  ]);
  assert.strictEqual(trace(step), trace(first));
  const [, , assistant, tool] = JSON.parse(step.init.body as string).messages;
  assert.deepStrictEqual(assistant, { role: "assistant", content: null, tool_calls: [call], reasoning_content: "" });
  assert.deepStrictEqual(Object.keys(tool), ["role", "tool_call_id", "content"]);

  const next = shape([user, { role: "assistant", content: "Done." }, { role: "user", content: "thanks" }]);
  assert.notStrictEqual(trace(next), trace(first), "a new prompt starts a new trace");

  // A rotation gives a new session, project hash and trace, so nothing links across it.
  rotateZenSessions();
  const rotated = shape([user]);
  assert.notStrictEqual(hdr(rotated)["x-opencode-session"], hdr(first)["x-opencode-session"]);
  assert.notStrictEqual(hdr(rotated)["x-opencode-project"], hdr(first)["x-opencode-project"]);
  assert.notStrictEqual(trace(rotated), trace(first));
}

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

async function testStartupSwitchBack() {
  resetModelTable();
  const dir = fs.mkdtempSync(path.join("/tmp", "free-switch-"));
  const prevDir = process.env.PI_CODING_AGENT_DIR;
  const provider = "opencode-free";
  const free = (id: string) => ({ id, name: id, contextWindow: 1000, maxTokens: 100, input: ["text"], variants: [] });
  const handlers: Record<string, any> = {};
  const switched: string[] = [];
  const notes: string[] = [];
  const target = { provider, id: "deepseek-v4.1-flash-free" };
  let settings: any = { defaultProvider: provider, defaultModel: "deepseek-v4.1-flash-free" };
  let setModelResult = true;
  const pi = {
    on: (event: string, handler: any) => {
      handlers[event] = handler;
    },
    registerProvider: () => {},
    getSettings: () => settings,
    setModel: async (model: any) => {
      switched.push(`${model.provider}/${model.id}`);
      return setModelResult;
    },
  } as any;
  const local = { provider: "llamacpp", id: "RVN-Qwen3.8-27B" };
  const ctx = (current: any, extra: any = {}) => ({
    hasUI: false,
    model: current,
    scopedModels: [],
    modelRegistry: { find: (p: string, id: string) => (p === provider && id === target.id ? target : undefined) },
    ui: { notify: (text: string) => notes.push(text) },
    ...extra,
  });
  const start = (reason: string, c: any) => handlers.session_start({ reason }, c);
  try {
    process.env.PI_CODING_AGENT_DIR = dir;
    writeCatalogCache([free("deepseek-v4.1-flash-free"), free("big-pickle")]);
    opencodeZenBun(pi);

    await start("startup", ctx(local));
    assert.deepStrictEqual(switched, [`${provider}/deepseek-v4.1-flash-free`], "a fresh startup switches back to the default");

    switched.length = 0;
    await start("startup", ctx(target));
    await start("resume", ctx(local));
    await start("new", ctx(local));
    await start("startup", ctx(local, { scopedModels: [{ model: target }] }));
    process.argv.push("--model", "anthropic/claude-opus-5-5");
    try {
      await start("startup", ctx(local));
    } finally {
      process.argv.length -= 2;
    }
    settings = { defaultProvider: provider, defaultModel: "gone-free" };
    await start("startup", ctx(local));
    assert.deepStrictEqual(switched, [], "every other selection is left alone");

    settings = { defaultProvider: provider, defaultModel: "deepseek-v4.1-flash-free" };
    setModelResult = false;
    await start("startup", ctx(local, { hasUI: true }));
    assert.ok(notes.some((note) => note.includes("no API key")), `expected a switch warning, got ${JSON.stringify(notes)}`);
    setModelResult = true;
  } finally {
    if (prevDir === undefined) delete process.env.PI_CODING_AGENT_DIR;
    else process.env.PI_CODING_AGENT_DIR = prevDir;
    fs.rmSync(dir, { recursive: true, force: true });
  }
}

async function runAll() {
  await primeModelTable(["big-pickle"]);
  testMultibyteSse();
  testSseErrors();
  testIsGitRepo();
  testPromptHygiene();
  testToolShaping();
  testOpencodeMimic();
  testThinkingLevelMap();
  testTunnelProxyEnv();
  testParseServedModels();
  testPoolInvariants();
  testCatalogCache();
  await testBunFetchSuite();
  await testRefreshCatalog();
  await testResolveCatalog();
  await testNoModelsUntilResolved();
  testCatalogOff();
  await testStartupSwitchBack();
  console.log("All opencode-free tests passed successfully.");
}

runAll().catch((err) => {
  console.error("Test failed:", err);
  process.exit(1);
});
