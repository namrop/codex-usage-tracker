/* Governed Cognition — runtime theme loader.
 *
 * Distribution (keeper-ratified 2026-08-29): repo = truth, origin = delivery,
 * vendored snapshot = fallback. This loader asks the origin
 * (http://127.0.0.1:7312) for the latest built CSS; if the origin does not
 * answer, it falls back to the app's bundled snapshot — an outage renders
 * yesterday's theme, never unstyled.
 *
 * No build, no CDN, vanilla JS. Works as a plain <script> OR as an ESM
 * side-effect import (`import './loader.js'`): there are no import/export
 * statements; the API attaches to globalThis.GovernedCognition.
 *
 * Config (all optional):
 *   <script src="loader.js" data-gc-origin="http://127.0.0.1:7312"
 *           data-gc-fallback="/static/gc" data-gc-default-theme="governed-cognition">
 *   — or set globalThis.GC_CONFIG = {origin, fallback, defaultTheme} before import.
 *
 * API: GovernedCognition.apply(theme, shape) — shape: 'rounded'|'slanted'|null
 *      (null = the theme's native shape). Selection persists in localStorage.
 *
 * Spec: Atrium uid 01a04ecd-8c2c-75ef-808c-c26c95de3a7f. */
(function () {
  'use strict';

  var THEMES = ['governed-cognition', 'neo-andean', 'johannah'];
  var NATIVE_SHAPE = {
    'governed-cognition': 'rounded',
    'neo-andean': 'rounded',
    'johannah': 'rounded'
  };
  var SHAPES = ['rounded', 'slanted'];
  var LS_THEME = 'gc.theme';
  var LS_SHAPE = 'gc.shape';

  var script = typeof document !== 'undefined' ? document.currentScript : null;
  var preset = (typeof globalThis !== 'undefined' && globalThis.GC_CONFIG) || {};
  var cfg = {
    origin: preset.origin ||
      (script && script.getAttribute('data-gc-origin')) ||
      'http://127.0.0.1:7312',
    fallback: preset.fallback ||
      (script && script.getAttribute('data-gc-fallback')) ||
      null,
    defaultTheme: preset.defaultTheme ||
      (script && script.getAttribute('data-gc-default-theme')) ||
      'governed-cognition'
  };

  function joinUrl(base, file) {
    return base.replace(/\/+$/, '') + '/' + file;
  }

  function store(key, value) {
    try {
      if (value === null) localStorage.removeItem(key);
      else localStorage.setItem(key, value);
    } catch (e) { /* storage unavailable: selection just doesn't persist */ }
  }

  function read(key) {
    try { return localStorage.getItem(key); } catch (e) { return null; }
  }

  /* Insert or update a managed <link>. Tries the origin first; if the origin
   * does not answer, the link's error event swaps in the bundled snapshot. */
  function setLink(id, file, afterId) {
    var link = document.getElementById(id);
    if (!file) {
      if (link) link.remove();
      return;
    }
    if (!link) {
      link = document.createElement('link');
      link.id = id;
      link.rel = 'stylesheet';
      var anchor = afterId && document.getElementById(afterId);
      if (anchor && anchor.nextSibling) {
        anchor.parentNode.insertBefore(link, anchor.nextSibling);
      } else {
        (document.head || document.documentElement).appendChild(link);
      }
    }
    link.onerror = function () {
      link.onerror = null; /* one fallback hop, no loops */
      if (cfg.fallback) link.href = joinUrl(cfg.fallback, file);
    };
    link.href = joinUrl(cfg.origin, file);
  }

  function resolveShape(theme, shape) {
    if (shape === 'native') shape = null;
    if (shape && SHAPES.indexOf(shape) === -1) shape = null;
    return shape || NATIVE_SHAPE[theme] || 'rounded';
  }

  function apply(theme, shape) {
    if (THEMES.indexOf(theme) === -1) theme = cfg.defaultTheme;
    var resolved = resolveShape(theme, shape);
    setLink('gc-theme', 'theme-' + theme + '.css');
    /* shape overlay loads AFTER the theme link so its :root override wins */
    setLink('gc-shape', resolved === 'slanted' ? 'shape-slant.css' : null, 'gc-theme');
    store(LS_THEME, theme);
    store(LS_SHAPE, shape || null);
    if (document.documentElement) {
      document.documentElement.setAttribute('data-gc-theme', theme);
      document.documentElement.setAttribute('data-gc-shape', resolved);
    }
    return { theme: theme, shape: resolved };
  }

  function current() {
    var theme = read(LS_THEME) || cfg.defaultTheme;
    if (THEMES.indexOf(theme) === -1) theme = cfg.defaultTheme;
    var shape = read(LS_SHAPE);
    return { theme: theme, shape: shape, resolvedShape: resolveShape(theme, shape) };
  }

  var GC = {
    themes: function () { return THEMES.slice(); },
    shapes: function () { return SHAPES.slice(); },
    nativeShape: function (theme) { return NATIVE_SHAPE[theme] || 'rounded'; },
    config: cfg,
    apply: apply,
    current: current
  };

  globalThis.GovernedCognition = GC;

  /* Apply the persisted (or default) selection immediately on load. */
  if (typeof document !== 'undefined') {
    var sel = current();
    apply(sel.theme, sel.shape);
  }
})();
