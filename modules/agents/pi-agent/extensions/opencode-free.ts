import * as crypto from "node:crypto";
import * as fs from "node:fs";
import * as path from "node:path";
import { execSync, spawn } from "node:child_process";
import {
  createProvider,
  envApiKeyAuth,
  openAICompletionsApi,
  type Model,
  type Provider,
} from "@earendil-works/pi-ai/compat";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import {
  BASE_URL,
  PROVIDER_ID,
  ZEN_USER_AGENT,
  catalogDisabled,
  loadCatalog,
  resolveCatalog,
  stalePools,
  toThinkingLevelMap,
  type FreeModel,
} from "./free-models.ts";

export { PROVIDER_ID, toThinkingLevelMap };

const BUN_BIN = "@bunBin@";

let cachedBun: string | null = null;

function findBun(): string | null {
  if (process.env.PI_OPENCODE_FREE_BUN) return process.env.PI_OPENCODE_FREE_BUN;
  if (cachedBun) return cachedBun;
  if (BUN_BIN && !BUN_BIN.startsWith("@") && fs.existsSync(BUN_BIN)) {
    cachedBun = BUN_BIN;
    return BUN_BIN;
  }
  try {
    const which = execSync("which bun 2>/dev/null", { encoding: "utf8" }).trim();
    if (which && fs.existsSync(which)) {
      cachedBun = which;
      return which;
    }
  } catch {}
  return null;
}

const BRIDGE = `
const fs = require("fs");
async function main() {
  const input = fs.readFileSync(0, "utf8");
  const spec = JSON.parse(input);
  let res;
  try {
    res = await fetch(spec.url, {
      method: spec.method || "POST",
      headers: spec.headers || {},
      body: spec.body,
      redirect: "follow",
    });
  } catch (err) {
    try {
      fs.writeSync(3, JSON.stringify({ status: 0, headers: {}, error: String(err) }));
    } catch {}
    process.stderr.write(String(err));
    process.exit(1);
    return;
  }
  const h = {};
  res.headers.forEach((v, k) => { h[k] = v; });
  try {
    fs.writeSync(3, JSON.stringify({ status: res.status, headers: h }));
  } catch {}
  if (!res.ok) {
    const errText = await res.text().catch(() => "");
    process.stdout.write(errText);
    process.exitCode = 0;
    return;
  }
  const reader = res.body && res.body.getReader ? res.body.getReader() : null;
  if (reader) {
    for (;;) {
      const r = await reader.read();
      if (r.done) break;
      process.stdout.write(Buffer.from(r.value));
    }
  }
}
main().catch((e) => {
  try {
    fs.writeSync(3, JSON.stringify({ status: 0, headers: {}, error: String(e) }));
  } catch {}
  process.stderr.write(String(e));
  process.exit(1);
});
`;

export function parseSSEToCompletion(sseText: string, model: string): any {
  let content = "";
  let id = "";
  let created = Math.floor(Date.now() / 1000);
  let finishReason = "stop";
  let usage: any = null;
  const toolCallMap = new Map<number, { id: string; type: string; function: { name: string; arguments: string } }>();

  for (const line of sseText.split("\n")) {
    const trimmed = line.trim();
    if (!trimmed.startsWith("data:")) continue;
    const payload = trimmed.slice(5).trim();
    if (payload === "[DONE]") break;
    let chunk: any;
    try {
      chunk = JSON.parse(payload);
    } catch {
      continue;
    }
    if (chunk?.error) {
      const msg = typeof chunk.error === "string" ? chunk.error : chunk.error.message || JSON.stringify(chunk.error);
      throw new Error(`SSE error: ${msg}`);
    }
    if (chunk?.type === "error") {
      throw new Error(`SSE error: ${chunk.message || chunk.error || JSON.stringify(chunk)}`);
    }
    if (chunk.id) id = chunk.id;
    if (chunk.created) created = chunk.created;
    if (chunk.usage) usage = chunk.usage;
    const choice = chunk.choices?.[0];
    if (choice?.finish_reason) finishReason = choice.finish_reason;
    if (choice?.delta?.content) content += choice.delta.content;
    if (choice?.delta?.tool_calls) {
      for (const tc of choice.delta.tool_calls) {
        const idx = tc.index ?? 0;
        if (!toolCallMap.has(idx)) {
          toolCallMap.set(idx, { id: tc.id || "", type: "function", function: { name: "", arguments: "" } });
        }
        const existing = toolCallMap.get(idx)!;
        if (tc.id) existing.id = tc.id;
        if (tc.function?.name) existing.function.name += tc.function.name;
        if (tc.function?.arguments) existing.function.arguments += tc.function.arguments;
      }
    }
  }
  const toolCalls = Array.from(toolCallMap.values());
  return {
    id: id || "chatcmpl-" + Math.random().toString(36).slice(2),
    object: "chat.completion",
    created,
    model: model || "opencode",
    choices: [
      {
        index: 0,
        message: {
          role: "assistant",
          content: content || null,
          ...(toolCalls.length > 0 ? { tool_calls: toolCalls } : {}),
        },
        finish_reason: finishReason,
      },
    ],
    usage: usage || { prompt_tokens: 0, completion_tokens: 0, total_tokens: 0 },
  };
}

function childToWebStream(
  child: ReturnType<typeof spawn>,
  getAbortError: () => Error | null,
  getStderr: () => string,
): ReadableStream<Uint8Array> {
  const queue: Uint8Array[] = [];
  const waiters: ((chunk: Uint8Array | null, isEnd: boolean) => void)[] = [];
  let stdoutEnded = false;
  let childClosed = false;
  let streamError: Error | null = null;
  let streamController: ReadableStreamDefaultController<Uint8Array> | null = null;
  let closed = false;

  const stdout = child.stdout!;

  stdout.on("data", (c: Buffer) => {
    const u = new Uint8Array(c);
    if (waiters.length > 0) {
      const w = waiters.shift()!;
      w(u, false);
    } else {
      queue.push(u);
    }
  });

  stdout.on("end", () => {
    stdoutEnded = true;
    checkDone();
  });

  stdout.on("error", (err: Error) => {
    if (!streamError && !getAbortError()) {
      streamError = err;
    }
    checkDone();
  });

  child.on("error", (err: Error) => {
    if (!streamError && !getAbortError()) {
      streamError = err;
    }
    checkDone();
  });

  child.on("close", (code) => {
    childClosed = true;
    const abortErr = getAbortError();
    if (abortErr) {
      streamError = abortErr;
    } else if (code !== 0 && !streamError) {
      const stderr = getStderr();
      streamError = new Error(`bun process exited with code ${code}: ${stderr.slice(0, 400)}`);
    }
    checkDone();
  });

  function checkDone() {
    if (streamError) {
      const err = streamError;
      if (streamController && !closed) {
        try {
          streamController.error(err);
        } catch {}
      }
      while (waiters.length > 0) {
        const w = waiters.shift()!;
        w(null, true);
      }
      return;
    }
    if (stdoutEnded) {
      while (waiters.length > 0) {
        const w = waiters.shift()!;
        w(null, true);
      }
    }
  }

  return new ReadableStream<Uint8Array>({
    start(controller) {
      streamController = controller;
    },
    pull(controller) {
      const err = streamError || getAbortError();
      if (err) {
        controller.error(err);
        return;
      }
      if (queue.length > 0) {
        const c = queue.shift()!;
        controller.enqueue(c);
        return;
      }
      if (childClosed && stdoutEnded) {
        controller.close();
        return;
      }
      const w = (chunk: Uint8Array | null, isEnd: boolean) => {
        if (closed) return;
        const currentErr = streamError || getAbortError();
        if (currentErr) {
          try {
            controller.error(currentErr);
          } catch {}
          return;
        }
        if (isEnd) {
          try {
            controller.close();
          } catch {}
        } else if (chunk) {
          try {
            controller.enqueue(chunk);
          } catch {}
        }
      };
      waiters.push(w);
    },
    cancel() {
      closed = true;
      try {
        child.kill("SIGKILL");
      } catch {}
      try {
        stdout.destroy?.();
      } catch {}
    },
  });
}

export function bunFetch(
  url: string,
  method: string,
  headers: Record<string, string>,
  body: string,
  callerWantsStream: boolean,
  modelName: string,
  signal?: AbortSignal,
): Promise<Response> {
  return new Promise<Response>((resolve, reject) => {
    let child: ReturnType<typeof spawn> | null = null;
    let timeoutTimer: NodeJS.Timeout | null = null;
    let settled = false;
    let abortError: Error | null = null;

    const cleanup = () => {
      if (signal && onAbort) signal.removeEventListener("abort", onAbort);
      if (timeoutTimer) {
        clearTimeout(timeoutTimer);
        timeoutTimer = null;
      }
      if (child) {
        try {
          child.kill("SIGKILL");
        } catch {}
      }
    };

    const settle = (r: Response) => {
      if (settled) return;
      settled = true;
      if (timeoutTimer) {
        clearTimeout(timeoutTimer);
        timeoutTimer = null;
      }
      resolve(r);
    };

    const fail = (e: Error) => {
      if (settled) return;
      settled = true;
      cleanup();
      reject(e);
    };

    const onAbort = () => {
      const err = new Error("The operation was aborted");
      err.name = "AbortError";
      abortError = err;
      if (!settled) {
        fail(err);
      } else {
        cleanup();
      }
    };

    if (signal?.aborted) {
      onAbort();
      return;
    }
    if (signal) {
      signal.addEventListener("abort", onAbort, { once: true });
    }

    try {
      const bun = findBun();
      if (!bun) {
        fail(new Error("bun binary not found"));
        return;
      }

      child = spawn(bun, ["-e", BRIDGE], {
        stdio: ["pipe", "pipe", "pipe", "pipe"],
      });

      let stderr = "";
      child.stderr?.on("data", (c: Buffer) => (stderr += c.toString()));

      let metaRaw = "";
      const metaStream = child.stdio[3] as import("stream").Readable;
      const nonStreamChunks: Buffer[] = [];
      let isNonStream = false;
      let stdoutEnded = false;
      let childClosed = false;
      let childExitCode = 0;
      let streamErr: Error | null = null;
      let responseHeaders: Headers | null = null;
      let responseStatus = 200;

      const finishNonStream = () => {
        if (settled) return;
        if (streamErr) {
          fail(streamErr);
          return;
        }
        if (!stdoutEnded || !childClosed) return;
        if (childExitCode !== 0) {
          fail(new Error(`bun process exited with code ${childExitCode}: ${stderr.slice(0, 400)}`));
          return;
        }
        if (!responseHeaders) {
          fail(new Error(`bun exited ${childExitCode} before response: ${stderr.slice(0, 400)}`));
          return;
        }
        try {
          if (responseStatus !== 200) {
            if (responseStatus === 429) {
              rotateZenSessions();
            }
            const raw = Buffer.concat(nonStreamChunks);
            const response = new Response(raw, {
              status: responseStatus,
              headers: responseHeaders!,
            });
            settle(response);
            cleanup();
            return;
          }
          const sseBody = Buffer.concat(nonStreamChunks).toString("utf8");
          responseHeaders!.set("content-type", "application/json");
          const comp = parseSSEToCompletion(sseBody, modelName);
          const response = new Response(JSON.stringify(comp), {
            status: 200,
            headers: responseHeaders!,
          });
          settle(response);
          cleanup();
        } catch (err) {
          fail(err as Error);
        }
      };

      const processMeta = () => {
        if (settled || !metaRaw) return;
        let meta: { status?: number; headers?: Record<string, string>; error?: string };
        try {
          meta = JSON.parse(metaRaw.trim());
        } catch {
          return;
        }
        if (meta.status === 429) {
          rotateZenSessions();
        }
        if (!meta.status || meta.status < 200 || meta.status > 599 || meta.error) {
          fail(new TypeError(meta.error || `fetch failed (status ${meta.status})`));
          return;
        }
        if (!child?.stdout) {
          fail(new Error("child stdout is null"));
          return;
        }
        const h = new Headers();
        for (const [k, v] of Object.entries(meta.headers ?? {})) h.set(k, v);

        if (callerWantsStream) {
          const response = new Response(childToWebStream(child, () => abortError, () => stderr), {
            status: meta.status,
            headers: h,
          });
          settle(response);
          return;
        }

        isNonStream = true;
        responseHeaders = h;
        responseStatus = meta.status;
        finishNonStream();
      };

      if (!callerWantsStream && child.stdout) {
        child.stdout.on("data", (chunk: Buffer) => {
          nonStreamChunks.push(chunk);
        });
        child.stdout.on("end", () => {
          stdoutEnded = true;
          finishNonStream();
        });
        child.stdout.on("error", (err: Error) => {
          if (!streamErr) streamErr = err;
          finishNonStream();
        });
      }

      metaStream?.on("data", (chunk: Buffer) => {
        metaRaw += chunk.toString("utf8");
        processMeta();
      });

      child.on("error", (e) => {
        if (!settled) {
          if (!callerWantsStream) {
            if (!streamErr) streamErr = e;
            finishNonStream();
          } else {
            fail(e);
          }
        }
      });

      child.on("close", (code) => {
        childClosed = true;
        childExitCode = code ?? 0;
        processMeta();
        if (!callerWantsStream) {
          finishNonStream();
        } else if (!settled) {
          fail(new Error(`bun exited ${code} before response: ${stderr.slice(0, 400)}`));
        }
        if (settled) {
          cleanup();
        }
      });

      timeoutTimer = setTimeout(() => {
        if (!settled) fail(new Error("bun fetch timed out after 10m"));
      }, 600000);
      timeoutTimer.unref?.();

      child.stdin?.on("error", () => {});
      child.stdin?.end(JSON.stringify({ url, method, headers, body }));
    } catch (err) {
      fail(err as Error);
    }
  });
}

const ID_CHARS = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz";
let lastTimestamp = 0;
let counter = 0;

function descendingId(timestamp = Date.now()): string {
  if (timestamp !== lastTimestamp) {
    lastTimestamp = timestamp;
    counter = 0;
  }
  counter++;
  const current = BigInt(timestamp) * 0x1000n + BigInt(counter);
  const value = ~current;
  const time = Array.from({ length: 6 }, (_, index) =>
    Number((value >> BigInt(40 - 8 * index)) & 0xffn)
      .toString(16)
      .padStart(2, "0"),
  ).join("");
  const bytes = crypto.getRandomValues(new Uint8Array(14));
  return "ses_" + time + Array.from(bytes, (byte) => ID_CHARS[byte % 62]).join("");
}

const STATE = `${process.env.XDG_RUNTIME_DIR || "/tmp"}/pi-vpn-rotate.json`;
let lastKnownRotationEpoch = 0;

function checkGlobalRotationEpoch(): boolean {
  try {
    if (fs.existsSync(STATE)) {
      const s = JSON.parse(fs.readFileSync(STATE, "utf8"));
      // rotatedAt only moves when a rotation finishes; `at` moves on every save (debounce, flags).
      if (s?.rotatedAt > lastKnownRotationEpoch) {
        lastKnownRotationEpoch = s.rotatedAt;
        return true;
      }
    }
  } catch {}
  return false;
}

export function rotateZenSessions(): void {
  sessionMap.clear();
  processFallbackSessionId = null;
}

async function waitForRotation(signal?: AbortSignal): Promise<void> {
  const start = Date.now();
  while (Date.now() - start < 20000) {
    if (signal?.aborted) return;
    try {
      if (fs.existsSync(STATE)) {
        const s = JSON.parse(fs.readFileSync(STATE, "utf8"));
        // Stale flag: a service killed mid-rotation leaves rotating:true (tmpfs) until logout.
        if (!s?.rotating || Date.now() - s.at > 120000) break;
      } else {
        break;
      }
    } catch {
      break;
    }
    await new Promise((r) => setTimeout(r, 500));
  }
}

const sessionMap = new Map<string, string>();
let processFallbackSessionId: string | null = null;

function getZenSessionId(key?: string): string {
  if (checkGlobalRotationEpoch()) {
    rotateZenSessions();
  }
  if (!key) {
    if (!processFallbackSessionId) {
      processFallbackSessionId = descendingId();
    }
    return processFallbackSessionId;
  }
  let ses = sessionMap.get(key);
  if (!ses) {
    ses = descendingId();
    sessionMap.set(key, ses);
  }
  return ses;
}

function isZenTarget(url: unknown): boolean {
  if (typeof url !== "string") return false;
  try {
    const parsed = new URL(url);
    if (process.env.PI_OPENCODE_FREE_BASE_URL && url.startsWith(process.env.PI_OPENCODE_FREE_BASE_URL)) {
      return parsed.pathname.endsWith("/chat/completions");
    }
    if (parsed.hostname !== "opencode.ai" && !parsed.hostname.endsWith(".opencode.ai")) {
      return false;
    }
    if (
      parsed.pathname.includes("/inference/go/") ||
      parsed.pathname.includes("/zen/go/") ||
      parsed.pathname.includes("/inference/anthropic")
    ) {
      return false;
    }
    return (
      parsed.pathname.endsWith("/chat/completions") &&
      (parsed.pathname.includes("/inference/openai/v1") || parsed.pathname.includes("/zen/v1"))
    );
  } catch {
    return false;
  }
}

const GATEWAY_READ_TOOL = {
  type: "function",
  function: {
    name: "read",
    description: "System placeholder. Do not invoke.",
    parameters: {
      type: "object",
      properties: { filePath: { type: "string" } },
      required: ["filePath"],
    },
  },
};

export function isGitRepo(dir: string): boolean {
  let curr = dir;
  while (true) {
    if (fs.existsSync(path.join(curr, ".git"))) return true;
    const parent = path.dirname(curr);
    if (parent === curr) break;
    curr = parent;
  }
  return false;
}

export function buildHarnessPrompt(): string {
  const cwd = process.cwd();
  const isGit = isGitRepo(cwd);
  return (
    "You are an AI agent running in OpenCode, a coding agent harness. Help the user accomplish their goals using the tools you have available.\n\n" +
    "Here is some useful information about the environment you are running in:\n" +
    "<env>\n" +
    "  Working directory: " + cwd + "\n" +
    "  Workspace root folder: " + cwd + "\n" +
    "  Is directory a git repo: " + (isGit ? "yes" : "no") + "\n" +
    "  Platform: linux\n" +
    "</env>"
  );
}

export function ensureShaped(
  url: string,
  init: RequestInit,
): { url: string; init: RequestInit; shaped: boolean; callerWantsStream: boolean; model: string } {
  let targetUrl = typeof url === "string" ? url : (url as any)?.url ?? "";
  if (!isZenTarget(targetUrl)) {
    return { url, init, shaped: false, callerWantsStream: true, model: "" };
  }
  if (targetUrl.includes("/zen/v1")) {
    targetUrl = targetUrl.replace("/zen/v1", "/inference/openai/v1");
  }
  let body: any;
  try {
    body = JSON.parse((init?.body as string) ?? "{}");
  } catch {
    return { url, init, shaped: false, callerWantsStream: true, model: "" };
  }
  if (typeof body !== "object" || body === null) {
    return { url, init, shaped: false, callerWantsStream: true, model: "" };
  }
  if (!body.model || !registeredIds.has(body.model)) {
    return { url, init, shaped: false, callerWantsStream: true, model: "" };
  }

  const callerWantsStream = Boolean(body.stream);
  const userTools = Array.isArray(body.tools) ? body.tools : [];
  let injectedDummyRead = false;

  if (userTools.length === 0) {
    body.tools = [GATEWAY_READ_TOOL];
    body.tool_choice = "none";
  } else {
    const hasRead = userTools.some((t: any) => t?.function?.name === "read");
    if (hasRead) {
      body.tools = userTools;
    } else {
      injectedDummyRead = true;
      body.tools = [GATEWAY_READ_TOOL, ...userTools];
    }
  }

  const incomingMessages = Array.isArray(body.messages) ? body.messages : [];
  const systemMsg = incomingMessages.find((m: any) => m.role === "system");
  const otherMessages = incomingMessages.filter((m: any) => m.role !== "system");
  const piSysPrompt = typeof systemMsg?.content === "string" ? systemMsg.content : "";
  let harness = buildHarnessPrompt();
  if (injectedDummyRead) {
    harness += "\n\nNotice: The 'read' tool is an internal placeholder and is unavailable. Do not call 'read'.";
  }
  const combinedSystemPrompt = harness + (piSysPrompt ? "\n\n# User Instructions\n" + piSysPrompt : "");
  body.messages = [
    { role: "system", content: combinedSystemPrompt },
    ...otherMessages,
  ];

  if (body.max_tokens !== undefined) {
    body.max_completion_tokens = body.max_tokens;
    delete body.max_tokens;
  } else if (body.max_completion_tokens === undefined) {
    // Unregistered ids returned above, so the model is in the published table.
    body.max_completion_tokens = publishedModels.find((model) => model.id === body.model)!.maxTokens;
  }

  body.stream = true;
  body.stream_options = { include_usage: true };

  const cwd = process.cwd();
  const headers = new Headers(init?.headers ?? {});
  headers.set("authorization", "Bearer public");
  headers.set("content-type", "application/json");
  headers.set("user-agent", ZEN_USER_AGENT);
  headers.set("x-opencode-client", "cli");

  let sesId = headers.get("x-opencode-session-id") ?? headers.get("x-opencode-session");
  if (!sesId || !sesId.startsWith("ses_")) {
    sesId = getZenSessionId(sesId ?? undefined);
  }
  headers.set("x-opencode-session-id", sesId);
  headers.set("x-opencode-session", sesId);
  headers.set("x-session-affinity", sesId);
  headers.set("x-session-id", sesId);

  const projectHash = crypto.createHash("sha1").update(`directory:${cwd}:${sesId}`).digest("hex");
  headers.set("x-opencode-project", projectHash);
  headers.delete("x-opencode-request");
  headers.delete("traceparent");
  headers.delete("b3");

  return {
    url: targetUrl,
    init: { ...init, method: init?.method ?? "POST", body: JSON.stringify(body), headers },
    shaped: true,
    callerWantsStream,
    model: body.model,
  };
}

type PatchedFetch = typeof globalThis.fetch & { __ocZenBun?: boolean };

function installBunTransport(): void {
  const current = globalThis.fetch as PatchedFetch;
  if (current.__ocZenBun) return;
  const original = current;
  const patched: typeof globalThis.fetch = async (input, init) => {
    const rawUrl =
      typeof input === "string"
        ? input
        : input instanceof URL
          ? input.href
          : (input as Request)?.url ?? String(input);

    if (!isZenTarget(rawUrl)) {
      return original(input, init);
    }

    let url: string;
    let reqInit: RequestInit;
    if (typeof input === "string") {
      url = input;
      reqInit = init ?? {};
    } else if (input instanceof URL) {
      url = input.href;
      reqInit = init ?? {};
    } else {
      const req = input as Request;
      url = req.url;
      reqInit = {
        method: init?.method ?? req.method,
        headers: init?.headers ?? req.headers,
        body: init?.body ?? (req as any).body,
        signal: init?.signal ?? req.signal,
      };
      const duplex = (init as any)?.duplex ?? (req as any)?.duplex;
      if (duplex) (reqInit as any).duplex = duplex;
    }
    const shaped = ensureShaped(url, reqInit);
    if (shaped.shaped) {
      await waitForRotation(reqInit.signal ?? undefined);
      const method = (shaped.init.method as string) || "POST";
      const headers: Record<string, string> = {};
      const h = shaped.init.headers as unknown;
      if (h instanceof Headers) for (const [k, v] of h.entries()) headers[k] = v;
      else if (h && typeof h === "object")
        for (const [k, v] of Object.entries(h as Record<string, string>)) headers[k] = v;
      const body = typeof shaped.init.body === "string" ? (shaped.init.body as string) : undefined;
      return await bunFetch(
        shaped.url,
        method,
        headers,
        body ?? "",
        shaped.callerWantsStream,
        shaped.model,
        reqInit.signal ?? undefined,
      );
    }
    return original(input, init);
  };
  (patched as PatchedFetch).__ocZenBun = true;
  globalThis.fetch = patched;
}

function toChatModel(def: FreeModel): Model<"openai-completions"> {
  // No effort variants means nothing to choose, so the model reports no reasoning at all.
  const reasoning = def.variants.some((variant) => variant !== "none");
  return {
    id: def.id,
    name: def.name,
    api: "openai-completions",
    provider: PROVIDER_ID,
    baseUrl: BASE_URL,
    reasoning,
    ...(reasoning ? { thinkingLevelMap: toThinkingLevelMap(def.variants) } : {}),
    input: def.input,
    cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
    contextWindow: def.contextWindow,
    maxTokens: def.maxTokens,
  } as Model<"openai-completions">;
}

function withZenSessionHeader(api: ReturnType<typeof openAICompletionsApi>): ReturnType<typeof openAICompletionsApi> {
  return {
    ...api,
    stream: (model, context, options) => {
      const sesId = getZenSessionId(options?.sessionId);
      const headers = { ...options?.headers, "x-opencode-session": sesId, "x-opencode-session-id": sesId };
      return api.stream(model, context, { ...options, headers });
    },
    streamSimple: (model, context, options) => {
      const sesId = getZenSessionId(options?.sessionId);
      const headers = { ...options?.headers, "x-opencode-session": sesId, "x-opencode-session-id": sesId };
      return api.streamSimple(model, context, { ...options, headers });
    },
  };
}

function createOpencodeProvider(models: FreeModel[]): Provider<"openai-completions"> {
  return createProvider({
    id: PROVIDER_ID,
    name: "OpenCode Free (bun)",
    baseUrl: BASE_URL,
    auth: { apiKey: envApiKeyAuth("OpenCode Zen API key", ["PI_OPENCODE_FREE_API_KEY", "OPENCODE_ZEN_API_KEY", "OPENCODE_API_KEY"]) },
    models: models.map(toChatModel),
    api: {
      "openai-completions": withZenSessionHeader(openAICompletionsApi()),
    },
  });
}

// The model ids the registered provider table serves; ensureShaped asks this set, so nothing is
// shaped (and no model is offered) until the catalog resolves. Empty is the launch state.
let registeredIds = new Set<string>();
/** The table the provider currently serves; the resolve compares against it. */
let publishedModels: FreeModel[] = [];

/**
 * Forgets the published table, the queued notes and the notifier: what a process start looks
 * like. Only tests call this, to run a launch scenario from a clean state.
 */
export function resetModelTable(): void {
  registeredIds = new Set();
  publishedModels = [];
  pendingNotes.length = 0;
  notifier = null;
}

function registerTable(pi: ExtensionAPI, models: FreeModel[]): void {
  registeredIds = new Set(models.map((model) => model.id));
  publishedModels = models;
  // With no catalog there is nothing to register: an empty provider would only add a dead
  // group to the picker. The resource appears as soon as the resolve lands.
  if (models.length > 0) pi.registerProvider(createOpencodeProvider(models));
}

// The resolve usually finishes before any session starts, so a note that arrives early waits
// for a session context instead of being dropped.
type Note = { text: string; level: "info" | "warning" | "error" };
let notifier: ((text: string, level: Note["level"]) => void) | null = null;
const pendingNotes: Note[] = [];

function notify(text: string, level: Note["level"] = "warning"): void {
  if (notifier) notifier(text, level);
  else pendingNotes.push({ text, level });
}

/**
 * Resolves the catalog, then publishes whatever it returned. There is no built-in model list,
 * so a failure is shown to the user instead of being swallowed: the provider keeps serving the
 * models it already had (the last cached resolve) or, when there are none, nothing at all until
 * a later resolve succeeds.
 */
async function resolveInBackground(pi: ExtensionAPI): Promise<void> {
  const resolution = await resolveCatalog({
    current: publishedModels,
    onModels: (models) => {
      registerTable(pi, models);
      const stale = stalePools(models);
      if (stale.length > 0) {
        // A pooled model the catalog dropped still counts against a bucket nothing serves.
        notify(`opencode-free: rate-limit pool lists ${stale.join(", ")}, which the catalog no longer serves`, "warning");
      }
    },
    onError: (message) => {
      // Headless runs have no ui.notify, so the launch path gets the error on stderr too.
      console.error(`opencode-free: ${message}`);
      notify(`opencode-free: ${message}`, "error");
    },
  });
  if (resolution.status === "updated") notify(`opencode-free catalog updated: ${resolution.change}`, "warning");
}

export default function opencodeZenBun(pi: ExtensionAPI): void {
  // off means no fetch and no models, so there is nothing to route either.
  if (catalogDisabled()) return;
  installBunTransport();
  const catalog = loadCatalog();
  if (catalog.models.length > 0) registerTable(pi, catalog.models);

  let resolving = false;
  let resolveAgain = false;
  const resolve = (): void => {
    if (resolving) {
      // A session started while a resolve was in flight: run once more when it settles, so the
      // retry is never lost to the in-flight request.
      resolveAgain = true;
      return;
    }
    resolving = true;
    void resolveInBackground(pi).finally(() => {
      resolving = false;
      if (resolveAgain) {
        resolveAgain = false;
        resolve();
      }
    });
  };

  pi.on("session_start", (_event, ctx: any) => {
    notifier = (text, level) => {
      if (!ctx.hasUI) return;
      try {
        ctx.ui?.notify(text, level);
      } catch {}
    };
    for (const note of pendingNotes.splice(0)) notifier(note.text, note.level);
    // Unresolved at launch: keep trying while there is no model table to serve.
    if (registeredIds.size === 0) resolve();
  });

  // A fresh cache is the point of the cache, so only an expired (or absent) one resolves now.
  if (catalog.expired) resolve();
}
