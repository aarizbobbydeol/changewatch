// Run with: node --test app/lib/ (Node 22.18 or newer reads the TypeScript file directly)
import assert from "node:assert/strict";
import test from "node:test";
import { logClock } from "./time.ts";

test("a log line's time is shown as HH:MM:SS in UTC", () => {
  assert.equal(logClock(1761400000.25), "13:46:40");
  assert.equal(logClock(0), "00:00:00");
});

test("a time no browser can show (sent in nanoseconds, or not a number) does not break the page", () => {
  assert.throws(() => new Date(1.7e18 * 1000).toISOString(), RangeError); // what the page used to do
  assert.equal(logClock(1.7e18), "--:--:--");
  assert.equal(logClock(Number.NaN), "--:--:--");
  assert.equal(logClock(undefined), "--:--:--");
});
