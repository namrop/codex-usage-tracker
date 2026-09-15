import assert from "node:assert/strict";
import { test } from "node:test";
import { weeklyCycleKey } from "../src/lib/weeklyUtilization";

test("repeated reset targets remain distinct boundary-evidence rows", () => {
  const window = { provider: "openai-codex", provider_account_index: 1, quota_name: "week" };
  const first = { resets_at: "2026-09-15T01:56:00Z", last_observed_at: "2026-09-09T16:00:00Z" };
  const second = { ...first, last_observed_at: "2026-09-12T08:00:00Z" };
  const keys = [weeklyCycleKey(window, first, 0), weeklyCycleKey(window, second, 1), weeklyCycleKey(window, second, 2)];
  assert.equal(new Set(keys).size, 3);
  assert.equal(keys[0], weeklyCycleKey(window, first, 0));
});
