import { useEffect, useRef, type ReactNode } from "react";
import { chartConstructor } from "../lib/chart";
import { useFoldState } from "../hooks/useFoldState";
import { compactIntFormat, intFormat } from "../lib/format";

// The ledger fold (keeper-ruled 2026-08-29): tables and their wide columns
// are collapsed by default. Estate law: fold is a class/attribute toggle,
// never a removal — the full table body stays rendered inside the folded
// body, and fold state lives on the .fold wrapper plus localStorage, which
// the hydrate cycle never touches. The summary is derived from the same
// data the table renders from — no extra fetch, no server field — so every
// hydrate keeps it honest while the table stays folded.

export interface FoldMetric {
  total: number | null;
  label: string;
}

export function tableFoldSummary({
  rows,
  singular,
  plural,
  columns,
  colNoun,
  metrics = [],
}: {
  rows: number;
  singular: string;
  plural?: string;
  columns?: number;
  colNoun?: string;
  metrics?: FoldMetric[];
}): string {
  const pluralNoun = plural || `${singular}s`;
  if (!rows) return `no ${pluralNoun} yet`;
  const parts = [`${intFormat(rows)} ${rows === 1 ? singular : pluralNoun}`];
  if (colNoun && columns) {
    parts.push(`${intFormat(columns)} ${columns === 1 ? colNoun.replace(/s$/, "") : colNoun}`);
  }
  metrics.forEach(({ total, label }) => {
    // Counts read exactly (25,319 events); token volumes compact (4.1B) — a
    // nine-digit sum in a one-line summary is a wall, not a summary.
    if (total !== null) {
      const shown = Math.abs(total) >= 1e6 ? compactIntFormat(total) : intFormat(total);
      parts.push(`${shown} ${label}`);
    }
  });
  return parts.join(" · ");
}

interface FoldProps {
  foldKey: string;
  label: string;
  summary: string;
  headingLevel?: 3 | 4;
  colsToggle?: boolean;
  // A counter the owner bumps to reveal the body (a deep link into a card
  // inside the fold). Zero/undefined never opens anything; the preference
  // is not written — a link revealing a card is not the reader's choice
  // to keep it open.
  revealToken?: number;
  children: ReactNode;
}

export function Fold({ foldKey, label, summary, headingLevel = 3, colsToggle = false, revealToken = 0, children }: FoldProps) {
  const { open, toggleOpen, reveal, allCols, toggleCols } = useFoldState(foldKey);
  const sectionRef = useRef<HTMLElement | null>(null);
  const Heading = headingLevel === 4 ? "h4" : "h3";

  useEffect(() => {
    if (revealToken > 0) reveal();
    // reveal is stable for the fold's lifetime; only the token drives this.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [revealToken]);

  const onToggle = () => {
    const willOpen = !open;
    toggleOpen();
    if (willOpen) {
      // Chart.js sizes a canvas from its container; a chart built while the
      // fold was display:none is 0×0 and stays blank until told the world
      // changed (keeper defect report 2026-08-30: "a bunch of the graphs
      // still don't work").
      requestAnimationFrame(() => {
        const Ctor = chartConstructor();
        if (!Ctor || !sectionRef.current) return;
        sectionRef.current.querySelectorAll("canvas").forEach((canvas) => {
          const chart = Ctor.getChart ? Ctor.getChart(canvas) : null;
          if (chart) chart.resize();
        });
      });
    }
  };

  return (
    <section
      ref={sectionRef}
      className="fold"
      data-fold={foldKey}
      data-open={open ? "true" : "false"}
      data-cols={colsToggle ? (allCols ? "all" : "essential") : undefined}
    >
      <div className="fold-head">
        <Heading className="fold-heading">
          <button
            type="button"
            className="fold-toggle"
            id={`${foldKey}FoldToggle`}
            aria-expanded={open}
            aria-controls={`${foldKey}FoldBody`}
            onClick={onToggle}
          >
            <span className="fold-caret" aria-hidden="true">▸</span>
            <span className="fold-label">{label}</span>
            <span className="fold-summary" data-fold-summary>{summary}</span>
          </button>
        </Heading>
        {colsToggle && (
          <button
            type="button"
            className="fold-cols"
            data-fold-cols
            aria-pressed={allCols}
            aria-controls={`${foldKey}FoldBody`}
            onClick={toggleCols}
          >
            All columns
          </button>
        )}
      </div>
      <div className="fold-body" id={`${foldKey}FoldBody`} role="region" aria-labelledby={`${foldKey}FoldToggle`}>
        {children}
      </div>
    </section>
  );
}
