import { test } from "node:test";
import assert from "node:assert/strict";
import turnTimer, { lib } from "../extensions/turn-timer.ts";

test("formatDuration formats seconds under a minute", () => {
  assert.equal(lib.formatDuration(0), "0s");
  assert.equal(lib.formatDuration(1), "1s");
  assert.equal(lib.formatDuration(42), "42s");
  assert.equal(lib.formatDuration(59), "59s");
  assert.equal(lib.formatDuration(42.8), "42s");
  assert.equal(lib.formatDuration(-5), "0s");
});

test("formatDuration formats minutes and seconds under an hour", () => {
  assert.equal(lib.formatDuration(60), "1m");
  assert.equal(lib.formatDuration(77), "1m 17s");
  assert.equal(lib.formatDuration(119), "1m 59s");
  assert.equal(lib.formatDuration(120), "2m");
  assert.equal(lib.formatDuration(3599), "59m 59s");
});

test("formatDuration formats hours and minutes for durations >= 1 hour", () => {
  assert.equal(lib.formatDuration(3600), "1h");
  assert.equal(lib.formatDuration(3720), "1h 2m");
  assert.equal(lib.formatDuration(3725), "1h 2m");
  assert.equal(lib.formatDuration(7200), "2h");
  assert.equal(lib.formatDuration(7380), "2h 3m");
});

test("formatTime formats 12-hour AM/PM time with minutes padded", () => {
  assert.equal(lib.formatTime(new Date(2026, 10, 18, 9, 49)), "9:49 AM");
  assert.equal(lib.formatTime(new Date(2026, 10, 18, 21, 49)), "9:49 PM");
  assert.equal(lib.formatTime(new Date(2026, 10, 18, 0, 0)), "12:00 AM");
  assert.equal(lib.formatTime(new Date(2026, 10, 18, 12, 0)), "12:00 PM");
  assert.equal(lib.formatTime(new Date(2026, 10, 18, 12, 5)), "12:05 PM");
  assert.equal(lib.formatTime(new Date(2026, 10, 18, 23, 59)), "11:59 PM");
});

test("compose builds the status line using the idle glyph", () => {
  const at = new Date(2026, 10, 18, 21, 49);
  assert.equal(lib.compose(77, at), `${lib.IDLE} Worked for 1m 17s · done 9:49 PM`);
  assert.equal(lib.compose(42, at), `${lib.IDLE} Worked for 42s · done 9:49 PM`);
  assert.equal(lib.compose(3720, at), `${lib.IDLE} Worked for 1h 2m · done 9:49 PM`);
  assert.equal(lib.compose(77, at, "★"), "★ Worked for 1m 17s · done 9:49 PM");
});

type Handler = (event: any, ctx: any) => void;

function harness(hasUI = true) {
  const handlers = new Map<string, Handler[]>();
  const notifications: string[] = [];

  const pi = {
    on(event: string, handler: Handler) {
      handlers.set(event, [...(handlers.get(event) ?? []), handler]);
      return () => {};
    },
  };

  const ctx = {
    hasUI,
    ui: {
      notify(msg: string) {
        notifications.push(msg);
      },
    },
  };

  const emit = (event: string, payload: any = {}, customCtx: any = ctx) => {
    for (const handler of handlers.get(event) ?? []) handler({ type: event, ...payload }, customCtx);
  };

  turnTimer(pi as any);
  return { emit, notifications, ctx };
}

test("agent_start and agent_end notify the elapsed time and completion time", () => {
  const originalNow = Date.now;
  let current = new Date(2026, 10, 18, 21, 47, 43).getTime();
  Date.now = () => current;

  try {
    const { emit, notifications } = harness();
    emit("agent_start");

    current += 77 * 1000;
    emit("agent_end", { messages: [] });

    assert.equal(notifications.length, 1);
    assert.equal(notifications[0], `${lib.IDLE} Worked for 1m 17s · done 9:49 PM`);
  } finally {
    Date.now = originalNow;
  }
});

test("agent_end without agent_start does not notify", () => {
  const { emit, notifications } = harness();
  emit("agent_end", { messages: [] });
  assert.equal(notifications.length, 0);
});

test("agent_end does not notify when hasUI is false", () => {
  const originalNow = Date.now;
  let current = 1000;
  Date.now = () => current;

  try {
    const { emit, notifications } = harness(false);
    emit("agent_start");
    current += 42 * 1000;
    emit("agent_end", { messages: [] });

    assert.equal(notifications.length, 0);
  } finally {
    Date.now = originalNow;
  }
});

test("session_start clears an in-flight timer", () => {
  const originalNow = Date.now;
  let current = 1000;
  Date.now = () => current;

  try {
    const { emit, notifications } = harness();
    emit("agent_start");
    current += 42 * 1000;

    emit("session_start", { reason: "startup" });
    emit("agent_end", { messages: [] });

    assert.equal(notifications.length, 0);
  } finally {
    Date.now = originalNow;
  }
});

test("consecutive agent runs measure each run independently", () => {
  const originalNow = Date.now;
  let current = new Date(2026, 10, 18, 21, 0, 0).getTime();
  Date.now = () => current;

  try {
    const { emit, notifications } = harness();

    // First run: 42s
    emit("agent_start");
    current += 42 * 1000;
    emit("agent_end", { messages: [] });

    assert.equal(notifications.length, 1);
    assert.equal(notifications[0], `${lib.IDLE} Worked for 42s · done 9:00 PM`);

    // Advance time while idle
    current += 10 * 60 * 1000;

    // Second run: 77s
    emit("agent_start");
    current += 77 * 1000;
    emit("agent_end", { messages: [] });

    assert.equal(notifications.length, 2);
    assert.equal(notifications[1], `${lib.IDLE} Worked for 1m 17s · done 9:11 PM`);
  } finally {
    Date.now = originalNow;
  }
});
