import assert from "node:assert/strict";
import { describe, it } from "node:test";
import { coverageNote, deriveSearchRows } from "../src/lib/searches";

// Keeper, 2026-09-25 (msg 1553067845766742228): "brave needs usage tracking too".
const PAYLOAD = {
  backends: [
    { backend: "brave", calls: 1100, ok: 1090, rate_limited: 10, error: 0 },
    { backend: "zai", calls: 300, ok: 280, rate_limited: 20, error: 0 },
  ],
  month_to_date: {
    month_start: "2026-09-01",
    ledger_since: "2026-09-11T10:35:00.971697Z",
    backends: [
      { backend: "brave", calls: 2400, billable_calls: 2380, by_harness: { hermes: 2330, openclaw: 70 },
        rate_usd: "0.005", estimated_usd: "11.90" },
      { backend: "keyless", calls: 900, billable_calls: 900, by_harness: { hermes: 900 } },
      { backend: "zai", calls: 600, billable_calls: 580, by_harness: { hermes: 600 } },
    ],
  },
};

describe("web searches", () => {
  it("joins the week and the month per backend, busiest first", () => {
    const rows = deriveSearchRows(PAYLOAD);
    assert.deepEqual(rows.map((row) => row.backend), ["brave", "keyless", "zai"]);
    const brave = rows[0];
    assert.equal(brave.label, "Brave Search");
    assert.equal(brave.billableCalls, 2380);
    assert.equal(brave.estimatedUsd, 11.9);
    assert.equal(brave.rateUsd, 0.005);
    assert.deepEqual(brave.byHarness, [["hermes", 2330], ["openclaw", 70]]);
    assert.equal(brave.weekRateLimited, 10);
    assert.equal(rows[1].weekCalls, 0);
    assert.equal(rows[2].estimatedUsd, null);
  });

  it("says when the ledger starts after the month does", () => {
    assert.equal(coverageNote(PAYLOAD), "The ledger starts Sep 11, so this month's counts are a floor.");
    assert.equal(coverageNote({ month_to_date: { ...PAYLOAD.month_to_date, ledger_since: "2026-10-01T00:00:03Z", month_start: "2026-10-01" } }), null);
    assert.deepEqual(deriveSearchRows(null), []);
  });
});
