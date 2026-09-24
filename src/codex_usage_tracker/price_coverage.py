"""Price-coverage detection over observed traffic.

Companion to :mod:`list_pricing`. That module answers "what does this
(provider, model) cost"; this one answers the question nobody was asking
until a model shipped mid-week: **which pairs are actually flowing that the
table cannot price, and which are being priced by a provider default rather
than a verified entry.**

Three classes, and the difference between them is the whole point:

``exact``
    A ``models[]`` entry exists. The rate was looked up and carries
    provenance in the table's comments. Trustworthy.
``default``
    No model entry; a provider ``default`` applied. The number is SILENTLY
    PRODUCED and may be wrong by any margin. ``list_pricing`` keeps two such
    defaults deliberately (zai, for glm-5.3-flash; the local $0 provider) and
    they are acknowledged below. Any OTHER default hit is a latent defect —
    it is the failure mode ``openai``'s ``"default": None`` was introduced to
    prevent on 2026-09-06.
``unpriced``
    No entry and no default. Surfaces as coverage. This is the HONEST
    failure: the estimate under-reports and says so, which is why
    ``list_pricing`` prefers it to a fabricated rate.

Detection is deliberately deterministic — no model call — so the nightly
watch costs nothing when coverage has not moved. See ``monitor_lines``.

Keeper direction 2026-09-24 (#gateway, msg 1552532146261073921): "we should
set up a standing job that checks if there are gaps and fills them. Runs
nightly." This module is the *checks* half. The *fills* half is an agent
step that must read vendor pages and may never guess — a fabricated rate
corrupts the cost ledger silently, which is strictly worse than the
acknowledged gap it would be replacing.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

from .hermes_home import resolve_hermes_home
from .list_pricing import LIST_PRICES, resolve_rate_provider

# (provider, model) pairs deliberately left unpriced, with the reason. These
# are excluded from the monitor's wake set: without this the nightly job
# would wake every night about the same permanently-unpriceable rows.
#
# Acknowledging is a governed act, not a convenience — each entry states why
# a rate would be WRONG, not merely unknown.
ACKNOWLEDGED_UNPRICED: Dict[Tuple[str, str], str] = {
    ("moa", "council"): (
        "preset name, not a model id. The MoA virtual provider's real "
        "inference lands on its reference/aggregator models under their own "
        "provider rows; pricing the preset would invent a second charge."
    ),
    ("moa", "glm-5.3"): (
        "MoA fan-out double-count risk: the aggregator call may already be "
        "recorded under its real provider (zai/opencode-go). Pricing this "
        "row before the overlap is resolved would overstate. Tracked with "
        "the contract-v2 physical-request identity work, not here."
    ),
}

# A provider label of ``unknown`` means the writer had no route evidence.
# Contract rule: unattributed beats misattributed. Never price these.
UNATTRIBUTED_PROVIDERS = frozenset({"unknown", ""})

# Provider defaults that exist on purpose. A ``default`` classification on
# any provider NOT in this set is reported as a defect rather than accepted.
#
# NARROWED 2026-09-24 to a single rule: **a provider default is sanctioned
# only when it is $0.** The previous set also sanctioned zai, kimi-coding and
# deepseek on the reasoning that an unlisted sibling would price at a
# conservative-high family rate. That reasoning does not survive contact with
# the ledger: a default cannot tell an unlisted sibling from a foreign model,
# and the 'deepseek' label turned out to carry ~119M tokens of gpt-5.5 plus
# Gemini and local-mlx traffic, all of which priced silently at DeepSeek
# rates. Those three defaults have been removed from the rate table itself;
# any remaining default hit on a priced provider is now reported as a defect.
SANCTIONED_DEFAULT_PROVIDERS = frozenset({
    # Local inference is $0 by definition, so a $0 default can never be a
    # silent misprice: there is no rate to get wrong, and a new local model
    # needs no lookup.
    "custom:gemma-4-moe-it",
    "custom:omlx",
    "custom:minicpm5",
})

DEFAULT_WINDOW_DAYS = 14


def classify(provider: str, model: str) -> str:
    """Return ``exact``, ``default`` or ``unpriced`` for one pair.

    Mirrors :func:`list_pricing._rates_with_context` resolution order so the
    report cannot drift from what the estimator actually does — including the
    proxy-provider aliasing, which is why that lives in ``list_pricing`` and
    is imported here rather than duplicated.
    """
    entry = LIST_PRICES.get(resolve_rate_provider(provider, model))
    if entry is None:
        return "unpriced"
    if entry["models"].get(model) is not None:
        return "exact"
    return "default" if entry.get("default") is not None else "unpriced"


def default_ledger_path(environ: Optional[Mapping[str, str]] = None) -> Path:
    """The Hermes usage-event sidecar, resolved the same way the tracker does."""
    return resolve_hermes_home(environ) / "usage_events.db"


def observed_pairs(
    db_path: Path, window_days: int = DEFAULT_WINDOW_DAYS
) -> List[Dict[str, Any]]:
    """Every (provider, model) seen in the window, with call and token volume.

    Read-only. An absent sidecar is zero rows, never an error — the ledger
    contract makes bootstrap lazy, so "no file" and "no traffic" are the same
    observation.

    ``model_reported`` is the provider-echoed identity and is preferred;
    rows that only ever carried a requested id fall back to ``model`` rather
    than being dropped, because dropping them would understate coverage gaps.
    """
    if not Path(db_path).exists():
        return []
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        cols = {row[1] for row in con.execute("PRAGMA table_info(usage_events)")}
        if not {"provider", "model"} <= cols:
            return []
        model_expr = (
            "COALESCE(NULLIF(model_reported, ''), model)"
            if "model_reported" in cols
            else "model"
        )
        token_expr = " + ".join(
            f"COALESCE({field}, 0)"
            for field in (
                "input_tokens", "output_tokens",
                "cache_read_tokens", "cache_write_tokens",
            )
            if field in cols
        ) or "0"
        # timestamp is epoch REAL in this sidecar (accounting time).
        rows = con.execute(
            f"SELECT provider, {model_expr} AS m, COUNT(*), SUM({token_expr}) "
            "FROM usage_events "
            "WHERE timestamp >= strftime('%s','now') - ? * 86400 "
            "GROUP BY provider, m",
            (window_days,),
        ).fetchall()
    finally:
        con.close()
    return [
        {
            "provider": provider or "unknown",
            "model": model or "unknown",
            "calls": int(calls or 0),
            "tokens": int(tokens or 0),
        }
        for provider, model, calls, tokens in rows
    ]


def coverage_report(
    pairs: Iterable[Mapping[str, Any]]
) -> Dict[str, Any]:
    """Classify observed pairs and separate actionable gaps from acknowledged ones.

    ``actionable`` is what the nightly job exists to close: a pair that is
    unpriced (or defaulted on an unsanctioned provider), is attributable, and
    has not been deliberately acknowledged.
    """
    rows: List[Dict[str, Any]] = []
    totals = {"exact": 0, "default": 0, "unpriced": 0}
    actionable: List[Dict[str, Any]] = []
    acknowledged: List[Dict[str, Any]] = []
    for pair in pairs:
        provider = str(pair.get("provider") or "unknown")
        model = str(pair.get("model") or "unknown")
        tokens = int(pair.get("tokens") or 0)
        verdict = classify(provider, model)
        row = {
            "provider": provider,
            "model": model,
            "calls": int(pair.get("calls") or 0),
            "tokens": tokens,
            "classification": verdict,
            "rate_provider": resolve_rate_provider(provider, model),
        }
        totals[verdict] += tokens
        # A default hit on an unsanctioned provider is a silent-pricing
        # defect, so it is actionable even though it is technically "priced".
        unsanctioned_default = (
            verdict == "default"
            and resolve_rate_provider(provider, model)
            not in SANCTIONED_DEFAULT_PROVIDERS
        )
        if provider in UNATTRIBUTED_PROVIDERS:
            row["acknowledged_reason"] = (
                "unattributed provider: the writer had no route evidence. "
                "Contract rule is unattributed beats misattributed."
            )
            acknowledged.append(row)
        elif (provider, model) in ACKNOWLEDGED_UNPRICED:
            row["acknowledged_reason"] = ACKNOWLEDGED_UNPRICED[(provider, model)]
            acknowledged.append(row)
        elif verdict == "unpriced" or unsanctioned_default:
            row["defect"] = (
                "silently priced by an unsanctioned provider default"
                if unsanctioned_default
                else "no rate entry and no provider default"
            )
            actionable.append(row)
        rows.append(row)
    grand = sum(totals.values())
    actionable.sort(key=lambda r: r["tokens"], reverse=True)
    acknowledged.sort(key=lambda r: r["tokens"], reverse=True)
    rows.sort(key=lambda r: r["tokens"], reverse=True)
    return {
        "rows": rows,
        "actionable": actionable,
        "acknowledged": acknowledged,
        "token_totals": totals,
        "priced_token_share": (totals["exact"] / grand) if grand else 1.0,
        "actionable_tokens": sum(r["tokens"] for r in actionable),
        "observed_pairs": len(rows),
    }


def monitor_lines(report: Mapping[str, Any]) -> str:
    """Stable wake signal for a cron ``monitor_script``.

    DELIBERATELY EXCLUDES volumes, dates and totals. The scheduler hashes
    this output and suppresses the agent run when it is unchanged, so any
    field that moves nightly — token counts above all — would wake the agent
    every single night and defeat the whole design. Only the *set* of
    actionable gaps appears here.
    """
    if not report["actionable"]:
        return "price-coverage: no actionable gaps\n"
    lines = ["price-coverage: actionable gaps"]
    lines.extend(
        f"  {row['classification']}\t{row['provider']}\t{row['model']}"
        for row in sorted(
            report["actionable"], key=lambda r: (r["provider"], r["model"])
        )
    )
    return "\n".join(lines) + "\n"


def render_report(report: Mapping[str, Any], window_days: int) -> str:
    """Human/agent-readable report, volumes included."""
    out = [
        f"Price coverage over the last {window_days} days "
        f"({report['observed_pairs']} provider/model pairs observed)",
        f"  priced by an exact entry: {report['priced_token_share']:.1%} of tokens",
    ]
    if report["actionable"]:
        out.append(
            f"\nACTIONABLE GAPS ({len(report['actionable'])}, "
            f"{report['actionable_tokens']:,} tokens):"
        )
        out.extend(
            f"  {row['provider']:26s} {row['model']:28s} "
            f"{row['calls']:>7,} calls {row['tokens']:>15,} tok  "
            f"[{row['classification']}: {row['defect']}]"
            for row in report["actionable"]
        )
    else:
        out.append("\nACTIONABLE GAPS: none.")
    if report["acknowledged"]:
        out.append(f"\nACKNOWLEDGED, deliberately unpriced "
                   f"({len(report['acknowledged'])}):")
        out.extend(
            f"  {row['provider']:26s} {row['model']:28s} "
            f"{row['tokens']:>15,} tok  — {row['acknowledged_reason']}"
            for row in report["acknowledged"]
        )
    return "\n".join(out) + "\n"


def build_report(
    db_path: Optional[Path] = None,
    window_days: int = DEFAULT_WINDOW_DAYS,
    environ: Optional[Mapping[str, str]] = None,
) -> Dict[str, Any]:
    path = Path(db_path) if db_path else default_ledger_path(environ)
    return coverage_report(observed_pairs(path, window_days))


def main(argv: Optional[List[str]] = None) -> int:
    import argparse
    import json

    parser = argparse.ArgumentParser(
        description="Report price-coverage gaps over observed traffic."
    )
    parser.add_argument("--days", type=int, default=DEFAULT_WINDOW_DAYS)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument(
        "--monitor", action="store_true",
        help="stable gap-set only, for a cron monitor_script wake gate",
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    report = build_report(args.db, args.days)
    if args.monitor:
        print(monitor_lines(report), end="")
    elif args.json:
        print(json.dumps(report, indent=2))
    else:
        print(render_report(report, args.days), end="")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
