import { percent } from "./format";

// Native whole-point quota readings (2026-09-14). The OpenCode Go usage API
// reports each window as a WHOLE percent — floor-rounded, capped at 100 —
// and the backend marks rows that come from it with
// source_surface=opencode_go_usage_api, percent_resolution=1 and
// provider_state=ok|rate_limited (subscription `latest` rows, burn series
// rows, each weekly window's `current`). Printing such a reading as "100.0%" claims
// a precision the provider never gave; this module is the one place that
// decides a row is whole-point and renders it as an integer with the
// provider-reported label. Everything else — the cookie-backed Go surface,
// other providers, aggregate account means, derived rates — keeps the
// estate's one-decimal formatter. Both markers are required: a resolution
// without the native surface, or the surface without the resolution, is
// not claimed.

export const NATIVE_WHOLE_POINT_SURFACE = "opencode_go_usage_api";
export const WHOLE_POINT_LABEL = "provider-reported · whole points";

export interface NativeResolutionFields {
  source_surface?: unknown;
  percent_resolution?: unknown;
  provider_state?: unknown;
}

export function isWholePointNative(row: NativeResolutionFields | null | undefined): boolean {
  if (!row || typeof row !== "object") return false;
  if (String(row.source_surface || "") !== NATIVE_WHOLE_POINT_SURFACE) return false;
  return Number(row.percent_resolution) === 1;
}

// Integer text for a whole-point reading; the one-decimal estate formatter
// otherwise. A non-integer value on a whole-point row is printed as it is,
// never rounded into a false integer; a missing value is an em-dash.
export function nativePercentText(value: unknown, row: NativeResolutionFields | null | undefined): string {
  if (value === null || value === undefined || value === "") return "—";
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) return "—";
  if (isWholePointNative(row) && Number.isInteger(parsed)) return `${parsed.toFixed(0)}%`;
  return percent(parsed);
}

export function wholePointLabel(row: NativeResolutionFields | null | undefined): string | null {
  return isWholePointNative(row) ? WHOLE_POINT_LABEL : null;
}

// "rate limited" / "ok" — the provider's own state in words, or null when
// the row carries none.
export function providerStateText(row: NativeResolutionFields | null | undefined): string | null {
  const state = row && typeof row === "object" ? row.provider_state : null;
  if (state === null || state === undefined) return null;
  const text = String(state).trim();
  return text ? text.replace(/[-_]+/g, " ") : null;
}
