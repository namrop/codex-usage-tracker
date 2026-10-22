"""List-price estimation over canonical token aggregates.

Keeper-ruled 2026-08-30: "run the token data and the cached token data
through the pricing info for the providers to create an estimate" — the
subscription lanes report $0 actual cost, so the useful number is what the
same traffic WOULD cost at providers' list prices.

The table below is the pricing source of truth and is deliberately a
commented Python dict, not JSON, so every rate can carry its provenance.
Rates are USD per MILLION tokens. Entries marked ASSUMED were seeded
2026-08-30 from the nearest known tier and want keeper verification; a
model with no entry (and no provider default) counts as unpriced and is
reported as coverage, never silently priced.

Reasoning tokens are NOT billed on top of output tokens: canonical
``output_tokens`` already includes reasoning (see README, "reasoning is
diagnostic and is not added twice where canonical output already includes
it"), so the estimate bills ``output_tokens`` alone at the output rate.

Coverage limitations, stated rather than papered over (see ``RATE_BASIS``):

- Service tiers are NOT modeled. OpenAI's Batch and Flex tiers bill at 0.5x
  and Fast/Priority at 2x, and the canonical ledger does not record which
  tier served a request, so every estimate assumes the standard tier and
  says so through the ``rate_basis`` field. The estimate is a list-price
  ESTIMATE, never a claim of actual billed rate.
- ``historical_aggregate`` rows bundle many calls into one row, so their
  summed prompt cannot classify any single request's context length. They
  price at the standard-context rate and are reported separately as
  ``aggregate_context_tokens``.
- Long-context thresholds are verified per model. Astra and GPT-5.6 Sol
  independently document the same 272,000-token boundary. Models awaiting
  threshold evidence can be listed in ``LONG_CONTEXT_UNVERIFIED``; this set
  is currently empty.
- DeepSeek Flash has time-of-day API tariffs. The current bucket shape does
  not retain a peak/off-peak classification, so its published OFF-PEAK rate
  is a lower-bound list-price estimate, not an actual-bill reconstruction;
  peak-period requests cost twice as much. Its cache-write charge is not
  published and is recorded as zero (an additional possible understatement).

Provenance note: the 2026-09-06 OpenAI rates below were retrieved from the
vendor pages by the parent agent. They are dated source observations, not
keeper rulings, and remain list-price estimates under the stated limits.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Dict, Iterable, Mapping, Optional, Tuple

_MTOK = Decimal(1_000_000)

# The gpt-6-astra long-context meter, read from
# developers.openai.com/api/docs/models/gpt-6-astra and /api/docs/pricing by
# the parent agent on 2026-09-06: a request whose PROMPT exceeds this many
# tokens bills every token of that request — input, cache read, cache write
# and output alike — at the long-context rate.
#
# Canonical ``input_tokens`` EXCLUDES cache, so the prompt this threshold
# tests is input + cache_read + cache_write for ONE request. A model's
# aggregate token total says nothing about it.
#
# This threshold is applied ONLY to models whose long meter carries it (a
# nested "long" table). It is NOT generalized to other OpenAI models.
LONG_CONTEXT_PROMPT_TOKENS = 272_000

# (provider, model) pairs that publish a long-context rate we deliberately do
# NOT apply, because the token threshold that triggers it has not been
# verified for that model. Their traffic prices at the short rate and the
# estimate reports the affected tokens, so the understatement is visible
# instead of being hidden behind a borrowed threshold.
#
# 2026-09-24 widens the meaning slightly, in the same spirit: grok-4.7's
# threshold IS verified — it is simply 200,000 tokens, not the single global
# 272,000 above. A model whose real cliff the global constant cannot express
# belongs here for exactly the same reason as an unverified one: applying the
# wrong boundary misprices in both directions, and a reported understatement
# beats a confident wrong number. The durable fix is a per-model threshold.
LONG_CONTEXT_UNVERIFIED: frozenset[tuple[str, str]] = frozenset({
    ("openrouter", "x-ai/grok-4.7"),
})

# Which service tier every estimate assumes. Batch/Flex (0.5x) and
# Fast/Priority (2x) are not modeled because the ledger does not record the
# tier; the estimate carries this marker so no consumer can read it as an
# actual-rate figure.
RATE_BASIS = "standard"


# Providers whose traffic IS another provider's models behind a local proxy
# label. Meridian is a loopback Anthropic-Messages bridge onto Claude Max
# subscriptions, so its models are Anthropic's and Anthropic's published
# rates are the correct list-price counterfactual for them.
#
# Added 2026-09-24 after a coverage audit found that ~1.3B tokens of Claude
# traffic in 14 days priced as NOTHING: every Claude call arrives labelled
# ``custom:meridian-yugen`` / ``custom:meridian-primary``, and no such
# provider key existed here, so the whole lane fell out of the estimate as
# unpriced coverage. Aliasing beats copying the anthropic rate table under
# more keys, which would drift the moment one copy is edited.
#
# Bare ``custom`` is deliberately NOT aliased. CORRECTED 2026-09-24 by the
# nightly price-coverage job: the earlier note here called it "residue of an
# older route", but it is live traffic. Every bare-``custom`` row in the
# 14-day window is a cron session from a job that jobs.json pins to
# ``provider: custom:meridian-primary`` or ``custom:meridian-yugen``. Their
# ``billing_base_url`` is a Meridian port (:3458 / :3457) and their
# ``model_reported`` is a real Claude id. Other cron runs on the same ports,
# including at least one pinned job (9ba33bba64c3), record the full
# ``custom:meridian-*`` label. So the writer loses the provider name on some
# cron runs; the exact trigger is not established. The fix belongs in the
# Hermes ledger writer, not here. An alias would price these rows correctly
# today, but it would hide the writer defect and claim that every anonymous
# custom endpoint is Meridian.
PROVIDER_RATE_ALIASES: Dict[str, str] = {
    "custom:meridian-yugen": "anthropic",
    "custom:meridian-primary": "anthropic",
}


def resolve_rate_provider(provider: str, model: str = "") -> str:
    """Map a transport/proxy provider label onto its rate-table provider.

    Identity for anything unaliased, so an unknown provider still resolves to
    unpriced coverage rather than borrowing a neighbour's rates.

    An alias applies ONLY when the target provider carries an EXACT entry for
    this model. That restriction is load-bearing, not caution: a plain
    provider-level alias would also inherit the target's ``default``, and the
    Meridian lanes carry a few non-Claude ids (``gpt-5.5``, ``gpt-6-sol`` —
    residue of routed auxiliary calls). Aliasing those wholesale would have
    priced GPT traffic at Anthropic's conservative-high Fable default, which
    is precisely the silent-fabrication failure this table exists to avoid.
    Unmatched models fall through to their own provider and surface as
    coverage, which is the honest answer.

    Consequence worth stating: a NEW Claude model reaching Meridian is
    unpriced until it gets a real entry. That is the intended wake signal for
    the nightly price-coverage job, not a regression.
    """
    target = PROVIDER_RATE_ALIASES.get(provider)
    if target is None:
        return provider
    entry = LIST_PRICES.get(target)
    if entry and entry["models"].get(model) is not None:
        return target
    return provider


def request_context_basis(row: Mapping[str, Any]) -> str:
    """Classify ONE canonical usage row for long-context pricing.

    Returns ``"long"`` when this single request's prompt exceeds
    :data:`LONG_CONTEXT_PROMPT_TOKENS`, ``"standard"`` when it does not, and
    ``"aggregate"`` when the row is not a single request at all — a
    ``historical_aggregate`` bundles many calls, so its summed prompt is not
    evidence that any one call was long-context. Aggregates price at the
    standard rate and are reported as such rather than guessed either way.
    """
    prompt = sum(
        int(row.get(field) or 0)
        for field in ("input_tokens", "cache_read_tokens", "cache_write_tokens")
    )
    # A correction that reverses one whole request (it names the record kind
    # it reverses) prices on that request's own basis, so the pair nets to
    # zero at either tier (SIL-77 OpenClaw Codex backfill).
    if row.get("record_kind") == "correction" and row.get("x_corrected_record_kind") == "api_attempt":
        prompt = abs(prompt)
    elif row.get("record_kind") != "api_attempt":
        return "aggregate"
    return "long" if prompt > LONG_CONTEXT_PROMPT_TOKENS else "standard"

# provider -> {"models": {model: rates}, "default": rates | None}
# rates: {"input": x, "output": y, "cache_read": z, "cache_write": w}
LIST_PRICES: Dict[str, Dict[str, Any]] = {
    # Looked up online 2026-08-30 (keeper-directed: "the real price tables").
    # Anthropic: published API list prices, cache write = 5-minute tier
    # (1-hour write is 2x base input — not modeled; our traffic is 5m).
    "anthropic": {
        "models": {
            "claude-fable-5": {"input": "10", "output": "50", "cache_read": "1.00", "cache_write": "12.50"},
            # claude-fable-5-1: cache-read $0.25 confirmed from Anthropic's
            # current published table (checked 2026-09-02); cache write
            # follows the Fable-tier 1.25x-input convention used for
            # claude-fable-5 above.
            "claude-fable-5-1": {"input": "10", "output": "50", "cache_read": "0.25", "cache_write": "12.50"},
            # claude-opus-5-5: released 2026-09-22 with a price cut against
            # Opus 5 — input and output both -20% ($5→$4, $25→$20) and the
            # cache READ rate -60% ($0.50→$0.20), which is the one that
            # matters most here: long agentic runs are 90%+ cache reads.
            # All four rates read from platform.claude.com/docs/en/about-claude/pricing
            # on 2026-09-24 (agent-sourced; the page's own prose confirms the
            # $0.20 read as 0.05x input). No long-context tier: Anthropic
            # prices the full 1M window at standard rates from the
            # 4.6-generation onward, so this entry deliberately has no nested
            # "long" table.
            "claude-opus-5-5": {"input": "4", "output": "20", "cache_read": "0.20", "cache_write": "5.00"},
            # claude-opus-5-5[1m]: added 2026-09-24 by the nightly
            # price-coverage job. "[1m]" is Claude Code's context-window
            # selector appended to a full model name, not a separate model:
            # code.claude.com/docs/en/model-config ("append [1m] to a full
            # model name"; Claude Code "resolves claude-opus-4-8[1m] to Opus
            # 4.8"), read 2026-09-24. The ledger shows the same thing: 155
            # main-loop rows that requested this id came back from the
            # provider reporting claude-opus-5-5. Only background_review rows,
            # which carry no reported identity, reach pricing under the
            # suffixed id.
            #
            # Same rates as claude-opus-5-5 because Anthropic prices the 1M
            # window at standard rates for this model: the pricing table
            # (platform.claude.com/docs/en/about-claude/pricing.md, read
            # 2026-09-24) lists Opus 5.5 at $4 input / $5 5m cache write /
            # $0.20 cache hit / $20 output, and its Long context section says
            # "Claude 4.6 and later models ... include the full 1M token
            # context window at standard pricing." So there is no long table.
            #
            # This is an explicit per-model entry, NOT a rule that strips
            # "[1m]" from every id. The suffix matters for pricing on any
            # model that has a long-context premium, because only the 1M
            # variant can send a prompt past 200K. A new "<model>[1m]" id
            # should therefore show up as a gap and get its own page check.
            "claude-opus-5-5[1m]": {"input": "4", "output": "20", "cache_read": "0.20", "cache_write": "5.00"},
            "claude-opus-5": {"input": "5", "output": "25", "cache_read": "0.50", "cache_write": "6.25"},
            "claude-opus-4-8": {"input": "5", "output": "25", "cache_read": "0.50", "cache_write": "6.25"},
            "claude-opus-4-6": {"input": "5", "output": "25", "cache_read": "0.50", "cache_write": "6.25"},
            # Published rate as of 2026-09-02 is still 2/10/0.20/2.50 (the
            # earlier "rises on Sep 1" note did not materialize in the
            # published table). Re-verify against the live table before
            # changing these four numbers.
            "claude-sonnet-5": {"input": "2", "output": "10", "cache_read": "0.20", "cache_write": "2.50"},
            # Sonnet 5.5: Anthropic's own model-pricing table, read 2026-10-03:
            # https://platform.claude.com/docs/en/about-claude/pricing.md
            # USD/MTok: base input $2, 5m cache write $2.50, cache hit $0.20,
            # output $10. The same page's Long context pricing section says
            # Claude 4.6 and later have standard rates across the full 1M
            # window, so there is deliberately no nested "long" table.
            # These are API list-price equivalents, not a Claude Max bill.
            "claude-sonnet-5-5": {"input": "2", "output": "10", "cache_read": "0.20", "cache_write": "2.50"},
            "claude-sonnet-4-6": {"input": "3", "output": "15", "cache_read": "0.30", "cache_write": "3.75"},
            "claude-haiku-4-5": {"input": "1", "output": "5", "cache_read": "0.10", "cache_write": "1.25"},
            # The provider-reported dated ID is Haiku 4.5; the shorter ID
            # above is its Claude API alias, not another tariff. Anthropic's
            # model roster confirms both IDs and the 200k context window:
            # https://platform.claude.com/docs/en/about-claude/models/overview.md
            # Anthropic's own model-pricing table publishes all four rates
            # (5-minute cache write); both pages read 2026-09-28:
            # https://platform.claude.com/docs/en/about-claude/pricing.md
            # No nested long tier: the model's context window ends at 200k.
            "claude-haiku-4-5-20251001": {"input": "1", "output": "5", "cache_read": "0.10", "cache_write": "1.25"},
        },
        # Unlisted anthropic models price at the fable tier (conservative-high).
        "default": {"input": "10", "output": "50", "cache_read": "1.00", "cache_write": "12.50"},
    },
    # OpenAI, read from developers.openai.com/api/docs/models/gpt-6-astra and
    # /api/docs/pricing by the parent AGENT on 2026-09-06 (agent-sourced, not
    # keeper-confirmed).
    #
    # gpt-6-astra standard: 10 / 1.00 / 12.50 / 50 (in / cache read / cache
    # write / out per Mtok). Above a 272,000-token prompt the WHOLE request
    # bills on the long-context meter at 20 / 2.00 / 25 / 75 — that is what
    # the nested "long" table is, and _rates_with_context selects it per
    # request. Each model's threshold was verified independently.
    #
    # gpt-5.6-sol is on a promotional rate through at least 2026-11-21,
    # replacing the stale 5/30 entry seeded 2026-08-30: short
    # 4 / 0.40 / 5 / 20, long 8 / 0.80 / 10 / 30. Its own model page at
    # developers.openai.com/api/docs/models/gpt-5.6-sol independently
    # confirms the >272,000-token prompt threshold (agent-read 2026-09-06).
    # RE-VERIFY the promotional rates after 2026-11-21.
    #
    # There is deliberately NO provider default (removed 2026-09-06): a new
    # OpenAI model ID must surface as unpriced coverage, because pricing it
    # off a stale flagship rate is a fabricated number and reporting $0 would
    # read as an actual cost of zero.
    "openai": {
        "models": {
            # GPT-6.1 Sol: OpenAI's own model page and Standard rate card,
            # both fetched directly as markdown on 2026-10-04:
            # https://developers.openai.com/api/docs/models/gpt-6.1-sol.md
            # https://developers.openai.com/api/docs/pricing.md
            # USD/MTok (input / cache read / cache write / output):
            # short 2 / 0.10 / 2.50 / 10; long 4 / 0.20 / 5 / 15.
            # This model's OWN page confirms >272K prompt tokens reprices
            # the entire request, matching the global threshold above.
            # Do not borrow GPT-6 Sol's $0.20 short cache-read rate: this
            # model publishes $0.10. Standard tier only, no regional premium.
            "gpt-6.1-sol": {
                "input": "2", "output": "10", "cache_read": "0.10", "cache_write": "2.50",
                "long": {"input": "4", "output": "15", "cache_read": "0.20", "cache_write": "5"},
            },
            # GPT-6 Sol and Luna, released 2026-09-22 at roughly half their
            # GPT-5.6 equivalents. Rates read 2026-09-24 from each model's own
            # page (developers.openai.com/api/docs/models/<id>) AND the master
            # rate card (/api/docs/pricing); both surfaces agree.
            #
            # The long tier is uniform across the current OpenAI family: a
            # prompt over 272,000 input tokens reprices the ENTIRE request at
            # 2x input / 2x cached input / 2x cache write / 1.5x output. Each
            # threshold below was confirmed on that model's OWN page — none is
            # borrowed from a sibling, which matters because a wrong threshold
            # silently misprices in both directions.
            "gpt-6-sol": {
                "input": "2", "output": "10", "cache_read": "0.20", "cache_write": "2.50",
                "long": {"input": "4", "output": "15", "cache_read": "0.40", "cache_write": "5"},
            },
            "gpt-6-luna": {
                "input": "0.10", "output": "0.50", "cache_read": "0.01", "cache_write": "0.125",
                "long": {"input": "0.20", "output": "0.75", "cache_read": "0.02", "cache_write": "0.25"},
            },
            "gpt-6-astra": {
                "input": "10", "output": "50", "cache_read": "1.00", "cache_write": "12.50",
                "long": {"input": "20", "output": "75", "cache_read": "2.00", "cache_write": "25"},
            },
            "gpt-5.6-sol": {
                "input": "4", "output": "20", "cache_read": "0.40", "cache_write": "5",
                "long": {
                    "input": "8", "output": "30", "cache_read": "0.80", "cache_write": "10",
                },
            },
            # gpt-5.5 bills NO separate cache write — the rate card prints "-"
            # in that column for both short and long context, so 0 is a
            # statement of fact here rather than a missing lookup.
            "gpt-5.5": {
                "input": "5", "output": "30", "cache_read": "0.50", "cache_write": "0",
                "long": {"input": "10", "output": "45", "cache_read": "1.00", "cache_write": "0"},
            },
            # CORRECTED 2026-09-24: terra and luna previously carried
            # cache_write "0", which understated every cached-write token on
            # those models. Unlike gpt-5.5 they DO bill cache writes — both
            # model pages state the 1.25x-uncached-input rule and the rate
            # card prints explicit dollar figures ($2.50 and $0.25). Their
            # long tiers were also absent and are added here.
            "gpt-5.6-terra": {
                "input": "2", "output": "12", "cache_read": "0.20", "cache_write": "2.50",
                "long": {"input": "4", "output": "18", "cache_read": "0.40", "cache_write": "5"},
            },
            "gpt-5.6-luna": {
                "input": "0.20", "output": "1.20", "cache_read": "0.02", "cache_write": "0.25",
                "long": {"input": "0.40", "output": "1.80", "cache_read": "0.04", "cache_write": "0.50"},
            },
        },
        "default": None,
    },
    # The Codex meter carries the same published rates for the models the
    # Codex lane actually runs. Subscription-included traffic still records
    # $0 ACTUAL; this table only estimates what it would have cost.
    #
    # This block DUPLICATES openai's rates rather than aliasing to it, and the
    # duplication is deliberate: a public-API tariff is not proof of the same
    # cliff in the Codex subscription/credit channel, so the two tables must
    # stay independently editable if they ever diverge. Keep them in sync by
    # hand until a divergence is actually observed.
    "openai-codex": {
        "models": {
            # GPT-6.1 Sol API list-price counterfactual for Codex usage,
            # NOT an actual subscription charge or Codex-quota multiplier.
            # Sources read directly on 2026-10-04:
            # https://developers.openai.com/api/docs/models/gpt-6.1-sol.md
            # https://developers.openai.com/api/docs/pricing.md
            # All four short/long rates and this model's >272K boundary are
            # verified there. Kept as a separate entry, not a provider alias,
            # so the Codex and API tables remain independently editable.
            "gpt-6.1-sol": {
                "input": "2", "output": "10", "cache_read": "0.10", "cache_write": "2.50",
                "long": {"input": "4", "output": "15", "cache_read": "0.20", "cache_write": "5"},
            },
            # Added 2026-09-24. gpt-5.6-terra was the single largest coverage
            # gap in the ledger — 1.007 BILLION tokens over 14 days priced as
            # nothing, because the model existed in the "openai" block but had
            # never been added here, and this provider correctly has no
            # default to fall back on. Same published rates and 272K long-tier
            # threshold as the openai entries above, read 2026-09-24.
            "gpt-6-sol": {
                "input": "2", "output": "10", "cache_read": "0.20", "cache_write": "2.50",
                "long": {"input": "4", "output": "15", "cache_read": "0.40", "cache_write": "5"},
            },
            "gpt-6-luna": {
                "input": "0.10", "output": "0.50", "cache_read": "0.01", "cache_write": "0.125",
                "long": {"input": "0.20", "output": "0.75", "cache_read": "0.02", "cache_write": "0.25"},
            },
            "gpt-5.6-terra": {
                "input": "2", "output": "12", "cache_read": "0.20", "cache_write": "2.50",
                "long": {"input": "4", "output": "18", "cache_read": "0.40", "cache_write": "5"},
            },
            "gpt-5.6-luna": {
                "input": "0.20", "output": "1.20", "cache_read": "0.02", "cache_write": "0.25",
                "long": {"input": "0.40", "output": "1.80", "cache_read": "0.04", "cache_write": "0.50"},
            },
            "gpt-6-astra": {
                "input": "10", "output": "50", "cache_read": "1.00", "cache_write": "12.50",
                "long": {"input": "20", "output": "75", "cache_read": "2.00", "cache_write": "25"},
            },
            "gpt-5.6-sol": {
                "input": "4", "output": "20", "cache_read": "0.40", "cache_write": "5",
                "long": {
                    "input": "8", "output": "30", "cache_read": "0.80", "cache_write": "10",
                },
            },
            "gpt-5.5": {
                "input": "5", "output": "30", "cache_read": "0.50", "cache_write": "0",
                "long": {"input": "10", "output": "45", "cache_read": "1.00", "cache_write": "0"},
            },
        },
        "default": None,
    },
    # OpenRouter, added 2026-09-24. Unlike every other provider in this table
    # these are REAL METERED ROUTES — not subscription-included traffic whose
    # $0 actual cost this table counterfactually reprices. An OpenRouter
    # estimate is an actual-bill estimate, so its accuracy matters more.
    #
    # Rates converted from the public catalog (openrouter.ai/api/v1/models,
    # read 2026-09-24), which publishes per-TOKEN decimals; these are x1e6.
    #
    # CAVEAT, structural and unavoidable: OpenRouter routes across multiple
    # upstream endpoints per model at DIFFERENT prices, and the ledger does
    # not record which endpoint served a request. The figures below are the
    # catalog's canonical/default price. Observed spread at read time:
    # minimax-m3 up to 2.5x canonical (13 endpoints), gpt-6-astra 2x (a $5/$25
    # endpoint and a $20/$100 one both exist), gemini-3.8-flash up to 1.8x,
    # gpt-6-sol ~10%. Treat an OpenRouter estimate as a central figure with
    # real dispersion, not a precise bill.
    #
    # No provider default, for the same reason openai has none: a new
    # OpenRouter id (there are 459) must surface as coverage rather than
    # borrow an unrelated model's rate.
    "openrouter": {
        "models": {
            # OpenRouter's OWN public catalog for this exact route, read
            # 2026-09-29: https://openrouter.ai/api/v1/models and
            # https://openrouter.ai/api/v1/models/anthropic/claude-opus-5.5/endpoints
            # Per-token prompt/completion/cache-read/5m-cache-write prices
            # (0.000004/0.00002/0.0000002/0.000005) converted to USD/MTok.
            # The 1M-context endpoints list no context-tier overrides;
            # Anthropic's model-specific pricing page confirms Opus 5.5 has
            # standard rates through 1M (read 2026-09-29):
            # https://platform.claude.com/docs/en/about-claude/pricing.md
            # Endpoint variance remains: standard routing lists $4/$20,
            # while some endpoints are $4.40/$22 or $8/$40 (Fast).
            "anthropic/claude-opus-5.5": {
                "input": "4", "output": "20", "cache_read": "0.20", "cache_write": "5",
            },
            # GPT-6.1 Sol on this exact OpenRouter route, read 2026-10-07:
            # https://openrouter.ai/api/v1/models
            # https://openrouter.ai/api/v1/models/openai/gpt-6.1-sol/endpoints
            # https://openrouter.ai/openai/gpt-6.1-sol
            # Catalog per-token prompt/completion/cache-read/cache-write:
            # 0.000002 / 0.00001 / 0.0000001 / 0.0000025, converted to USD/MTok.
            # Its own override starts at min_prompt_tokens 272000 and lists
            # 0.000004 / 0.000015 / 0.0000002 / 0.000005. The exact model's
            # first-party page independently confirms the >272K full-request
            # boundary (raw markdown read 2026-10-07):
            # https://developers.openai.com/api/docs/models/gpt-6.1-sol.md
            # Standard routing only: the OpenRouter page lists Flex at half,
            # Fast at twice and regional endpoints at 1.1x these rates. The
            # ledger has no endpoint/tier identity, so this remains an estimate,
            # not an exact bill. Source OpenRouter, not a borrowed OpenAI entry.
            "openai/gpt-6.1-sol": {
                "input": "2", "output": "10", "cache_read": "0.10", "cache_write": "2.50",
                "long": {"input": "4", "output": "15", "cache_read": "0.20", "cache_write": "5"},
            },
            # The 272K long-context override is published in the catalog for
            # both GPT-6 routes and matches OpenAI's own documented threshold
            # and multipliers — independent cross-confirmation.
            "openai/gpt-6-sol": {
                "input": "2", "output": "10", "cache_read": "0.20", "cache_write": "2.50",
                "long": {"input": "4", "output": "15", "cache_read": "0.40", "cache_write": "5"},
            },
            "openai/gpt-6-astra": {
                "input": "10", "output": "50", "cache_read": "1.00", "cache_write": "12.50",
                "long": {"input": "20", "output": "75", "cache_read": "2.00", "cache_write": "25"},
            },
            # GLM-5.3 on this exact OpenRouter route, read 2026-10-21:
            # https://openrouter.ai/api/v1/models
            # https://openrouter.ai/api/v1/models/z-ai/glm-5.3/endpoints
            # Catalog model-level per-token prompt/completion/cache-read:
            # 0.000000079 / 0.0000014 / 0.000000078, converted to USD/MTok.
            # First observed 2026-10-21 (two ACP sessions, 31 calls).
            #
            # DISPERSION IS EXTREME ON THIS ROUTE, worse than the caveat above.
            # The catalog figure is the Wafer (wafer/us, fp4) endpoint's row:
            # the lowest input and output price among the 48 endpoints listed
            # at read time. Most endpoints, including Z.AI's own (z-ai/fp8),
            # list 1.40 / 4.40 / 0.26, which equals the first-party ``zai``
            # entry in this table; the priority-tier alibaba/prime endpoint
            # lists 2.80 / 8.80 / 0.56. Every endpoint other than Wafer's
            # bills more for input and output, so this entry sits at the
            # floor of the range, not its centre. The served endpoint is not
            # recorded (no provider_request_id on these rows, and Hermes sets
            # no OpenRouter provider-routing preferences), so it cannot be
            # chosen per request. Same figure Hermes' own estimate used.
            #
            # cache_write: the ``input_cache_write`` key is ABSENT at model
            # level and on every endpoint but Amazon Bedrock's. Recorded as 0
            # (written-down possible understatement, not a verified zero).
            # No long-context tier: no endpoint publishes a context override,
            # so no nested "long" table.
            "z-ai/glm-5.3": {"input": "0.079", "output": "1.40", "cache_read": "0.078", "cache_write": "0"},
            # cache_write ABSENT from the catalog for the four models below —
            # the ``input_cache_write`` key is simply not present on the model
            # or any endpoint. Recorded as 0, which is a KNOWN UNDERSTATEMENT
            # of cached-write tokens rather than a verified zero. Not inferred
            # from a 1.25x convention: that would be a fabricated rate, and an
            # understatement that is written down beats a guess that is not.
            #
            # grok-4.7 publishes a long tier at 200,000 tokens, NOT the 272,000
            # this module's global LONG_CONTEXT_PROMPT_TOKENS encodes. It is
            # therefore given NO nested "long" table — attaching one would fire
            # it at the wrong boundary and misprice in both directions — and is
            # listed in LONG_CONTEXT_UNVERIFIED so the resulting understatement
            # is reported. The proper fix is a per-model threshold; that is a
            # schema change, deliberately not smuggled in here.
            "x-ai/grok-4.7": {"input": "1.60", "output": "4.80", "cache_read": "0.40", "cache_write": "0"},
            "minimax/minimax-m3": {"input": "0.30", "output": "1.20", "cache_read": "0.06", "cache_write": "0"},
            "xiaomi/mimo-v2.6-pro": {"input": "0.435", "output": "0.87", "cache_read": "0.0036", "cache_write": "0"},
            "meta/muse-spark-1.3": {"input": "1.25", "output": "4.25", "cache_read": "0.15", "cache_write": "0"},
            "google/gemini-3.8-flash": {"input": "0.75", "output": "3.75", "cache_read": "0.075", "cache_write": "0.0417"},
            "qwen/qwen3.8-max-0902": {"input": "2", "output": "6", "cache_read": "0.25", "cache_write": "2.50"},
        },
        "default": None,
    },
    # Z.ai. All rates read 2026-09-24 from Z.ai's OWN pricing page,
    # https://docs.z.ai/guides/overview/pricing ("All prices are in USD"),
    # fetched directly from its markdown endpoint rather than through an
    # aggregator — an aggregator's resale price is not the provider's list
    # price. The pre-existing GLM-5.3 figures were confirmed unchanged.
    #
    # "Cached Input Storage" is published as "Limited-time Free", so
    # cache_write 0 here is Z.ai's stated current position rather than a
    # missing lookup. It is TIME-LIMITED by Z.ai's own wording — recheck.
    #
    # PROVIDER DEFAULT REMOVED 2026-09-24. It existed solely as a
    # conservative-high stand-in for glm-5.3-flash — which turns out to be
    # published, at $0.15/$0.50/$0.03. The stand-in was overstating flash
    # traffic by ~8.9x (112.7M tokens in the trailing 14 days). With flash and
    # 5.2 both carrying real entries, a default could now only serve to
    # silently price some FUTURE Z.ai model, which is the exact failure this
    # table exists to prevent. New ids surface as coverage and wake the
    # nightly price-coverage job instead.
    "zai": {
        "models": {
            "glm-5.3": {"input": "1.40", "output": "4.40", "cache_read": "0.26", "cache_write": "0"},
            "glm-5.3-flash": {"input": "0.15", "output": "0.50", "cache_read": "0.03", "cache_write": "0"},
            # 653M tokens of historical traffic, and still published by Z.ai
            # at the same rates as 5.3.
            "glm-5.2": {"input": "1.40", "output": "4.40", "cache_read": "0.26", "cache_write": "0"},
        },
        "default": None,
    },
    # OpenCode Go: a $10/month subscription with NO per-token bill, where
    # included usage is metered as a monthly DOLLAR allowance per model
    # (GLM-5.3 $15/mo; the 5-hour window is 20% of it, the weekly 50%).
    #
    # CORRECTED 2026-09-24. The previous comment called these "the underlying
    # model's list rates", i.e. borrowed from Z.ai. They are not borrowed:
    # OpenCode publishes its OWN per-token table for the Go plan at
    # https://opencode.ai/docs/go ("Token prices are per 1M tokens"), verified
    # against the docs source in the sst/opencode repo on 2026-09-24. The
    # figures happen to equal Z.ai's GLM list rates, but the source of record
    # for an opencode-go label is now OpenCode itself.
    #
    # cache_write is printed as "-" on that table, i.e. NOT published.
    # Recorded as 0, a written-down understatement rather than a verified
    # zero. No provider default: an unlisted Go model surfaces as coverage.
    "opencode-go": {
        "models": {
            "glm-5.3": {"input": "1.40", "output": "4.40", "cache_read": "0.26", "cache_write": "0"},
            # 18.8M tokens of historical traffic that was previously unpriced.
            "glm-5.2": {"input": "1.40", "output": "4.40", "cache_read": "0.26", "cache_write": "0"},
        },
        "default": None,
    },
    # Moonshot / Kimi. The 'kimi-coding' label is Kimi Code, a MEMBERSHIP
    # product (Plus $19/mo through Ultra $199/mo) that publishes quota
    # allowances only and no per-token rate card — so exactly as with the
    # Codex and Claude Max lanes, Moonshot's pay-as-you-go API card is the
    # correct counterfactual for what this traffic would otherwise cost.
    #
    # Rates read 2026-09-24 from https://platform.kimi.ai/docs/pricing/chat.
    # The pre-existing $3 / $15 / $0.30 figures were confirmed correct.
    #
    # cache_write ADDED 2026-09-24 at $3.00. Moonshot bills a one-time
    # per-token cache WRITE, tiered by TTL: $3.00 at the default 5-minute TTL
    # and $6.00 at one hour. This table has no TTL dimension, so it records
    # the default tier, and 1-hour-TTL traffic is consequently understated 2x
    # on writes. Cache hits within the TTL bill only the $0.30 cached-input
    # rate and renew the entry; there is no ongoing storage rent.
    #
    # PROVENANCE NOTE, because it nearly cost us the number: this page's own
    # .md export and a plain fetch both return a 4-column table with the two
    # Cache Write columns MISSING. The full 7-column table is only visible in
    # a rendered browser session, which is where these figures came from,
    # cross-checked against the RMB card on platform.kimi.com (¥20 at 5min,
    # ¥40 at 1h). Do NOT "correct" this back to 0 from the .md export.
    #
    # No effect on any current estimate: the ledger records ZERO cache-write
    # tokens on this provider to date. It is recorded so that when that
    # changes, the change is priced.
    #
    # PROVIDER DEFAULT REMOVED 2026-09-24: both served ids are explicit now,
    # and the published siblings (k2.7-code $0.95/$4.00, k2.6 $0.95/$4.00) are
    # CHEAPER than K3 — so a K3-rate default would overstate them, and worse,
    # would silently price any foreign model arriving under this label.
    "kimi-coding": {
        "models": {
            "k3": {"input": "3", "output": "15", "cache_read": "0.30", "cache_write": "3.00"},
            # k3-256k: added 2026-10-06. Kimi's first-party model matrix
            # https://www.kimi.com/code/docs/en/kimi-code/models.html
            # (read 2026-10-06) explicitly identifies BOTH k3 and k3-256k as
            # model version K3; this ID caps context at 262,144 tokens. It is
            # not a sibling-model rate inference or a generic suffix strip.
            # Kimi Code has no per-token bill; use K3's published API card as
            # the counterfactual, not the matrix's subscription-quota ratio
            # (k3 uses about twice the quota of k3-256k).
            # All four rates re-read 2026-10-06 from the raw vendor export:
            # https://platform.kimi.ai/docs/pricing/chat.md
            # Its JSX DocTable supplies $3 input / $15 output / $0.30 cache
            # hit / $3 cache write at default 5min TTL ($6 at 1h, not modeled).
            # The text extractor drops these cells; raw curl retains them.
            # No long tier: the model's own first-party guide explicitly
            # states flat pricing with no context-length tiering:
            # https://platform.kimi.ai/docs/guide/kimi-k3-quickstart.md
            # (read 2026-10-06, "How is Kimi K3 billed?").
            "k3-256k": {"input": "3", "output": "15", "cache_read": "0.30", "cache_write": "3.00"},
            "kimi-k3": {"input": "3", "output": "15", "cache_read": "0.30", "cache_write": "3.00"},
        },
        "default": None,
    },
    # DeepSeek FIRST-PARTY API card, read 2026-09-25:
    # https://api-docs.deepseek.com/quick_start/pricing
    # Off-peak per-Mtok (cache miss / hit / output): Flash
    # $0.15 / $0.003 / $0.60; V4 Pro $0.66 / $0.022 / $1.98.
    # Peak: Flash $0.30 / $0.006 / $1.20; Pro $1.32 / $0.044 / $3.96.
    # Peak is 01-04 and 06-10 UTC weekdays excluding Chinese holidays. Our
    # buckets lack a time-tier dimension: these are the published OFF-PEAK
    # FLOOR, not a blended or actual-bill rate; peak usage can cost 2x.
    # No cache-write tariff is published: zero is a written-down possible
    # understatement, not a statement that writes are free. Both models
    # list a 1M context with no published pricing cliff; no "long" tables.
    #
    # The requested legacy deepseek-v4-flash now SERVES V4.1-Flash at the
    # Flash tariff, so its current list price is the SAME as deepseek-flash.
    # The sidecar's two new rows request that legacy id but report
    # deepseek-flash; the detector uses the provider-reported id. Release:
    # https://api-docs.deepseek.com/news/news260910 (read 2026-09-25).
    # Old seeded rates for the two legacy ids contained INFERRED cache-hit
    # coefficients and have been replaced with the published current card.
    #
    # PROVIDER DEFAULT REMOVED 2026-09-24: the label also carried foreign GPT
    # and Gemini traffic. No rate-bearing fallback may silently price them.
    "deepseek": {
        "models": {
            "deepseek-v4-pro": {"input": "0.66", "output": "1.98", "cache_read": "0.022", "cache_write": "0"},
            "deepseek-v4-flash": {"input": "0.15", "output": "0.60", "cache_read": "0.003", "cache_write": "0"},
            "deepseek-flash": {"input": "0.15", "output": "0.60", "cache_read": "0.003", "cache_write": "0"},
        },
        "default": None,
    },
    # Local inference is $0 by definition, and saying so beats "unpriced".
    "custom:gemma-4-moe-it": {
        "models": {},
        "default": {"input": "0", "output": "0", "cache_read": "0", "cache_write": "0"},
    },
    # The other local endpoints, added 2026-09-24 from a coverage audit that
    # found them reporting as unpriced gaps. Same rationale as the entry
    # above: these run on Luis's own hardware (Acubens oMLX and the llama.cpp
    # lane), so the marginal token cost is zero and a $0 default is a
    # STATEMENT rather than a guess. A $0 provider default is safe in a way a
    # priced one is not — a new local model needs no rate lookup, because
    # every model served from local hardware is $0.
    #
    # Note this is list-price coverage only. Electricity and the amortised
    # hardware are real costs; they are simply not per-token costs and have
    # never been in this table's scope.
    "custom:omlx": {
        "models": {},
        "default": {"input": "0", "output": "0", "cache_read": "0", "cache_write": "0"},
    },
    "custom:minicpm5": {
        "models": {},
        "default": {"input": "0", "output": "0", "cache_read": "0", "cache_write": "0"},
    },
}


def _rates_with_context(
    provider: str, model: str, context_basis: str = "standard"
) -> Tuple[Optional[Dict[str, Any]], str]:
    """Resolve one rate table plus the context basis actually applied.

    A model without a nested ``long`` table has no long-context meter, so a
    long request prices at its single published rate and the applied basis
    reads ``"standard"`` — the caller must not report long-context billing
    that did not happen.
    """
    entry = LIST_PRICES.get(resolve_rate_provider(provider, model))
    if entry is None:
        return None, "standard"
    rates = entry["models"].get(model)
    if rates is None:
        rates = entry.get("default")
    if rates is None:
        return None, "standard"
    if context_basis == "long" and rates.get("long"):
        return rates["long"], "long"
    return rates, "standard"


def _rates_for(provider: str, model: str) -> Optional[Dict[str, Any]]:
    """Keep the existing rate-only contract used by quota derivation."""
    rates, _basis = _rates_with_context(provider, model)
    return rates


def estimate_list_price(buckets: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    """Estimate list-price USD over (provider, model, context basis) aggregates.

    Each bucket may carry ``context_basis`` — ``"standard"`` (the default),
    ``"long"`` for traffic whose per-request prompt exceeded
    :data:`LONG_CONTEXT_PROMPT_TOKENS`, or ``"aggregate"`` for rows that
    bundle many calls and therefore cannot be classified. Callers build those
    buckets per request with :func:`request_context_basis`; grouping by model
    first and testing the aggregate would misprice both directions.

    Returns exact-decimal strings; unpriced tokens are surfaced as coverage,
    never silently priced, and the estimate names the service tier it assumed.
    """
    total = Decimal(0)
    priced_tokens = 0
    unpriced_tokens = 0
    long_context_tokens = 0
    aggregate_context_tokens = 0
    long_context_unverified_tokens = 0
    unpriced_providers: set[str] = set()
    for bucket in buckets:
        provider = str(bucket.get("provider") or "unknown")
        model = str(bucket.get("model") or "")
        context_basis = str(bucket.get("context_basis") or "standard")
        tokens = int(bucket.get("total_tokens") or 0)
        rates, applied_basis = _rates_with_context(provider, model, context_basis)
        if rates is None:
            unpriced_tokens += tokens
            unpriced_providers.add(provider)
            continue
        priced_tokens += tokens
        if (provider, model) in LONG_CONTEXT_UNVERIFIED:
            long_context_unverified_tokens += tokens
        if applied_basis == "long":
            long_context_tokens += tokens
        elif context_basis == "aggregate":
            aggregate_context_tokens += tokens
        total += (
            Decimal(int(bucket.get("input_tokens") or 0)) * Decimal(rates["input"])
            # output_tokens already includes reasoning — do not add
            # reasoning_tokens on top (it is diagnostic only).
            + Decimal(int(bucket.get("output_tokens") or 0)) * Decimal(rates["output"])
            + Decimal(int(bucket.get("cache_read_tokens") or 0)) * Decimal(rates["cache_read"])
            + Decimal(int(bucket.get("cache_write_tokens") or 0)) * Decimal(rates["cache_write"])
        ) / _MTOK
    denominator = priced_tokens + unpriced_tokens
    return {
        "estimate_usd": str(total.quantize(Decimal("0.01"))),
        "priced_token_share": (priced_tokens / denominator) if denominator else 1.0,
        "unpriced_providers": sorted(unpriced_providers),
        # Basis disclosure, so no consumer can read the estimate as actual.
        "rate_basis": RATE_BASIS,
        "long_context_tokens": long_context_tokens,
        "aggregate_context_tokens": aggregate_context_tokens,
        # Tokens on a model that publishes a long-context meter we cannot
        # apply for lack of a verified threshold: priced at the short rate,
        # so the estimate may understate them. Stated, not hidden.
        "long_context_unverified_tokens": long_context_unverified_tokens,
    }


# ── Token bucketing for the estimate. These two helpers moved here from
# dashboard.py on 2026-09-10 (rulings-per-dollar lane) so every consumer —
# the dashboard's window/daily estimates, and now the weekly rulings join —
# folds rows into price buckets exactly one way, against exactly one rate
# table. The dashboard keeps its private aliases; nothing about the grouping
# changed in the move.
LIST_PRICE_TOKEN_FIELDS = (
    "input_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "output_tokens",
    "reasoning_tokens",
)


def accumulate_price_bucket(
    buckets: Dict[Tuple[str, str, str], Dict[str, Any]], row: Mapping[str, Any]
) -> None:
    """Fold one canonical row into its (provider, model, context basis) bucket.

    The long-context meter is a PER-REQUEST test (see
    :func:`request_context_basis`): canonical ``input_tokens`` excludes cache,
    so the threshold is input + cache_read + cache_write on a single request.
    Classifying here — while the individual rows are still in hand — is why
    the basis is part of the grouping key rather than something re-derived
    from a model's aggregate, which would be wrong in both directions (many
    short requests summing past the threshold, or one long request diluted by
    short ones).

    Model identity resolves reported -> requested -> ``model`` -> unknown.
    That last fallback was ADDED 2026-09-24 and is not cosmetic: the canonical
    ledger names the requested id ``model_requested``, but the Hermes usage
    sidecar names it ``model``, and there is no ``model_requested`` column
    there at all. Every sidecar row whose provider did not echo an identity
    was therefore bucketing as ``unknown`` and dropping out of pricing while
    holding a perfectly good model id — ~950M tokens in a 14-day window.
    Reported identity still wins where present; this only rescues rows that
    would otherwise have been discarded.
    """
    provider = str(row.get("provider") or "unknown")
    model = str(
        row.get("model_reported")
        or row.get("model_requested")
        or row.get("model")
        or "unknown"
    )
    # A relayed row whose own label has no rate table (a bare ``custom``
    # Hermes row through Meridian) prices as the provider it reached, and only
    # when that provider lists this exact model (the same guard as
    # resolve_rate_provider). Added 2026-09-25: 396 such Claude rows (~90M
    # tokens) were sitting in unpriced coverage under the label "custom".
    upstream = row.get("upstream_provider")
    if upstream not in (None, "") and provider not in LIST_PRICES:
        entry = LIST_PRICES.get(str(upstream))
        if entry and entry["models"].get(model) is not None:
            provider = str(upstream)
    key = (provider, model, request_context_basis(row))
    bucket = buckets.setdefault(
        key,
        {
            "provider": key[0],
            "model": key[1],
            "context_basis": key[2],
            **{field: 0 for field in LIST_PRICE_TOKEN_FIELDS},
        },
    )
    for field in LIST_PRICE_TOKEN_FIELDS:
        bucket[field] += int(row.get(field) or 0)


def finalize_price_buckets(
    buckets: Dict[Tuple[str, str, str], Dict[str, Any]]
) -> list[Dict[str, Any]]:
    for bucket in buckets.values():
        # Reasoning is diagnostic and already inside output — never a second
        # time in the accounted total.
        bucket["total_tokens"] = sum(
            bucket[field]
            for field in LIST_PRICE_TOKEN_FIELDS
            if field != "reasoning_tokens"
        )
    return list(buckets.values())
