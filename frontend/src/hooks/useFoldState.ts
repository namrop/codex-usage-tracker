import { useState } from "react";

// Ledger fold state (keeper-ruled 2026-08-29: ledger tables and their wide
// columns are collapsed by default). Estate law: fold is a class/attribute
// toggle, never a removal — every renderer keeps writing full table bodies
// into folded bodies, and fold state lives on the .fold wrapper (plus
// localStorage), which the hydrate cycle never touches.
const FOLD_OPEN_KEY = "aiUsage.fold.open";
const FOLD_COLS_KEY = "aiUsage.fold.cols";

function readFoldPrefs(storeKey: string): Record<string, unknown> {
  try {
    const raw = window.localStorage.getItem(storeKey);
    const parsed = raw ? JSON.parse(raw) : null;
    return parsed && typeof parsed === "object" ? parsed : {};
  } catch (error) {
    return {};
  }
}

function writeFoldPref(storeKey: string, foldKey: string, value: boolean): void {
  try {
    const prefs = readFoldPrefs(storeKey);
    prefs[foldKey] = value;
    window.localStorage.setItem(storeKey, JSON.stringify(prefs));
  } catch (error) {
    /* storage is a convenience; the fold works without it */
  }
}

// Default is collapsed / essential columns when the key is unset.
// ?expand=all opens everything for one load — a debug/print affordance; it
// does not write prefs.
function expandAllRequested(): boolean {
  return new URLSearchParams(location.search).has("expand");
}

export interface FoldState {
  open: boolean;
  toggleOpen: () => void;
  allCols: boolean;
  toggleCols: () => void;
}

export function useFoldState(foldKey: string): FoldState {
  const [open, setOpen] = useState<boolean>(() => (
    expandAllRequested() || readFoldPrefs(FOLD_OPEN_KEY)[foldKey] === true
  ));
  const [allCols, setAllCols] = useState<boolean>(() => (
    readFoldPrefs(FOLD_COLS_KEY)[foldKey] === true
  ));
  return {
    open,
    toggleOpen: () => {
      setOpen((prev) => {
        const next = !prev;
        writeFoldPref(FOLD_OPEN_KEY, foldKey, next);
        return next;
      });
    },
    allCols,
    toggleCols: () => {
      setAllCols((prev) => {
        const next = !prev;
        writeFoldPref(FOLD_COLS_KEY, foldKey, next);
        return next;
      });
    },
  };
}
