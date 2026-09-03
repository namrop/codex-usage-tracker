import { RANK_SPREAD_ORDER, type Palette } from "./palette";

// Providers pin fixed slots of the rank set (nearest-hue to the retired
// literal map) so a provider keeps one color across renders; unmapped
// providers walk the UNCLAIMED slots by index (below), so they never wear
// a mapped provider's color.
export const SUBSCRIPTION_UNIFIED_PROVIDER_RANK: Readonly<Record<string, number>> = Object.freeze({
  "openai-codex": 0,
  "anthropic": 5,
  "opencode-go": 3,
  "z-ai-glm": 7,
  "kimi-k3-coding": 2,
});

// The rank set has ten members (rank-1 … rank-10, indexed 0–9); these are
// the slots the map above leaves free — 4, 9, 1, 6, 8 as of this writing —
// derived rather than listed so a change to the map cannot desynchronize.
// Walked in hue-spread order (RANK_SPREAD_ORDER), not by rank: the first
// unmapped provider used to land on slot 1, the light blue beside
// openai-codex's slot-0 cyan.
export const SUBSCRIPTION_UNIFIED_UNCLAIMED_RANK_SLOTS: readonly number[] = Object.freeze(
  RANK_SPREAD_ORDER
    .filter((slot) => !Object.values(SUBSCRIPTION_UNIFIED_PROVIDER_RANK).includes(slot)),
);

// Display-label mapping ONLY (polish round item 1): the unified utilization
// legends read "Claude 1", "Codex", … instead of the full series labels that
// wrapped mid-word. Series identity and data are untouched — the fold tables
// keep the full labels.
export const SUBSCRIPTION_PROVIDER_SHORT_LABELS: Readonly<Record<string, string>> = Object.freeze({
  "anthropic": "Claude",
  "openai-codex": "Codex",
  "opencode-go": "OpenCode Go",
  "z-ai-glm": "Z.AI",
  "kimi-k3-coding": "Kimi K3",
});

export const PROVIDER_DISPLAY_NAMES: Readonly<Record<string, string>> = Object.freeze({
  "openai-codex": "OpenAI / Codex",
  anthropic: "Anthropic / Claude",
  "opencode-go": "OpenCode Go",
  "z-ai-glm": "Z.AI / GLM",
  "kimi-k3-coding": "Kimi K3 Coding",
});

export const PROVIDER_ALIASES: Readonly<Record<string, string>> = Object.freeze({
  "openai": "openai-codex",
  "openai-codex": "openai-codex",
  "codex": "openai-codex",
  "anthropic": "anthropic",
  "claude": "anthropic",
  "claude-code": "anthropic",
  "opencode-go": "opencode-go",
  "z.ai": "z-ai-glm",
  "z-ai": "z-ai-glm",
  "zai": "z-ai-glm",
  "z-ai-glm": "z-ai-glm",
  "zhipu": "z-ai-glm",
  "zhipuai": "z-ai-glm",
  "glm": "z-ai-glm",
  "kimi": "kimi-k3-coding",
  "kimi-code": "kimi-k3-coding",
  "kimi-coding": "kimi-k3-coding",
  "kimi-k3-coding": "kimi-k3-coding",
  "moonshot": "kimi-k3-coding",
  "moonshot-ai": "kimi-k3-coding",
});

export const PROVIDER_ORDER: readonly string[] = Object.freeze([
  "openai-codex", "anthropic", "opencode-go", "z-ai-glm", "kimi-k3-coding",
]);

export function providerKey(provider: unknown): string {
  const rawProvider = String(provider || "unknown").trim() || "unknown";
  const aliasKey = rawProvider.toLowerCase();
  return Object.prototype.hasOwnProperty.call(PROVIDER_ALIASES, aliasKey)
    ? PROVIDER_ALIASES[aliasKey]
    : rawProvider;
}

export function subscriptionUnifiedProviderColor(
  palette: Palette,
  row: { provider?: unknown; provider_canonical?: unknown } | null | undefined,
  index: number,
): string {
  const canonical = row && row.provider_canonical;
  const provider = String((canonical !== null && canonical !== undefined && canonical !== "" ? canonical : row && row.provider) || "")
    .trim()
    .toLowerCase();
  const rank = SUBSCRIPTION_UNIFIED_PROVIDER_RANK[provider];
  if (rank !== undefined && palette.rankColors[rank]) return palette.rankColors[rank];
  // Unmapped: cycle the free slots only, so no collision with a pinned one.
  const free = SUBSCRIPTION_UNIFIED_UNCLAIMED_RANK_SLOTS;
  const slot = free.length ? free[((index % free.length) + free.length) % free.length] : undefined;
  if (slot !== undefined && palette.rankColors[slot]) return palette.rankColors[slot];
  return palette.modelColors[index % palette.modelColors.length];
}

export function subscriptionSeriesDisplayLabel(
  row: { provider?: unknown; label?: unknown; provider_account_count?: unknown; provider_account_index?: unknown } | null | undefined,
): string {
  const provider = String((row && row.provider) || "").trim().toLowerCase();
  const short = SUBSCRIPTION_PROVIDER_SHORT_LABELS[provider];
  if (!short) return String((row && row.label) || "Quota");
  return Number(row!.provider_account_count) > 1
    ? `${short} ${row!.provider_account_index}`
    : short;
}

// Stable per-provider card anchors (polish round item 13): the ids the rail
// chips and the digest chip link to. Keys are the canonical provider group
// keys the subscription cards render under.
export function providerCardDomId(key: unknown): string {
  return `subscription-card-${String(key || "unknown").toLowerCase().replace(/[^a-z0-9_-]+/g, "-")}`;
}
