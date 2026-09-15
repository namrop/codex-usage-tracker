"""AI Usage first implementation tranche (keeper authorization 2026-09-14):
account-level capacity summary, focused Now / History / Accounting /
Diagnostics navigation, consistent account labels, explicit burn projection
states, and list-price-equivalent financial copy.

Backend contract (``/api/quota-burn``): every series carries the same
``provider_account_index`` / ``provider_account_count`` ordinals the
subscriptions endpoint serializes, derived from the canonical private
identity at generation time (never from percentages or array position), and
an explicit ``projection_status`` enum with a ``projection_reason`` so the
frontend never has to guess why a row has no forecast.

Frontend contract: hash-routed views, the capacity summary ahead of the long
charts, every old chart and table still reachable, reset deep links still
landing on their card, and pure helper logic exercised by runnable Node
tests (``frontend/tests``) rather than substring checks alone.
"""

from __future__ import annotations

from datetime import datetime
import json
import os
from pathlib import Path
import re
import shutil
import subprocess

import pytest

import codex_usage_tracker.dashboard as dashboard_module
from codex_usage_tracker.dashboard import create_app

import frontend_surfaces

REPO_ROOT = Path(__file__).resolve().parents[1]
FRONTEND_DIR = REPO_ROOT / "frontend"


class _FrozenNoon(datetime):
    """2026-07-14T12:34:56Z, the frozen "now" every burn expectation below uses."""

    @classmethod
    def now(cls, tz=None):
        return cls(2026, 7, 14, 12, 34, 56, tzinfo=tz)


def _observation(source_id: str, **updates):
    row = {
        "fact_type": "quota_observation_v1",
        "schema_version": 1,
        "source_namespace": "private:test-quota",
        "source_observation_id": source_id,
        "harness": "claude_code",
        "observed_at": "2026-07-14T12:00:00Z",
        "provider": "anthropic",
        "quota_name": "five_hour",
        "quota_scope": "account",
        "window_kind": "rolling",
        "unit": "percent",
        "measurement_confidence": "exact",
        "limit_value": "100",
        "account_ref": None,
        "provider_payload_ref": "/private/provider/payload",
    }
    row.update(updates)
    if "used_value" in row and row.get("unit") == "percent" and "remaining_value" not in row:
        row["remaining_value"] = str(100 - float(row["used_value"]))
    return row


def _pct(source_id: str, observed_at: str, used: float, **updates):
    return _observation(source_id, observed_at=observed_at, used_value=str(used), **updates)


def _client(tmp_path, rows):
    ledger = tmp_path / "quota.jsonl"
    ledger.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    return create_app(quota_ledger=str(ledger)).test_client()


# ── Backend: account ordinals ────────────────────────────────────────────


def test_quota_burn_account_ordinals_agree_with_subscriptions(tmp_path, monkeypatch):
    """Two Claude accounts read as two burn rows AND wear the same ordinals the
    subscriptions endpoint hands out, even when the burn window only sees one
    of them: the roster is the dashboard's 168-hour subscription window, so a
    secondary account never gets renumbered "1" because the primary was quiet
    for the last hour. account_ref never crosses the API."""
    monkeypatch.setattr(dashboard_module, "datetime", _FrozenNoon)
    secondary = "claude-code-secondary"
    rows = [
        # Primary account (NULL account reference): sampled hours ago, so it
        # is inside the subscriptions roster but outside a one-hour burn read.
        _pct("p1", "2026-07-14T08:00:00Z", 40, resets_at="2026-07-14T13:00:00Z"),
        _pct("p2", "2026-07-14T09:00:00Z", 55, resets_at="2026-07-14T13:00:00Z"),
        # Secondary account: burning right now.
        _pct("s1", "2026-07-14T11:40:00Z", 20, resets_at="2026-07-14T14:00:00Z", account_ref=secondary),
        _pct("s2", "2026-07-14T12:10:00Z", 35, resets_at="2026-07-14T14:00:00Z", account_ref=secondary),
        # A single-account provider for the count=1 shape.
        _pct("k1", "2026-07-14T12:00:00Z", 92, provider="kimi-coding", harness="kimi_code",
             quota_name="week", window_kind="fixed", unit="provider_unit",
             resets_at="2026-07-20T00:00:00Z", account_ref="kimi-account"),
    ]
    client = _client(tmp_path, rows)

    subscriptions = client.get("/api/subscriptions?hours=168&history=0").get_json()
    latest = subscriptions["latest"]
    claude_by_used = {float(row["used_value"]): row for row in latest if row["provider_canonical"] == "anthropic"}
    assert claude_by_used[55.0]["provider_account_index"] == 1
    assert claude_by_used[35.0]["provider_account_index"] == 2
    assert all(row["provider_account_count"] == 2 for row in claude_by_used.values())

    burn = client.get("/api/quota-burn?hours=1").get_json()
    serialized = json.dumps(burn)
    assert secondary not in serialized
    assert "kimi-account" not in serialized
    assert "account_ref" not in serialized

    by_provider = {row["provider"]: row for row in burn["series"]}
    assert set(by_provider) == {"anthropic", "kimi-k3-coding"}
    claude = by_provider["anthropic"]
    # Only the secondary account burned in the last hour — and it is still
    # account 2 of 2, exactly as the subscriptions payload numbers it.
    assert claude["latest_used_pct"] == 35.0
    assert claude["provider_account_index"] == 2
    assert claude["provider_account_count"] == 2
    assert by_provider["kimi-k3-coding"]["provider_account_index"] == 1
    assert by_provider["kimi-k3-coding"]["provider_account_count"] == 1

    # The join key the capacity summary uses is complete: canonical provider,
    # quota name, harness, and account ordinal — never array position.
    for entry in burn["series"]:
        assert entry["harness"]
        assert entry["quota_scope"]
        assert entry["unit"]
        match = [
            row for row in latest
            if row["provider_canonical"] == entry["provider"]
            and row["quota_name"] == entry["quota_name"]
            and row["harness"] == entry["harness"]
            and row["provider_account_index"] == entry["provider_account_index"]
        ]
        assert len(match) == 1, entry
        assert float(match[0]["used_value"]) == entry["latest_used_pct"]


def test_quota_burn_ordinals_hold_when_only_a_later_account_burns(tmp_path, monkeypatch):
    """Reorder/filter: the ledger lists the secondary account first and the
    primary account has no observation at all in the burn window. The burn
    payload still numbers the secondary account 2, because the ordinal comes
    from the same canonical sort the subscriptions endpoint applies to the
    roster, not from the order rows arrive or which accounts happen to burn."""
    monkeypatch.setattr(dashboard_module, "datetime", _FrozenNoon)
    rows = [
        _pct("s1", "2026-07-14T12:00:00Z", 10, resets_at="2026-07-14T14:00:00Z", account_ref="zeta-account"),
        _pct("s2", "2026-07-14T12:20:00Z", 30, resets_at="2026-07-14T14:00:00Z", account_ref="zeta-account"),
        _pct("p1", "2026-07-13T12:00:00Z", 70, resets_at="2026-07-13T14:00:00Z"),
    ]
    client = _client(tmp_path, rows)
    subscriptions = client.get("/api/subscriptions?hours=168&history=0").get_json()
    ordinals = {float(row["used_value"]): row["provider_account_index"] for row in subscriptions["latest"]}
    assert ordinals == {70.0: 1, 30.0: 2}

    burn = client.get("/api/quota-burn?hours=2").get_json()
    assert [(row["latest_used_pct"], row["provider_account_index"], row["provider_account_count"]) for row in burn["series"]] == [
        (30.0, 2, 2),
    ]


# ── Backend: projection status enum ─────────────────────────────────────


def test_quota_burn_projection_status_covers_every_state(tmp_path, monkeypatch):
    """Every row carries an explicit projection status and reason. resets_first
    is asserted only with a positive measured rate, a future reset, and a
    fresh observation; balances are not-applicable rather than "unknown";
    stale observations never assert current capacity; a tool quota at its
    cap is exhausted like any other cap. The estimator arithmetic (rate over
    the last three monotonic rises, no forecast beyond reset) is untouched."""
    monkeypatch.setattr(dashboard_module, "datetime", _FrozenNoon)
    rows = [
        # exhausted: fresh, at 100.
        _pct("a1", "2026-07-14T11:00:00Z", 60, resets_at="2026-07-14T14:00:00Z"),
        _pct("a2", "2026-07-14T11:30:00Z", 80, resets_at="2026-07-14T14:00:00Z"),
        _pct("a3", "2026-07-14T12:00:00Z", 100, resets_at="2026-07-14T14:00:00Z"),
        # projected_before_reset: 40 pts/h with 50 left lands 13:15 < 16:00.
        _pct("b1", "2026-07-14T11:00:00Z", 10, provider="opencode-go", harness="opencode_go_api", resets_at="2026-07-14T16:00:00Z"),
        _pct("b2", "2026-07-14T11:30:00Z", 30, provider="opencode-go", harness="opencode_go_api", resets_at="2026-07-14T16:00:00Z"),
        _pct("b3", "2026-07-14T12:00:00Z", 50, provider="opencode-go", harness="opencode_go_api", resets_at="2026-07-14T16:00:00Z"),
        # resets_first: 4 pts/h with 86 left is 21.5 h away; reset at 14:00.
        _pct("c1", "2026-07-14T11:00:00Z", 10, provider="z-ai", harness="z_ai_api", resets_at="2026-07-14T14:00:00Z"),
        _pct("c2", "2026-07-14T11:30:00Z", 12, provider="z-ai", harness="z_ai_api", resets_at="2026-07-14T14:00:00Z"),
        _pct("c3", "2026-07-14T12:00:00Z", 14, provider="z-ai", harness="z_ai_api", resets_at="2026-07-14T14:00:00Z"),
        # insufficient_observations / single observation.
        _pct("d1", "2026-07-14T12:00:00Z", 92, provider="kimi-coding", harness="kimi_code", quota_name="week",
             window_kind="fixed", unit="provider_unit", resets_at="2026-07-20T00:00:00Z"),
        # insufficient_observations / flat (no positive rate).
        _pct("e1", "2026-07-14T11:00:00Z", 50, provider="openai-codex", harness="hermes", quota_name="week", resets_at="2026-07-18T00:00:00Z"),
        _pct("e2", "2026-07-14T12:00:00Z", 50, provider="openai-codex", harness="hermes", quota_name="week", resets_at="2026-07-18T00:00:00Z"),
        # not_applicable / balance: a lifetime credit balance is not a window.
        _observation("f1", provider="openrouter", harness="provider_api", quota_name="credit_balance",
                     window_kind="lifetime", unit="credits", used_value="10", limit_value="10", remaining_value="0",
                     account_ref="openrouter-account"),
        # exhausted tool quota: a monthly search cap at its cap.
        _observation("g1", provider="z-ai", harness="z_ai_api", quota_name="web_search_month", window_kind="fixed",
                     unit="requests", used_value="4000", limit_value="4000", remaining_value="0",
                     resets_at="2026-08-01T00:00:00Z"),
        # stale: latest observation older than the ledger max age (3 h).
        _pct("h1", "2026-07-14T08:00:00Z", 40, quota_name="seven_day", resets_at="2026-07-18T00:00:00Z", account_ref="claude-code-secondary"),
        _pct("h2", "2026-07-14T09:00:00Z", 45, quota_name="seven_day", resets_at="2026-07-18T00:00:00Z", account_ref="claude-code-secondary"),
        # stale: the reset passed after the last observation (window rolled).
        _pct("i1", "2026-07-14T11:00:00Z", 50, provider="kimi-coding", harness="kimi_code", unit="provider_unit", resets_at="2026-07-14T12:00:00Z"),
        _pct("i2", "2026-07-14T11:30:00Z", 70, provider="kimi-coding", harness="kimi_code", unit="provider_unit", resets_at="2026-07-14T12:00:00Z"),
        # not_applicable / no reset reported, despite a positive rate.
        _pct("j1", "2026-07-14T11:00:00Z", 10, provider="opencode-go", harness="opencode_go_api", quota_name="month", window_kind="fixed"),
        _pct("j2", "2026-07-14T12:00:00Z", 20, provider="opencode-go", harness="opencode_go_api", quota_name="month", window_kind="fixed"),
    ]
    client = _client(tmp_path, rows)
    payload = client.get("/api/quota-burn?hours=6").get_json()
    by_key = {(row["provider"], row["quota_name"], row["provider_account_index"]): row for row in payload["series"]}

    def status(provider, quota_name, index=1):
        row = by_key[(provider, quota_name, index)]
        return row["projection_status"], row["projection_reason"]

    assert status("anthropic", "five_hour") == ("exhausted", "used_at_cap")
    assert by_key[("anthropic", "five_hour", 1)]["exhausted"] is True

    projected = by_key[("opencode-go", "five_hour", 1)]
    assert projected["projection_status"] == "projected_before_reset"
    assert projected["projection_reason"] == "empty_before_reset_at_observed_rate"
    assert projected["projected_exhaustion_at"] == "2026-07-14T13:15:00Z"
    assert projected["burn_rate_pct_per_hour"] == pytest.approx(40.0, rel=1e-3)

    assert status("z-ai-glm", "five_hour") == ("resets_first", "reset_before_empty_at_observed_rate")
    assert by_key[("z-ai-glm", "five_hour", 1)]["projected_exhaustion_at"] is None

    assert status("kimi-k3-coding", "week") == ("insufficient_observations", "single_observation")
    assert status("openai-codex", "week") == ("insufficient_observations", "no_positive_rate")

    balance = by_key[("openrouter", "credit_balance", 1)]
    assert balance["projection_status"] == "not_applicable"
    assert balance["projection_reason"] == "balance_window"
    # The underlying data stays: the row is still serialized with its values.
    assert balance["latest_used_pct"] == 100.0
    assert balance["projected_exhaustion_at"] is None

    assert status("z-ai-glm", "web_search_month") == ("exhausted", "used_at_cap")

    assert status("anthropic", "seven_day", 2) == ("stale", "observation_stale")
    assert by_key[("anthropic", "seven_day", 2)]["exhausted"] is False
    assert status("kimi-k3-coding", "five_hour") == ("stale", "reset_passed_since_observation")
    assert status("opencode-go", "month") == ("not_applicable", "no_reset_reported")

    statuses = {row["projection_status"] for row in payload["series"]}
    assert statuses == {
        "exhausted", "projected_before_reset", "resets_first",
        "insufficient_observations", "not_applicable", "stale",
    }
    # Confidence travels with every row.
    assert all(row["measurement_confidence"] for row in payload["series"])
    assert all(row["confidence_status"] for row in payload["series"])
    # The description no longer claims only five-hour windows.
    assert "five-hour" not in payload["description"].lower() or "balance" in payload["description"].lower()
    assert "balance" in payload["description"].lower()


def test_quota_burn_estimated_rows_keep_their_confidence(tmp_path, monkeypatch):
    """A partial-coverage estimate of any provider keeps its confidence on
    the burn row. The one exception is the retired OpenCode Go USD replay
    (keeper acceptance 2026-09-14), which no projection uses at all."""
    monkeypatch.setattr(dashboard_module, "datetime", _FrozenNoon)
    rows = [
        _observation("o1", observed_at="2026-07-14T11:00:00Z", provider="openai-codex", harness="hermes",
                     unit="usd", used_value="2", limit_value="20", remaining_value="18",
                     measurement_confidence="estimated", resets_at="2026-07-14T15:00:00Z",
                     x_coverage={"status": "partial"}),
        _observation("o2", observed_at="2026-07-14T12:00:00Z", provider="openai-codex", harness="hermes",
                     unit="usd", used_value="4", limit_value="20", remaining_value="16",
                     measurement_confidence="estimated", resets_at="2026-07-14T15:00:00Z",
                     x_coverage={"status": "partial"}),
        _observation("go1", observed_at="2026-07-14T11:00:00Z", provider="opencode-go", harness="opencode",
                     unit="usd", used_value="2", limit_value="20", remaining_value="18",
                     measurement_confidence="estimated", resets_at="2026-07-14T15:00:00Z",
                     x_coverage={"status": "partial"}),
        _observation("go2", observed_at="2026-07-14T12:00:00Z", provider="opencode-go", harness="opencode",
                     unit="usd", used_value="4", limit_value="20", remaining_value="16",
                     measurement_confidence="estimated", resets_at="2026-07-14T15:00:00Z",
                     x_coverage={"status": "partial"}),
    ]
    client = _client(tmp_path, rows)
    payload = client.get("/api/quota-burn?hours=3").get_json()
    [row] = payload["series"]
    assert row["provider"] == "openai-codex"
    assert row["measurement_confidence"] == "estimated"
    assert row["confidence_status"] == "partial"
    assert row["projection_status"] == "resets_first"
    assert payload["exclusions"] == {"retired_opencode_go_estimate_rows": 2}


# ── Frontend: navigation, hierarchy, labels, copy ────────────────────────


def _src(relative: str) -> str:
    return frontend_surfaces.source(relative)


def test_navigation_is_hash_routed_with_four_real_views():
    views = _src("lib/views.ts")
    for view_id in ("now", "history", "accounting", "diagnostics"):
        assert f'"{view_id}"' in views
    assert "export function parseViewHash" in views
    assert "export function viewHref" in views
    # Existing reset deep links (#subscription-card-<key>) resolve to the Now
    # view and carry the anchor so the card is revealed, not dropped.
    assert "subscription-card-" in views

    nav = _src("components/ViewNav.tsx")
    assert '<nav className="view-nav" aria-label="Dashboard views">' in nav
    assert 'aria-current={' in nav
    assert "href={viewHref(" in nav
    # The refresh control lives on the shared header so every view can use it.
    assert 'id="refreshUsage"' in nav
    assert "onClick={onRefresh}" in nav

    app = _src("App.tsx")
    assert "hashchange" in app
    assert "<ViewNav" in app
    for view in ("NowView", "HistoryView", "AccountingView", "DiagnosticsView"):
        assert view in app
    # Inactive views unmount rather than hide, so Chart.js never sizes a
    # canvas inside a display:none container; charts rebuild on activation.
    for view_id, component in (
        ("now", "NowView"), ("history", "HistoryView"),
        ("accounting", "AccountingView"), ("diagnostics", "DiagnosticsView"),
    ):
        assert re.search(rf'view === "{view_id}" && \(?\s*<{component}', app), component
    assert "hidden={view" not in app, "views unmount; they are not hidden"


def test_now_view_leads_with_capacity_then_burn_then_detail():
    now = _src("views/NowView.tsx")
    capacity_at = now.index("<CapacityCard")
    burn_at = now.index("<BurnRateCard")
    rail_at = now.index("<ResetRail")
    status_at = now.index("<ProviderStatus")
    detail_at = now.index("<SubscriptionProviderDetail")
    assert capacity_at < burn_at < rail_at < status_at < detail_at
    # Long quota-history charts are not on the initial screen.
    assert "SubscriptionHistoryCharts" not in now
    assert "UnifiedUsageCard" not in now
    # Detail is a real collapsible (the estate fold), collapsed by default.
    subs = _src("components/SubscriptionsCard.tsx")
    assert 'foldKey="subscriptionProviderDetail"' in subs
    assert "export function SubscriptionProviderDetail" in subs
    assert "export function SubscriptionHistoryCharts" in subs
    assert "export function SubscriptionRawLedgerFold" in subs


def test_history_accounting_and_diagnostics_keep_every_old_surface():
    history = _src("views/HistoryView.tsx")
    assert "<UnifiedUsageCard" in history
    assert "<SubscriptionHistoryCharts" in history
    accounting = _src("views/AccountingView.tsx")
    assert "<ListPriceHeadline" in accounting
    assert "<BillingCard" in accounting
    assert "<RulingsPerDollarCard" in accounting
    diagnostics = _src("views/DiagnosticsView.tsx")
    assert "<SubscriptionRawLedgerFold" in diagnostics
    assert "lanes" in diagnostics
    assert "observations" in diagnostics  # burn observation evidence
    # Nothing is a placeholder: no view promises content it does not render.
    joined = frontend_surfaces.frontend_text()
    for placeholder in ("Coming soon", "coming soon", "TODO", "placeholder view"):
        assert placeholder not in joined


def test_reset_deep_links_reveal_the_provider_card_in_now():
    app = _src("App.tsx")
    assert "revealToken" in app or "reveal" in app
    # The scroll/focus lives in lib/reveal.ts since the second fix pass.
    assert "scrollIntoView" in app or "scrollIntoView" in _src("lib/reveal.ts")
    fold = _src("components/Fold.tsx")
    assert "revealToken" in fold
    rail = _src("components/ResetRail.tsx")
    assert "href={`#${providerCardDomId(chip.cardKey)}`}" in rail


def test_repeated_reset_link_reveals_the_card_again_and_focuses_it():
    """Second fix pass (parent browser QA 2026-09-14): clicking the SAME reset
    link twice — collapse the provider-detail fold in between — must reopen
    the fold. hashchange never fires for same-hash navigation, so App routes
    relevant same-document anchor clicks through sameDocumentAnchorNavigation
    (plain primary clicks only; modifiers, other targets, downloads and
    cross-document hrefs stay the browser's) and bumps the reveal token
    itself. The reveal waits for the card to be rendered and visible, then
    focuses it (the cards are focusable), retrying while focus has not
    landed."""
    app = _src("App.tsx")
    assert 'addEventListener("click"' in app
    assert "sameDocumentAnchorNavigation(" in app
    assert "sameHash" in app
    assert "scheduleReveal(" in app
    # No fake hash/query rewriting to force a change.
    assert "location.replace" not in app and "history.replaceState" not in app
    assert "revealToken" in _src("components/Fold.tsx")
    views = _src("lib/views.ts")
    assert "export function sameDocumentAnchorNavigation" in views
    reveal = _src("lib/reveal.ts")
    assert "export function scheduleReveal" in reveal
    assert "focused()" in reveal
    subs = _src("components/SubscriptionsCard.tsx")
    assert subs.count("tabIndex={-1}") >= 2, "provider cards (and placeholders) take programmatic focus"


def test_phone_header_is_compact_and_the_detail_stays_reachable():
    """Second fix pass (parent browser QA 2026-09-14): at 390px the first
    capacity account started at y≈1079, a full screen below the fold. The
    long hero subtitle and the capacity caption collapse behind a native
    <details> on phones (full text one tap away), the digest tiles sit two
    abreast, and the hero padding tightens. Desktop keeps the hero as it
    was; nothing pushes the root wider than the viewport."""
    hero = _src("components/OpsHero.tsx")
    assert "useViewportWidth()" in hero
    assert 'className="hero-brief"' in hero
    assert "<summary" in hero
    assert 'className="subtitle"' in hero, "the full subtitle text is still rendered"
    assert 'id="aiUsageTitle"' in hero and 'id="gcSwitcher"' in hero
    for digest_id in ("digestFreshness", "digestReset", "digestSpend", "digestSubstitutions"):
        assert f'id="{digest_id}"' in hero, digest_id
    card = _src("components/CapacityCard.tsx")
    assert "useViewportWidth()" in card
    assert 'className="card-brief"' in card
    assert 'id="capacityTitle"' in card

    css = frontend_surfaces.app_css()
    phone = css.split("@media (max-width: 640px)", 1)[1].split("\n}\n", 1)[0]
    assert re.search(r"\.title-panel\s*\{[^}]*padding:", phone)
    assert re.search(r"\.ops-digest\s*\{[^}]*grid-template-columns:\s*repeat\(2,", phone)
    assert re.search(r"\.digest-item\s*\{[^}]*padding:", phone)
    assert re.search(r"\n\.hero-brief\b", css) or re.search(r"\n\.hero-brief,", css)
    brief_rule = re.search(r"\n(?:\.hero-brief,\s*)?\.card-brief[^{]*\{([^}]*)\}", css) or re.search(r"\n\.hero-brief[^{]*\{([^}]*)\}", css)
    assert brief_rule is not None
    assert "overflow-wrap: anywhere" in css
    # The phone rules do not widen anything: the 2-up digest columns are
    # minmax(0, …) so a long value wraps instead of overflowing.
    assert "minmax(0, 1fr)" in phone


def test_capacity_summary_helper_is_pure_and_honest():
    capacity = _src("lib/capacity.ts")
    assert "export function deriveCapacityAccounts" in capacity
    assert "export function capacityWindowKind" in capacity
    for kind in ('"model"', '"model_specific"', '"tool"', '"balance"'):
        assert kind in capacity
    # No pooled capacity, no percent/dollar conflation, no routing.
    for forbidden in ("pooled", "totalRemaining", "route", "notify"):
        assert forbidden not in capacity
    card = _src("components/CapacityCard.tsx")
    assert "deriveCapacityAccounts(" in card
    assert 'id="capacityTitle"' in card
    # Estimated rows are labelled as such; nothing reads "Exact" unless the
    # provider measured it, and a partial row never reads "Exact" alone
    # (confidenceText carries coverage and measurement together).
    assert "confidenceText(" in card
    assert "export function confidenceText" in capacity
    assert '"allowance"' in capacity


def test_account_labels_share_one_helper_everywhere():
    providers = _src("lib/providers.ts")
    assert "export function accountDisplayLabel" in providers
    for surface in (
        "components/ResetRail.tsx",
        "lib/burn.ts",
        "lib/capacity.ts",
        "components/SubscriptionsCard.tsx",
    ):
        assert "accountDisplayLabel(" in _src(surface), surface


def test_burn_copy_names_every_window_kind_and_the_status_enum():
    burn_card = _src("components/BurnRateCard.tsx")
    assert "Per-account five-hour windows" not in burn_card
    assert "at the observed active-use rate" in burn_card
    burn_lib = _src("lib/burn.ts")
    for status in ("exhausted", "projected_before_reset", "resets_first",
                   "insufficient_observations", "not_applicable", "stale"):
        assert f'"{status}"' in burn_lib
    assert "projection_status" in burn_lib
    assert '"unknown"' not in burn_lib.split("export function projectionText", 1)[1].split("\n}\n", 1)[0]


def test_financial_copy_says_list_price_equivalent_not_spend():
    joined = frontend_surfaces.frontend_text()
    assert "Est. spend" not in joined
    assert "API list-price equivalent" in _src("components/OpsHero.tsx")
    headline = _src("components/ListPriceHeadline.tsx")
    assert "API list-price equivalent" in headline
    assert "not a bill" in headline or "not billed" in headline
    rulings = _src("components/RulingsPerDollarCard.tsx")
    assert "list-price equivalent" in rulings
    assert "recorded" in rulings
    # Actual billing stays its own record class, untouched.
    assert "Billing ledger" in _src("components/BillingCard.tsx")


def test_phone_containment_rules_exist():
    css = frontend_surfaces.app_css()
    nav_rule = re.search(r"\n\.view-nav\s*\{([^}]*)\}", css)
    assert nav_rule is not None
    assert "flex-wrap: wrap" in nav_rule.group(1) or "overflow-x: auto" in nav_rule.group(1)
    assert re.search(r"\.view-nav a\[aria-current=\"page\"\]", css)
    row_rule = re.search(r"\n\.capacity-account\s*\{([^}]*)\}", css)
    assert row_rule is not None
    assert "min-width: 0" in row_rule.group(1)
    assert re.search(
        r"@media \(max-width: 640px\).*?\.capacity-windows\s*\{[^}]*grid-template-columns:\s*1fr\s*;",
        css,
        re.DOTALL,
    )
    assert "overflow-x: hidden" in css


# ── Frontend: runnable Node tests for the pure helpers ───────────────────


def test_frontend_node_unit_tests_pass():
    """The pure helpers (capacity derivation, hash routing, burn rows, account
    labels) are exercised by real Node tests bundled with the esbuild that
    Vite already ships — not by substring checks. Skips only when the
    frontend toolchain is absent on this host."""
    node = shutil.which("node")
    esbuild = FRONTEND_DIR / "node_modules" / "esbuild"
    if node is None or not esbuild.exists():
        pytest.skip("frontend toolchain (node + node_modules/esbuild) not present")
    runner = FRONTEND_DIR / "tests" / "run.mjs"
    assert runner.is_file(), "frontend/tests/run.mjs is the Node test runner"
    completed = subprocess.run(
        [node, str(runner)],
        cwd=str(FRONTEND_DIR),
        env={**os.environ, "CI": "1"},
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "# fail 0" in completed.stdout, completed.stdout
