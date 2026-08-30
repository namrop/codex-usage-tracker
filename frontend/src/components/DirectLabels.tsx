import type { CSSProperties } from "react";
import { compactIntFormat } from "../lib/format";

// Direct series labels beside a chart: swatch + name + total. Swatch colors
// are resolved GC token values handed through inline style — never literals.

export interface DirectLabelRow {
  label: string;
  detail: string;
  color: string;
  dashed?: boolean;
  ariaLabel?: string;
}

export function DirectLabels({ id, className, ariaLabel, rows }: {
  id: string;
  className?: string;
  ariaLabel: string;
  rows: DirectLabelRow[];
}) {
  return (
    <div id={id} className={className || "direct-labels"} aria-label={ariaLabel}>
      {rows.map((row, index) => {
        const swatchStyle: CSSProperties & Record<string, string> = {
          "--series-color": row.color,
        };
        if (row.dashed) {
          swatchStyle.background = "transparent";
          swatchStyle.borderTop = `2px dashed ${row.color}`;
        }
        return (
          <div className="direct-label" key={`${row.label}-${index}`} aria-label={row.ariaLabel}>
            <span className="swatch" style={swatchStyle} />
            <strong>{row.label}</strong>
            <span>{row.detail}</span>
          </div>
        );
      })}
    </div>
  );
}

export function tokenTotalDetail(total: unknown): string {
  return compactIntFormat(total);
}
