// Run: node --experimental-transform-types --test tests/llamacpp-lifecycle.test.ts
import { test } from "node:test";
import assert from "node:assert/strict";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import extension from "../../../models/llamacpp/extensions/llamacpp-lifecycle.ts";

const LOCAL = { provider: "llamacpp", id: "RVN-Qwen3.8-27B" };
const settle = () => new Promise((resolve) => setTimeout(resolve, 25));

// beginLoad only probes /health when the real ensureUp would then start the server, so counting
// that probe tells us whether the startup preload was attempted, without ever spawning anything.
async function preloadAttempts(
  settings: any | undefined,
  argv: string[] = [],
  ctxExtra: any = {},
  current: any = LOCAL,
): Promise<number> {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "llamacpp-"));
  const prevDir = process.env.PI_CODING_AGENT_DIR;
  const prevFetch = globalThis.fetch;
  const prevArgv = process.argv.slice();
  const handlers: Record<string, any> = {};
  let probes = 0;
  try {
    process.env.PI_CODING_AGENT_DIR = dir;
    globalThis.fetch = (async (url: unknown) => {
      if (String(url).endsWith("/health")) probes++;
      return { ok: true, json: async () => [] };
    }) as unknown as typeof fetch;
    process.argv.push(...argv);
    const pi = {
      on: (event: string, handler: any) => {
        handlers[event] = handler;
      },
      registerCommand: () => {},
      getSettings: settings === undefined ? undefined : () => settings,
    } as any;
    extension(pi);
    await handlers.session_start({ reason: "startup" }, {
      model: current,
      scopedModels: [],
      modelRegistry: { find: () => undefined },
      ui: { notify: () => {} },
      ...ctxExtra,
    });
    await settle();
    return probes;
  } finally {
    globalThis.fetch = prevFetch;
    process.argv.splice(0, process.argv.length, ...prevArgv);
    if (prevDir === undefined) delete process.env.PI_CODING_AGENT_DIR;
    else process.env.PI_CODING_AGENT_DIR = prevDir;
    fs.rmSync(dir, { recursive: true, force: true });
  }
}

test("a fallback session does not preload when the default is a cloud model", async () => {
  assert.equal(await preloadAttempts({ defaultProvider: "opencode-free" }), 0);
});

test("an extension without getSettings is not treated as local by default", async () => {
  assert.equal(await preloadAttempts(undefined), 0);
});

test("a local default preloads", async () => {
  assert.equal(await preloadAttempts({ defaultProvider: "llamacpp" }), 1);
});

test("an explicit local model on the command line preloads", async () => {
  assert.equal(await preloadAttempts({ defaultProvider: "opencode-free" }, ["--model", "llamacpp/RVN-Qwen3.8-27B"]), 1);
  assert.equal(await preloadAttempts({ defaultProvider: "opencode-free" }, ["--provider", "llamacpp", "--model", "RVN"]), 1);
});

test("a scoped local model is an explicit choice too", async () => {
  const probes = await preloadAttempts({ defaultProvider: "opencode-free" }, [], {
    scopedModels: [{ model: LOCAL }],
  });
  assert.equal(probes, 1);
});
