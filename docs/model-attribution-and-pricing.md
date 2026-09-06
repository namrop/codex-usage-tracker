# Model attribution and list-price estimates

Public model names are reviewed labels, not raw provider IDs. Changes must be
accepted by all consumers before the producer emits them: Namrop's route
validator, the fleet publish-time guard, and the fleet watchdog. Public v3 has
a sixteen-model-row ceiling; Namrop's legacy v2 remains frozen at twelve.
Unrecognized models stay in `Other`, and overflow is still bounded. Do not
bypass allowlists or change exact token/request reconciliation to improve a
chart's appearance.

## Private visibility

The private chart defaults to the top five models. **All models** exposes all
returned series, bounded to twenty-four named series plus a labeled overflow
series. The provider/model detail table remains available independently.

A read-only diagnostic shows IDs missing a public mapping or price coverage:

```sh
codex-usage-tracker unmapped-models \
  --sqlite ~/.local/state/codex-usage-tracker/usage_events.sqlite3 \
  --hours 168
```

The diagnostic anchors on the newest ledger fact so a stalled collector does
not produce a falsely empty report. It applies the date bound in SQLite,
checks row integrity, caps the scan, and exposes `scan_truncated`. Its raw
inventory counts are not a corrected accounting projection. Output stays
private: a name appearing here is not permission to publish it.

## Pricing basis

Actual subscription billing remains separate from the list-price estimate.
Astra and GPT-5.6 Sol use explicit standard and long-context rates retrieved
from vendor model pages on 2026-09-06. The threshold is evaluated per request,
including cached prompt tokens, not against an aggregated model total.
Reasoning is already included in canonical output and is not billed twice.

- https://developers.openai.com/api/docs/models/gpt-6-astra
- https://developers.openai.com/api/docs/models/gpt-5.6-sol
- https://developers.openai.com/api/docs/pricing

OpenAI/Codex models without a rate are unpriced coverage, not silently assigned
the flagship price. The estimate assumes the standard service tier because
the canonical ledger lacks tier metadata. Historical aggregates cannot reveal
per-request context length and are explicitly marked as aggregate uncertainty.
Signed correction rows without original request-context metadata also use the
standard rate; they can leave a residual estimate difference against an original
long-context request and make aggregate disclosure counters net-negative.
Sol's promotional rate requires re-verification after 2026-11-21.

Keep `_rates_for(provider, model)`'s rate-only contract for quota derivation;
context-aware estimation uses `_rates_with_context` separately. Run the full
suite, not only the pricing tests, when changing this shared code.

## Activation

Deploy accepting validators first, then the producer/dashboard. NixOS switching
remains keeper-authorized. A built package or a passing preview is not proof
of production activation: verify the live systemd paths, fresh published JSON,
Namrop API response, and rendered model list after activation.
