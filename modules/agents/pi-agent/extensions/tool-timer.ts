import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

const TICK_MS = 1000;
const ANSI_ALL = /\u001b\[[0-9;]*m/g;

import { formatToolDuration } from "./tool-format.ts";

export { formatToolDuration };

/** Columns a rendered line occupies, ignoring colour codes. */
export function visibleLength(text: string): number {
  return text.replace(ANSI_ALL, "").length;
}

/** A colour/font escape sequence at the start of a string slice. */
const ANSI_SEQ = /^\u001b\[[0-9;]*m/;

/** Escape sequences in front of the first visible character: the row's background, in practice. */
export function leadingAnsi(text: string): string {
  let out = "";
  let rest = text;
  for (let code = rest.match(ANSI_SEQ); code; code = rest.match(ANSI_SEQ)) {
    out += code[0];
    rest = rest.slice(code[0].length);
  }
  return out;
}

/**
 * Put the timer inside the row's trailing padding, so the line keeps its width and background and no
 * extra row appears. Undefined when the text reaches the padding, or the padding is not blank spaces.
 */
export function placeInPadding(head: string, suffix: string): string | undefined {
  let at = 0;
  let afterLastText = -1;
  while (at < head.length) {
    const code = head.slice(at).match(ANSI_SEQ);
    if (code) {
      at += code[0].length;
      continue;
    }
    if (head[at] !== " ") afterLastText = at + 1;
    at++;
  }
  if (afterLastText < 0) return undefined;

  const need = visibleLength(suffix);
  const rest = head.slice(afterLastText);
  let taken = 0;
  let end = 0;
  while (end < rest.length && taken < need) {
    const code = rest.slice(end).match(ANSI_SEQ);
    if (code) {
      end += code[0].length;
      continue;
    }
    if (rest[end] !== " ") return undefined;
    end++;
    taken++;
  }
  if (taken < need) return undefined;
  return head.slice(0, afterLastText) + suffix + leadingAnsi(head) + rest.slice(end);
}

/**
 * Append the timer to the row's first line, or give it a line of its own when the line is already full.
 * A suffix that wrapped mid-word would be unreadable, so it never overflows.
 */
export function withSuffix(lines: string[], suffix: string, width: number): string[] {
  if (!suffix || lines.length === 0) return lines;
  const head = lines[0] ?? "";
  const padded = placeInPadding(head, suffix);
  if (padded !== undefined) return [padded, ...lines.slice(1)];
  if (visibleLength(head) + visibleLength(suffix) <= width) return [head + suffix, ...lines.slice(1)];
  return [head, suffix.trim(), ...lines.slice(1)];
}

type ClockState = {
  toolTimerStartedAt?: number;
  toolTimerInterval?: ReturnType<typeof setInterval>;
};

/** The row's component, with the timer glued to its first line. */
class TimedCall {
  base: any;
  suffix: string;

  constructor(base: any, suffix: string) {
    this.base = base;
    this.suffix = suffix;
  }

  render(width: number): string[] {
    return withSuffix(this.base.render(width), this.suffix, width);
  }

  invalidate(): void {
    this.base.invalidate?.();
  }

  handleMouse(event: any): any {
    return this.base.handleMouse?.(event);
  }
}

function stopClock(state: ClockState): void {
  if (state.toolTimerInterval !== undefined) {
    clearInterval(state.toolTimerInterval);
    state.toolTimerInterval = undefined;
  }
}

/**
 * ` (12s)` while the tool runs, ` (20.0s)` once it is done. Undefined when the row has no clock to show:
 * not started yet, or a result stored without a duration.
 */
export function timerSuffix(state: ClockState, context: any, theme: any): string | undefined {
  if (!context.isPartial) {
    stopClock(state);
    const ms = typeof context.durationMs === "number" ? context.durationMs : undefined;
    if (ms !== undefined) return theme.fg("muted", ` (${formatToolDuration(ms)})`);
    if (state.toolTimerStartedAt === undefined) return undefined;
    return theme.fg("muted", ` (${formatToolDuration(Date.now() - state.toolTimerStartedAt)})`);
  }
  const startedAt = state.toolTimerStartedAt;
  if (startedAt === undefined) return undefined;
  if (state.toolTimerInterval === undefined) {
    state.toolTimerInterval = setInterval(() => context.invalidate(), TICK_MS);
    (state.toolTimerInterval as any)?.unref?.();
  }
  return theme.fg("muted", ` (${formatToolDuration(Date.now() - startedAt)})`);
}

export const lib = { formatToolDuration, visibleLength, withSuffix, placeInPadding, leadingAnsi, timerSuffix, TICK_MS };

/**
 * Show how long a tool row has been running, on every row pi does not already time.
 *
 * pi clocks bash, powershell and codemode itself, from the result renderer; rows without one (the `ipython`
 * tool of RLM mode) had no time at all. The timer rides on the call line, so a row keeps it while running
 * and after it finishes, and tool output is never touched.
 */
export default function toolTimer(pi: ExtensionAPI): void {
  pi.registerToolRenderer((_toolName, next) => {
    const base = next();
    const renderCall = base?.renderCall;
    // No call renderer: keep whatever pi would draw, which has nowhere to hang a timer.
    if (!base || !renderCall) return base;
    return {
      ...base,
      renderCall(args: any, theme: any, context: any) {
        const state: ClockState & { toolTimerBase?: any; startedAt?: number } = context.state;
        // The base renderer reuses the component it made last time; hand it that one, never our wrapper.
        const inner = renderCall(args, theme, { ...context, lastComponent: state.toolTimerBase });
        state.toolTimerBase = inner;
        // startedAt means pi's own startToolTimer owns this row and prints the time under the output.
        if (state.startedAt !== undefined) return inner;
        if (context.executionStarted) state.toolTimerStartedAt ??= Date.now();
        const suffix = timerSuffix(state, context, theme);
        return suffix === undefined ? inner : new TimedCall(inner, suffix);
      },
    };
  });
}
