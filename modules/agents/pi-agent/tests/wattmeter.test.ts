import { test } from "node:test";
import assert from "node:assert/strict";
import { aiReading } from "../extensions/cost-footer/wattmeter.ts";

const snap = JSON.stringify({
  ts: 1000,
  interval: 2,
  currency: "€",
  ai: { active: true, watts: 27, windows: [{ label: "1h", wh: 10, cost: 0.0016 }], all: { wh: 10, cost: 0.0016 } },
});

test("fresh snapshot gives watts, currency and window costs", () => {
  assert.deepEqual(aiReading(snap, 1_003_000), { currency: "€", watts: 27, windows: [["1h", 0.0016]] });
});

test("a snapshot older than 3 intervals is ignored", () => {
  assert.equal(aiReading(snap, 1_007_000), null);
});

test("bad JSON or a snapshot without ai is ignored", () => {
  assert.equal(aiReading("{", 1_003_000), null);
  assert.equal(aiReading(JSON.stringify({ ts: 1000, interval: 2 }), 1_003_000), null);
});
