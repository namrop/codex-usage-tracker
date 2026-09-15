import assert from "node:assert/strict";
import { test } from "node:test";
import { reconciliationText, type GoUsagePayload } from "../src/lib/goUsage";

function payload(status: string, complete = true): GoUsagePayload {
  return { reconciliation: { status, modeled_pct: "37.333", native_lower_pct: "18", native_upper_pct: "19", modeled_complete: complete } } as GoUsagePayload;
}

test("Go does not compare activity newer than the native snapshot", () => {
  const text = reconciliationText(payload("usage_ahead_of_native"));
  assert.match(text, /newer than the native reading/i);
  assert.match(text, /not compared/i);
});

test("Go incomplete model coverage cannot claim reconciliation", () => {
  const text = reconciliationText(payload("modeled_incomplete", false));
  assert.match(text, /not compared/i);
  assert.doesNotMatch(text, /within rounding/i);
});
