import { Relay, formatMessage, readConfig, type Message, type Watcher } from "./core.ts";
import { INSTRUCTIONS, TOOLS, peersText, runTool } from "./tools.ts";

// pi cannot remove a queued follow-up, so a batch counts as delivered only when its message reaches a run.
const ACK_MS = 5000;
// A dead model would eat a batch per flush, so a failed run holds mail until a probe finds a live model.
const PROBE_MS = 60_000;
const PROBE_MAX_MS = 600_000;

type Clock = { setTimeout: (fn: () => void, ms: number) => any; clearTimeout: (h: any) => void };

export default function (
  pi: any,
  relayFactory: () => Relay = () => new Relay(),
  ackMs = ACK_MS,
  probeMs = PROBE_MS,
  clock: Clock = { setTimeout, clearTimeout },
) {
  let relay: Relay | undefined;
  let selfId = "";
  let watcher: Watcher | undefined;
  let lastCtx: any;
  let uiBusy = false; // ui_prompt_start/end coalesce nested dialogs into one span
  let inFlight: Message[] = [];
  let awaitingRead: Message[] = [];
  let ackTimer: any;
  let errored = false; // last assistant message of the current run ended in an error
  // From a turn's first tool start to its turn_end: a steer sent then reaches the model right after the tools.
  let toolPhase = false;
  let steered = false; // inFlight went out as a steer, so it has no ack timer
  let modelDown = false;
  let probing = false;
  let backoffMs = probeMs;
  let probeTimer: any;

  const status = () => {
    if (!relay || !lastCtx?.hasUI) return;
    const n = relay.members(selfId).length;
    lastCtx.ui.setStatus("relay", `relay ${selfId}${n > 1 ? ` · ${n - 1} peer${n > 2 ? "s" : ""}` : ""}`);
  };

  const call = (name: string, args: Record<string, unknown>) => {
    if (!relay) throw new Error("agent-relay: session not started");
    const text = runTool(relay, selfId, readConfig().hopLimit, name, args);
    status();
    return { content: [{ type: "text", text }], details: {} };
  };

  // Idle mail starts a run; mid-run mail goes in only as a steer between a tool batch and the next model call.
  const idle = () => !uiBusy && (lastCtx?.isIdle?.() ?? true);

  const clearAck = () => {
    if (ackTimer !== undefined) clock.clearTimeout(ackTimer);
    ackTimer = undefined;
  };

  const clearProbe = () => {
    if (probeTimer !== undefined) clock.clearTimeout(probeTimer);
    probeTimer = undefined;
  };

  // One held batch per backoff window: probing lets the next idle flush through.
  const holdModel = () => {
    if (modelDown) backoffMs = Math.min(backoffMs * 2, PROBE_MAX_MS);
    else backoffMs = probeMs;
    modelDown = true;
    probing = false;
    clearProbe();
    probeTimer = clock.setTimeout(() => {
      probeTimer = undefined;
      probing = true;
      void watcher?.drain();
    }, backoffMs);
    probeTimer.unref?.();
  };

  const modelAlive = () => {
    modelDown = false;
    probing = false;
    backoffMs = probeMs;
    clearProbe();
  };

  // One collapsed batch per idle window. Returns false so the inbox keeps the files until ack().
  const deliver = (_m: Message): boolean => {
    if (!relay || inFlight.length) return false;
    if (modelDown && !probing) return false;
    const steer = !idle();
    // Mid-run, only between a tool batch and the next model call; a retry or backoff never sees mail.
    if (steer && (!toolPhase || errored)) return false;
    const batch = relay.pending(selfId);
    // An empty probe spends nothing, so it stays armed: the next message that arrives is the probe.
    if (!batch.length) return false;

    const head =
      batch.length > 1
        ? `${batch.length} relay messages arrived while you were busy; newest last, older ones may be outdated\n\n`
        : "";
    inFlight = batch;
    steered = steer;
    try {
      pi.sendMessage(
        { customType: "relay", content: head + batch.map(formatMessage).join("\n\n"), display: true },
        steer ? { triggerTurn: true, deliverAs: "steer" } : { triggerTurn: true },
      );
    } catch {
      inFlight = [];
      return false;
    }
    // A steer is acked when the loop injects it; an abort that clears the queue is settled in agent_settled.
    if (steer) {
      status();
      return false;
    }
    // sendMessage only reports failures through runner.emitError, so the ack comes from message_start.
    ackTimer = clock.setTimeout(() => {
      inFlight = [];
      ackTimer = undefined;
    }, ackMs);
    ackTimer.unref?.();
    status();
    return false;
  };

  const ack = () => {
    if (!relay || !inFlight.length) return;
    const batch = inFlight;
    inFlight = [];
    clearAck();
    for (const m of batch) relay.delivered(m);
    awaitingRead = [...awaitingRead, ...batch];
    status();
  };

  for (const t of TOOLS) {
    pi.registerTool({
      name: t.name,
      label: t.name,
      description: t.description,
      parameters: t.inputSchema,
      promptGuidelines: t.name === "relay_send" ? [INSTRUCTIONS] : undefined,
      async execute(_id: string, params: Record<string, unknown>) {
        return call(t.name, params);
      },
    });
  }

  pi.registerCommand("relay", {
    description: "agent-relay: show ID and peers, `/relay link <id>`, `/relay unlink`",
    handler: async (args: string, ctx: any) => {
      if (!relay) return;
      const [cmd, ...rest] = args.trim().split(/\s+/);
      try {
        if (cmd === "link") relay.link(selfId, rest.join(" "));
        else if (cmd === "unlink") relay.unlink(selfId);
        ctx.ui.notify(peersText(relay, selfId), "info");
      } catch (e) {
        ctx.ui.notify(String((e as Error).message), "error");
      }
      status();
    },
  });

  pi.on("session_start", async (_e: any, ctx: any) => {
    lastCtx = ctx;
    uiBusy = false;
    relay = relayFactory();
    const rec = relay.register({
      kind: "pi",
      pid: process.pid,
      cwd: ctx.sessionManager?.getCwd?.() ?? ctx.cwd ?? process.cwd(),
      id: process.env.AGENT_RELAY_ID,
      name: ctx.sessionManager?.getSessionName?.() || undefined,
    });
    selfId = rec.id;
    watcher?.close();
    watcher = relay.watch(selfId, deliver);
    status();
    await watcher.drain();
  });

  pi.on("message_start", async (e: any) => {
    if (e?.message?.role === "custom" && e.message.customType === "relay") ack();
  });

  pi.on("message_end", async (e: any) => {
    const m = e?.message;
    if (m?.role !== "assistant") return;
    // "aborted" is a user cancel, not an unavailable model.
    errored = m.stopReason !== "aborted" && (m.stopReason === "error" || !!m.errorMessage);
    if (!errored) modelAlive();
  });

  pi.on("tool_execution_start", async () => {
    toolPhase = true;
  });

  pi.on("tool_execution_end", async () => {
    await watcher?.drain();
  });

  pi.on("turn_end", async () => {
    toolPhase = false;
  });

  pi.on("agent_settled", async () => {
    toolPhase = false;
    // A steer that never reached the model was dropped with the queue; the inbox still has it.
    if (steered && inFlight.length) inFlight = [];
    steered = false;
    if (relay && awaitingRead.length) {
      for (const m of awaitingRead) relay.read(m);
      awaitingRead = [];
    }
    if (errored) holdModel();
    await watcher?.drain();
  });

  pi.on("ui_prompt_start", async () => {
    uiBusy = true;
  });

  pi.on("ui_prompt_end", async () => {
    uiBusy = false;
    await watcher?.drain();
  });

  pi.on("input", async (e: any) => {
    if (relay && e.source === "interactive") relay.markHuman(selfId);
  });

  pi.on("session_shutdown", async () => {
    watcher?.close();
    watcher = undefined;
    clearAck();
    clearProbe();
    modelDown = false;
    probing = false;
    toolPhase = false;
    steered = false;
    inFlight = [];
    awaitingRead = [];
  });
}
