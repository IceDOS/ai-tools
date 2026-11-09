import assert from "node:assert";
import * as fs from "node:fs";
import * as path from "node:path";
import {
  parseSSEToCompletion,
  isGitRepo,
  buildHarnessPrompt,
  ensureShaped,
  bunFetch,
} from "../extensions/opencode-free.ts";

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

async function runAll() {
  testMultibyteSse();
  testSseErrors();
  testIsGitRepo();
  testPromptHygiene();
  testToolShaping();
  await testBunFetchSuite();
  console.log("All opencode-free tests passed successfully.");
}

runAll().catch((err) => {
  console.error("Test failed:", err);
  process.exit(1);
});
