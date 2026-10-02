export type ShapeName = string;
export type SourceName = 'origin' | 'fallback' | 'none';
/** The scheme PREFERENCE. native = each theme in the half it was designed
 * in (the default — nothing changes until someone picks); light/dark = that
 * half wherever the theme offers it; system = follow prefers-color-scheme. */
export type SchemePref = 'native' | 'light' | 'dark' | 'system';
export interface GCSelection {
    theme: string;
    requestedTheme: string;
    shape: string | null;
    resolvedShape: string;
    scheme: SchemePref;
    /** the half that renders: 'light' | 'dark' — or null when the theme's
     * native half is unknown (pre-roster, unknown theme) and native was asked */
    resolvedScheme: string | null;
}
export interface GCConfig {
    origin: string;
    fallback: string | null;
    defaultTheme: string;
    /** RETIRED 2026-10-01 — always false. The selection is per app now; there
     * is no estate-wide selection to read or publish. Kept so consumers that
     * set GC_CONFIG.settings still type-check. */
    settings: boolean;
}
export interface SwitcherMountOptions {
    align?: 'left' | 'right';
    label?: string;
    [key: string]: unknown;
}
export interface GovernedCognitionSurface {
    themes(): string[];
    /** the offered shape list for a theme; no argument = the current theme */
    shapes(theme?: string): ShapeName[];
    nativeShape(theme: string): ShapeName;
    isHardEdged(theme: string): boolean;
    rosterSource(): 'seed' | 'origin' | 'fallback';
    apply(theme: string, shape?: string | null): {
        theme: string;
        shape: string;
    };
    current(): GCSelection;
    /** the schemes a theme carries, native first; no argument = current theme.
     * Empty when the roster does not say (pre-roster, or an index.json older
     * than schemes) — the sheet then decides. */
    schemes(theme?: string): string[];
    /** the half a theme was designed in, or null if the roster does not say */
    nativeScheme(theme: string): string | null;
    /** the scheme PREFERENCE (native | light | dark | system) */
    scheme(): SchemePref;
    setScheme(pref: SchemePref): SchemePref;
    mountSwitcher(target: string | Element, opts?: SwitcherMountOptions): Promise<Element>;
    /** RETIRED 2026-10-01 — always 'local': the selection is per app. */
    settingsSource(): 'local' | 'origin';
    themeSource(): SourceName | null;
    /** the liquid-glass PREFERENCE (the toggle). Default off. */
    glass(): boolean;
    /** the preference AND NOT prefers-reduced-transparency — what actually
     * renders. The reader's directive outranks the toggle, same rank order as
     * the reduced-motion clamp in every theme file. Apps mount glass off THIS. */
    glassEffective(): boolean;
    setGlass(on: boolean): boolean;
    /** glass opacity 0..1 — the backing wash behind a glass panel. 0 is pure
     * refraction, 1 is an opaque panel. This is the fine-grained transparency
     * control the on/off toggle only approximates, and it is what makes glass
     * viable on CONTENT panels: turn it up until the text underneath reads. */
    glassOpacity(): number;
    setGlassOpacity(value: number): number;
    /** THE RENDERER (2026-09-02, keeper: "GC owns the library, the apps call
     * it"). Make an element a glass host; a layer mounts on it while glass is
     * effective and leaves when it is not, the host's own inline styles come
     * back on detach. Which elements are the app's glass class, and how many
     * are attached at once on a long page, are the app's decisions; nothing
     * else is. */
    glassAttach(el: Element): void;
    glassDetach(el: Element): void;
    /** how many hosts carry a mounted layer right now */
    glassLayers(): number;
    config: GCConfig;
    rosterEvent: string;
    applyEvent: string;
    sourceEvent: string;
    glassEvent: string;
    ready: Promise<unknown> | null;
}
/** what switcher.js registers; the loader delivers and then calls it */
export interface GovernedCognitionSwitcherSurface {
    mount(target: string | Element, opts?: SwitcherMountOptions): Element;
}
export interface ThemeBackground {
    file: string;
    size: string;
    repeat: string;
    position: string;
}
declare global {
    var GovernedCognition: GovernedCognitionSurface | undefined;
    var GovernedCognitionSwitcher: GovernedCognitionSwitcherSurface | undefined;
    var GC_CONFIG: Partial<GCConfig> | undefined;
}
