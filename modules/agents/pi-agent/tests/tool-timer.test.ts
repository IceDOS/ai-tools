import { test } from "node:test";
import assert from "node:assert/strict";
import toolTimer, { lib } from "../extensions/tool-timer.ts";
import type { ToolRenderers } from "@earendil-works/pi-coding-agent";

const theme = { fg: (_color: string, text: string) => text };

const component = (lines: string[], log?: string[]) => ({
  render(_width: number) {
    return lines;
  },
  invalidate() {
    log?.push("invalidate");
  },
});

function harness(base: ToolRenderers | undefined) {
  const resolvers: any[] = [];
  const pi = {
    registerToolRenderer(resolver: any) {
      resolvers.push(resolver);
    },
  };
  toolTimer(pi as any);
  const chain = (index: number, toolName: string): ToolRenderers | undefined =>
    index < resolvers.length
      ? resolvers[index](toolName, () => chain(index + 1, toolName))
      : base;
  return { resolve: (toolName: string) => chain(0, toolName) };
}

test("formatToolDuration matches pi's own tool rows", () => {
  assert.equal(lib.formatToolDuration(0), "0.0s");
  assert.equal(lib.formatToolDuration(4200), "4.2s");
  assert.equal(lib.formatToolDuration(59999), "60.0s");
  assert.equal(lib.formatToolDuration(60000), "1m 0s");
  assert.equal(lib.formatToolDuration(90500), "1m 30s");
  assert.equal(lib.formatToolDuration(3725000), "1h 2m 5s");
  assert.equal(lib.formatToolDuration(-500), "0.0s");
});

test("visibleLength ignores colour codes", () => {
  assert.equal(lib.visibleLength("\u001b[2mdim\u001b[0m"), 3);
  assert.equal(lib.visibleLength("plain"), 5);
});

test("withSuffix appends to the head line when it fits", () => {
  assert.deepEqual(lib.withSuffix(["head", "tail"], " (12s)", 40), ["head (12s)", "tail"]);
  assert.deepEqual(lib.withSuffix(["\u001b[1mhead\u001b[0m"], " (12s)", 20), ["\u001b[1mhead\u001b[0m (12s)"]);
});

test("withSuffix gives the timer its own line when the head fills the width", () => {
  assert.deepEqual(lib.withSuffix(["x".repeat(38), "tail"], " (12s)", 40), ["x".repeat(38), "(12s)", "tail"]);
});

test("withSuffix leaves lines alone without a suffix or content", () => {
  assert.deepEqual(lib.withSuffix(["head"], "", 40), ["head"]);
  assert.deepEqual(lib.withSuffix([], " (12s)", 40), []);
});

test("leadingAnsi keeps the escape sequences in front of the text", () => {
  assert.equal(lib.leadingAnsi("\u001b[48;5;235m\u001b[2mhead"), "\u001b[48;5;235m\u001b[2m");
  assert.equal(lib.leadingAnsi("head"), "");
});

test("placeInPadding writes the timer into the blank tail of a padded row", () => {
  // pi's Text pads every line to the viewport width and paints the row background behind that padding.
  const bg = "\u001b[48;5;235m";
  const padded = `${bg}head${" ".repeat(116)}\u001b[0m`;
  const placed = lib.placeInPadding(padded, "\u001b[2m (12s)\u001b[0m")!;
  assert.equal(placed, `${bg}head\u001b[2m (12s)\u001b[0m${bg}${" ".repeat(110)}\u001b[0m`);
  assert.equal(lib.visibleLength(placed), 120, "the line keeps its width");
});

test("placeInPadding gives up when the row is full or the padding is not blank", () => {
  assert.equal(lib.placeInPadding("x".repeat(40), " (12s)"), undefined);
  assert.equal(lib.placeInPadding(`head${" ".repeat(3)}`, " (12s)"), undefined);
  assert.equal(lib.placeInPadding(" ".repeat(40), " (12s)"), undefined);
});

test("a row without a call renderer is left exactly as it was", () => {
  const base = { renderResult: component(["out"]) };
  assert.equal(harness(base).resolve("tool"), base);

  const none = harness(undefined).resolve("tool");
  assert.equal(none, undefined);
});

test("wrapping keeps the base's other renderers", () => {
  const base = { renderShell: "self" as const, renderCall: () => component(["head"]), renderResult: component(["out"]) };
  const renderers = harness(base).resolve("tool")!;
  assert.equal(renderers.renderShell, "self");
  assert.equal(renderers.renderResult, base.renderResult);
  assert.notEqual(renderers.renderCall, base.renderCall);
});

test("a running row carries the elapsed time and redraws every second", () => {
  const originalNow = Date.now;
  const originalSetInterval = globalThis.setInterval;
  const intervals: (() => void)[] = [];
  let invalidated = 0;
  let now = 1_000_000;
  Date.now = () => now;
  (globalThis as any).setInterval = (fn: () => void, ms: number) => {
    assert.equal(ms, 1000);
    intervals.push(fn);
    return { unref() {} };
  };
  try {
    const { resolve } = harness({ renderCall: () => component(["head"]) });
    const renderers = resolve("tool")!;
    const context: any = { args: {}, state: {}, isPartial: true, executionStarted: true, invalidate: () => invalidated++ };
    const draw = () => (renderers.renderCall as any)(context.args, theme, context);

    assert.deepEqual(draw().render(40), ["head (0.0s)"]);
    now += 4200;
    assert.deepEqual(draw().render(40), ["head (4.2s)"]);
    assert.equal(intervals.length, 1, "one ticker per row");

    intervals[0]!();
    assert.equal(invalidated, 1);

    context.isPartial = false;
    context.durationMs = 20_000;
    assert.deepEqual(draw().render(40), ["head (20.0s)"]);
  } finally {
    Date.now = originalNow;
    globalThis.setInterval = originalSetInterval;
  }
});

test("a finished row clears its ticker", () => {
  const originalSetInterval = globalThis.setInterval;
  const originalClearInterval = globalThis.clearInterval;
  const cleared: any[] = [];
  const handle = { unref() {} };
  (globalThis as any).setInterval = () => handle;
  (globalThis as any).clearInterval = (timer: any) => cleared.push(timer);
  try {
    const { resolve } = harness({ renderCall: () => component(["head"]) });
    const renderers = resolve("tool")!;
    const context: any = { args: {}, state: {}, isPartial: true, executionStarted: true, durationMs: undefined };
    (renderers.renderCall as any)(context.args, theme, context);
    assert.equal(context.state.toolTimerInterval, handle);
    context.isPartial = false;
    context.durationMs = 1500;
    (renderers.renderCall as any)(context.args, theme, context);
    assert.deepEqual(cleared, [handle]);
    assert.equal(context.state.toolTimerInterval, undefined);
  } finally {
    globalThis.setInterval = originalSetInterval;
    globalThis.clearInterval = originalClearInterval;
  }
});

test("a stored result shows its recorded duration without a running clock", () => {
  const { resolve } = harness({ renderCall: () => component(["head"]) });
  const renderers = resolve("tool")!;
  const context: any = { args: {}, state: {}, isPartial: false, executionStarted: false, durationMs: 20_000 };
  assert.deepEqual((renderers.renderCall as any)(context.args, theme, context).render(40), ["head (20.0s)"]);
  assert.equal(context.state.toolTimerStartedAt, undefined, "history does not start a clock");
});

test("a row pi already times is not timed twice", () => {
  const inner = component(["head"]);
  const base = {
    renderCall: (_args: any, _theme: any, context: any) => {
      context.state.startedAt = Date.now();
      return inner;
    },
  };
  const { resolve } = harness(base);
  const renderers = resolve("tool")!;
  const context: any = { args: {}, state: {}, isPartial: true, executionStarted: true };
  assert.equal((renderers.renderCall as any)(context.args, theme, context), inner);
  assert.equal(context.state.toolTimerStartedAt, undefined);
});

test("nothing is timed before the tool starts running", () => {
  const inner = component(["head"]);
  const { resolve } = harness({ renderCall: () => inner });
  const renderers = resolve("tool")!;
  const context: any = { args: {}, state: {}, isPartial: true, executionStarted: false };
  assert.equal((renderers.renderCall as any)(context.args, theme, context), inner);
});

test("the base renderer gets its own component back, not the wrapper", () => {
  const made: any[] = [];
  const seen: any[] = [];
  const base = {
    renderCall: (_args: any, _theme: any, context: any) => {
      seen.push(context.lastComponent);
      const next = component(["head"]);
      made.push(next);
      return next;
    },
  };
  const { resolve } = harness(base);
  const renderers = resolve("tool")!;
  const context: any = { args: {}, state: {}, isPartial: true, executionStarted: true };

  const first = (renderers.renderCall as any)(context.args, theme, { ...context, lastComponent: undefined });
  (renderers.renderCall as any)(context.args, theme, { ...context, lastComponent: first });

  assert.equal(seen[0], undefined);
  assert.equal(seen[1], made[0]);
});
