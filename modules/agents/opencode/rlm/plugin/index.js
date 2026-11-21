import fs from "node:fs";
import net from "node:net";
import os from "node:os";
import path from "node:path";
import { execFile, spawn } from "node:child_process";
import { promisify } from "node:util";

const execFileAsync = promisify(execFile);

export function getDataDir() {
  return (
    process.env.PLUGIN_DATA ||
    path.join(process.env.XDG_STATE_HOME || path.join(os.homedir(), ".local/state"), "icedos", "rlm")
  );
}

export function loadHarness(filePath) {
  try {
    const lines = fs.readFileSync(filePath, "utf-8").split("\n");
    const byId = new Map();
    for (const line of lines) {
      if (!line.trim()) continue;
      try {
        const item = JSON.parse(line);
        if (item && item.id) byId.set(item.id, item);
      } catch {}
    }
    return Array.from(byId.values()).filter((e) => !e.deleted);
  } catch {
    return [];
  }
}

export function renderEntries(entries) {
  return entries
    .slice(-20)
    .map((e) => {
      let content = e.content || "";
      if (content.length > 500) content = content.slice(0, 500) + "…";
      return `- [${e.id}] ${e.title} — ${content}`;
    })
    .join("\n");
}

export function buildHarnessSection(globalEntries, localEntries) {
  const all = [...globalEntries, ...localEntries];
  if (all.length === 0) return "";
  const memories = all.filter((e) => e.kind === "memory");
  const prompts = all.filter((e) => e.kind === "prompt");
  const parts = [
    "## Continual harness (rlm plugin)",
    "Persistent notes from past refinements. Follow them; they survive this conversation.",
  ];
  if (memories.length) parts.push("### Memories", renderEntries(memories));
  if (prompts.length) parts.push("### Policies", renderEntries(prompts));
  parts.push('Manage via /refine, or refine("instructions") inside the ipython kernel.');
  return parts.join("\n");
}

export function sendSnapshotControl(dataDir, sessionID) {
  const sockPath = path.join(dataDir, "run", `${sessionID}.sock`);
  if (!fs.existsSync(sockPath)) return;
  try {
    const client = net.createConnection(sockPath, () => {
      client.write(JSON.stringify({ type: "snapshot", session_id: sessionID }) + "\n");
    });
    client.on("data", () => client.end());
    client.on("error", () => {});
  } catch {}
}

export function rlmChildrenArgs(sessionID, text) {
  const child = (text || "").trim();
  const args = ["children", "--session", sessionID];
  if (child) args.push(child);
  return args;
}

export default {
  id: "opencode-rlm",
  async setup(ctx) {
    const rlmBin = (ctx.options && ctx.options.rlmMcp) || process.env.RLM_SELF || "rlm-mcp";
    const dataDir = getDataDir();

    // Session start harness injection via context hook
    await ctx.session.hook("context", async (event) => {
      const g = loadHarness(path.join(dataDir, "harness.jsonl"));
      const l = loadHarness(path.join(dataDir, "harness", `${event.sessionID}.jsonl`));
      const section = buildHarnessSection(g, l);
      if (section && event.system) {
        event.system.push({ type: "text", text: section });
      }
    });

    // Snapshot and refine on experimental.idle
    await ctx.session.hook("experimental.idle", async (event) => {
      sendSnapshotControl(dataDir, event.sessionID);
      const queueFile = path.join(dataDir, "refine-queue", `${event.sessionID}.json`);
      if (fs.existsSync(queueFile)) {
        try {
          spawn(rlmBin, ["refine-run", "--conversation", event.sessionID], {
            detached: true,
            stdio: "ignore",
            env: { ...process.env, PLUGIN_DATA: dataDir, RLM_SELF: rlmBin },
          }).unref();
        } catch {}
      }
    });

    // /rlm-children command via ctx.command.transform
    await ctx.command.transform((editor) => {
      editor.add({
        name: "rlm-children",
        description: "Show running and finished RLM children",
        execute: async (input) => {
          const cliArgs = rlmChildrenArgs(input.sessionID, input?.prompt?.text);
          try {
            const { stdout } = await execFileAsync(rlmBin, cliArgs, {
              env: { ...process.env, PLUGIN_DATA: dataDir, RLM_SELF: rlmBin },
            });
            const report = stdout && stdout.trim() ? stdout.trim() : "No child logs found.";
            if (ctx.session.synthetic) {
              await ctx.session.synthetic({ sessionID: input.sessionID, text: report });
            }
          } catch (err) {
            if (ctx.session.synthetic) {
              await ctx.session.synthetic({
                sessionID: input.sessionID,
                text: `Failed to inspect children: ${err.message || String(err)}`,
              });
            }
          }
        },
      });
    });
  },
};
