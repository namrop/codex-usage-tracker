export type ShapeName = string;
export type SourceName = 'origin' | 'fallback' | 'none';
export interface GCSelection {
    theme: string;
    requestedTheme: string;
    shape: string | null;
    resolvedShape: string;
}
export interface GCConfig {
    origin: string;
    fallback: string | null;
    defaultTheme: string;
    /** false when the app opted out of the estate selection (data-gc-settings="local") */
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
    mountSwitcher(target: string | Element, opts?: SwitcherMountOptions): Promise<Element>;
    settingsSource(): 'local' | 'origin';
    themeSource(): SourceName | null;
    config: GCConfig;
    rosterEvent: string;
    applyEvent: string;
    sourceEvent: string;
    ready: Promise<unknown> | null;
}
/** what switcher.js registers; the loader delivers and then calls it */
export interface GovernedCognitionSwitcherSurface {
    mount(target: string | Element, opts?: SwitcherMountOptions): Element;
}
declare global {
    var GovernedCognition: GovernedCognitionSurface | undefined;
    var GovernedCognitionSwitcher: GovernedCognitionSwitcherSurface | undefined;
    var GC_CONFIG: Partial<GCConfig> | undefined;
}
