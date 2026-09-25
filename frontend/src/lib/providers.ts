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

// One name per provider (keeper-approved UI review item 5, Discord unified
// usage ledger thread msg 1553037883747598450, 2026-09-25): every surface
// says "Codex", "Claude", "Z.AI", "Kimi K3", "OpenCode Go". The API keeps its
// longer labels; the page shows these. The long forms (OpenAI / Codex,
// Anthropic / Claude, Z.AI / GLM, Kimi K3 Coding) are retired from display.
export const PROVIDER_DISPLAY_NAMES: Readonly<Record<string, string>> = Object.freeze({
  "openai-codex": "Codex",
  anthropic: "Claude",
  "opencode-go": "OpenCode Go",
  "z-ai-glm": "Z.AI",
  "kimi-k3-coding": "Kimi K3",
});

// Names for providers that are not subscriptions. The three acubens model
// servers (oMLX, the Gemma MoE lane on oMLX, llama.cpp MiniCPM) pool as one
// provider, "local-acubens", through the alias table below.
export const OTHER_PROVIDER_NAMES: Readonly<Record<string, string>> = Object.freeze({
  deepseek: "DeepSeek",
  openrouter: "OpenRouter",
  moa: "MoA council",
  "local-acubens": "Local (acubens)",
});

// Byte-identical to the server's _QUOTA_PROVIDER_ALIASES (a test parses it as
// JSON). The two Meridian lanes relay to the two Claude accounts (keeper,
// 2026-09-25), so usage on either reads as Anthropic.
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
  "custom:meridian-primary": "anthropic",
  "custom:meridian-yugen": "anthropic",
  "custom:omlx": "local-acubens",
  "custom:gemma-4-moe-it": "local-acubens",
  "custom:minicpm5": "local-acubens",
});

export const PROVIDER_ORDER: readonly string[] = Object.freeze([
  "openai-codex", "anthropic", "opencode-go", "z-ai-glm", "kimi-k3-coding",
]);

// The display name for any provider id, raw ("zai", "kimi-coding",
// "custom:omlx") or canonical ("z-ai-glm"). Accounts are not named here; see
// accountDisplayLabel for "Claude 1" / "Claude 2".
export function providerDisplayName(provider: unknown): string {
  const canonical = providerKey(provider);
  return SUBSCRIPTION_PROVIDER_SHORT_LABELS[canonical]
    || OTHER_PROVIDER_NAMES[canonical]
    || canonical;
}

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

// The ONE account label (2026-09-14): "Claude 1" / "Claude 2" on the
// capacity summary, the reset rail, the burn table and the provider cards.
// Identity is the canonical provider plus the API's account ordinal
// (provider_account_index of provider_account_count, derived server-side
// from the private account identity, never from array position); a single
// account wears the bare short label. Rows may carry `provider_canonical`
// (subscriptions) or a canonical `provider` (burn) — both resolve here.
export function accountDisplayLabel(
  row: {
    provider?: unknown;
    provider_canonical?: unknown;
    provider_account_count?: unknown;
    provider_account_index?: unknown;
  } | null | undefined,
): string {
  const canonicalRaw = row && row.provider_canonical;
  const canonical = canonicalRaw !== null && canonicalRaw !== undefined && canonicalRaw !== ""
    ? String(canonicalRaw)
    : providerKey(row && row.provider);
  const short = SUBSCRIPTION_PROVIDER_SHORT_LABELS[canonical]
    || PROVIDER_DISPLAY_NAMES[canonical]
    || OTHER_PROVIDER_NAMES[canonical]
    || canonical
    || "unknown";
  const count = Number(row && row.provider_account_count);
  const index = Number(row && row.provider_account_index);
  if (Number.isSafeInteger(count) && count > 1 && Number.isSafeInteger(index) && index >= 1) {
    return `${short} ${index}`;
  }
  return short;
}

// Stable per-provider card anchors (polish round item 13): the ids the rail
// chips and the digest chip link to. Keys are the canonical provider group
// keys the subscription cards render under.
export function providerCardDomId(key: unknown): string {
  return `subscription-card-${String(key || "unknown").toLowerCase().replace(/[^a-z0-9_-]+/g, "-")}`;
}
