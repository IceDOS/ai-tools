/** @jsxImportSource @opentui/solid */
// OpenCode TUI status plugin: terminal title, turn-end message, live tool timer, VPN row and relay ID.
import { readdirSync, readFileSync } from "node:fs";
import { Plugin } from "@opencode/plugin/tui";
import { createSignal, Show } from "solid-js";
import {
  composeVpnRow,
  clearExecutionTools,
  composeTitle,
  composeToolTimer,
  composeTurnEnd,
  createToolTracker,
  DEFAULT_FALLBACK_TITLE,
  DEFAULT_IDLE_GLYPH,
  DEFAULT_WORKING_GLYPHS,
  finishTool,
  getOldestRunningTool,
  parseRlmChildrenRow,
  readChildLogSample,
  pickGlyph,
  recordToolName,
  startTool,
  updateToolProgress,
} from "./lib.ts";

export default Plugin.define({
  id: "icedos.status",
  setup(context) {
    const idleGlyph = String(context.options.idleGlyph ?? DEFAULT_IDLE_GLYPH);
    const workingGlyphs = Array.isArray(context.options.workingGlyphs)
      ? context.options.workingGlyphs.map(String)
      : DEFAULT_WORKING_GLYPHS;
    const enableTerminalTitle = context.options.terminalTitle !== false;
    const enableTurnEnd = context.options.turnEnd !== false;
    const enableToolTimer = context.options.toolTimer !== false;

    const cleanups: Array<() => void> = [];
    const [now, setNow] = createSignal(Date.now());
    const [turnStartTimes, setTurnStartTimes] = createSignal<Record<string, number>>({});
    const [turnEndBanners, setTurnEndBanners] = createSignal<Record<string, string>>({});

    // Running sessions set and tool tracker state
    const runningSessions = new Set<string>();
    const tracker = createToolTracker();
    const [runningRevision, setRunningRevision] = createSignal(0);

    // Track execution boundaries for terminal title and turn duration
    cleanups.push(
      context.data.on("session.execution.started", (event) => {
        runningSessions.add(event.data.sessionID);
        setTurnStartTimes((prev) => ({ ...prev, [event.data.sessionID]: Date.now() }));
        setTurnEndBanners((prev) => {
          const next = { ...prev };
          delete next[event.data.sessionID];
          return next;
        });
      }),
    );

    const onExecutionEnd = (event: { data: { sessionID: string } }) => {
      runningSessions.delete(event.data.sessionID);
      const start = turnStartTimes()[event.data.sessionID];
      const elapsed = start ? Math.max(0, Math.round((Date.now() - start) / 1000)) : 0;
      const banner = composeTurnEnd(elapsed, new Date(Date.now()), idleGlyph);
      setTurnEndBanners((prev) => ({ ...prev, [event.data.sessionID]: banner }));
      clearExecutionTools(tracker, event.data.sessionID);
      setRunningRevision((r) => r + 1);
    };

    cleanups.push(context.data.on("session.execution.succeeded", onExecutionEnd));
    cleanups.push(context.data.on("session.execution.failed", onExecutionEnd));
    cleanups.push(context.data.on("session.execution.interrupted", onExecutionEnd));

    // 1. Terminal Title
    if (enableTerminalTitle) {
      let lastTitle = "";
      let tickCount = 0;
      const updateTitle = () => {
        const route = context.ui.router.current();
        const sessionID = route.type === "session" ? route.sessionID : undefined;
        const session = sessionID ? context.data.session.get(sessionID) : undefined;
        const oldest = sessionID ? getOldestRunningTool(tracker, sessionID) : undefined;
        const isRunning = sessionID
          ? runningSessions.has(sessionID) ||
            context.data.session.status(sessionID) === "running" ||
            Boolean(oldest)
          : runningSessions.size > 0 || Object.keys(tracker.running).length > 0;
        const glyph = pickGlyph(isRunning, tickCount++, idleGlyph, workingGlyphs);
        const titleText = session?.title || DEFAULT_FALLBACK_TITLE;
        const timer = oldest ? composeToolTimer(oldest.tool, Date.now() - oldest.start) : undefined;
        const nextTitle = composeTitle(
          glyph,
          route.type === "home" ? DEFAULT_FALLBACK_TITLE : titleText,
          DEFAULT_FALLBACK_TITLE,
          timer,
        );
        if (nextTitle !== lastTitle) {
          lastTitle = nextTitle;
          context.renderer.setTerminalTitle(nextTitle);
        }
      };

      const titleInterval = setInterval(updateTitle, 250);
      cleanups.push(() => clearInterval(titleInterval));
      cleanups.push(() => context.renderer.setTerminalTitle(""));
    }

    // 2. Turn End Banner
    if (enableTurnEnd) {
      cleanups.push(
        context.ui.slot({
          append: "session.composer.top",
          render: (input) => {
            const banner = () => turnEndBanners()[input.sessionID];
            return (
              <Show when={banner()}>
                {(msg) => (
                  <box paddingLeft={1}>
                    <text fg={context.theme.text.muted}>{msg()}</text>
                  </box>
                )}
              </Show>
            );
          },
        }),
      );
    }

    // 3. Live Tool Timer
    if (enableToolTimer) {
      const toolInterval = setInterval(() => setNow(Date.now()), 1000);
      cleanups.push(() => clearInterval(toolInterval));

      cleanups.push(
        context.data.on("session.tool.input.started", (event) => {
          recordToolName(tracker, event.data.id, event.data.name, event.data.sessionID);
        }),
      );

      cleanups.push(
        context.data.on("session.tool.called", (event) => {
          startTool(tracker, event.data.id, event.data.sessionID);
          setRunningRevision((r) => r + 1);
        }),
      );

      cleanups.push(
        context.data.on("session.tool.progress", (event) => {
          updateToolProgress(tracker, event.data.id, event.data.metadata);
          setRunningRevision((r) => r + 1);
        }),
      );

      const onToolEnd = (event: { data: { id: string } }) => {
        finishTool(tracker, event.data.id);
        setRunningRevision((r) => r + 1);
      };
      cleanups.push(context.data.on("session.tool.success", onToolEnd));
      cleanups.push(context.data.on("session.tool.failed", onToolEnd));

      cleanups.push(
        context.ui.slot({
          append: "prompt.footer.status",
          render: (input) => {
            const active = () => {
              runningRevision();
              return getOldestRunningTool(tracker, input.sessionID);
            };
            return (
              <Show when={active()}>
                {(t) => (
                  <text fg={context.theme.text.muted}>
                    {composeToolTimer(t().tool, now() - t().start)}
                  </text>
                )}
              </Show>
            );
          },
        }),
      );
    }

    // 4. VPN rotation row
    if (context.options.vpnRow === true) {
      const statePath = `${process.env.XDG_RUNTIME_DIR || "/tmp"}/vpn-rotate.json`;
      const readRow = () => {
        try {
          return composeVpnRow(JSON.parse(readFileSync(statePath, "utf8")), Date.now() / 1000);
        } catch {
          return undefined;
        }
      };
      const [vpnRow, setVpnRow] = createSignal(readRow());
      const vpnInterval = setInterval(() => setVpnRow(readRow()), 2000);
      cleanups.push(() => clearInterval(vpnInterval));

      cleanups.push(
        context.ui.slot({
          append: "prompt.footer.status",
          render: () => (
            <Show when={vpnRow()}>{(row) => <text fg={context.theme.text.muted}>{row()}</text>}</Show>
          ),
        }),
      );
    }

    // 5. Relay ID; agent-relay writes it per session on the first model request.
    const relayRoots = `${process.env.XDG_RUNTIME_DIR || "/tmp"}/agent-relay/opencode/roots`;
    const [relayTick, setRelayTick] = createSignal(0);
    const relayInterval = setInterval(() => setRelayTick((t) => t + 1), 2000);
    cleanups.push(() => clearInterval(relayInterval));
    cleanups.push(
      context.ui.slot({
        append: "prompt.footer.status",
        render: (input) => {
          const relayId = () => {
            relayTick();
            if (!input.sessionID) return undefined;
            try {
              return String(JSON.parse(readFileSync(`${relayRoots}/${input.sessionID}.json`, "utf8")).relayId || "") || undefined;
            } catch {
              return undefined;
            }
          };
          return <Show when={relayId()}>{(id) => <text fg={context.theme.text.muted}>relay {id()}</text>}</Show>;
        },
      }),
    );


    // 6. RLM children row
    if (context.options.rlmRow === true) {
      const dataDir =
        process.env.PLUGIN_DATA ||
        `${process.env.XDG_DATA_HOME || `${process.env.HOME || ""}/.local/share`}/icedos-rlm`;
      const isAlive = (pid: number) => {
        try {
          process.kill(pid, 0);
          return true;
        } catch {
          return false;
        }
      };
      const readRow = (sessionID: string) => {
        try {
          const childrenDir = `${dataDir}/children/${sessionID}`;
          const files = readdirSync(childrenDir).filter((f) => f.endsWith(".jsonl"));
          if (files.length === 0) return undefined;
          const logs = files.map((f) => readChildLogSample(`${childrenDir}/${f}`));
          return parseRlmChildrenRow(logs, Date.now() / 1000, isAlive);
        } catch {
          return undefined;
        }
      };

      const [rlmTick, setRlmTick] = createSignal(0);
      const rlmInterval = setInterval(() => setRlmTick((t) => t + 1), 2000);
      cleanups.push(() => clearInterval(rlmInterval));

      cleanups.push(
        context.ui.slot({
          append: "prompt.footer.status",
          render: (input) => {
            const row = () => {
              rlmTick();
              if (!input.sessionID) return undefined;
              return readRow(input.sessionID);
            };
            return (
              <Show when={row()}>
                {(text) => <text fg={context.theme.text.muted}>{text()}</text>}
              </Show>
            );
          },
        }),
      );
    }

    return () => {
      for (const cleanup of cleanups) cleanup();
    };
  },
});
