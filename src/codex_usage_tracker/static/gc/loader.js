/* Governed Cognition — runtime theme loader.
 *
 * Distribution (keeper-ratified 2026-08-29): repo = truth, origin = delivery,
 * vendored snapshot = fallback. This loader asks the origin
 * (http://127.0.0.1:7312) for the latest built CSS; if the origin does not
 * answer, it falls back to the app's bundled snapshot — an outage renders
 * yesterday's theme, never unstyled.
 *
 * The ROSTER follows the same doctrine (2026-08-30). It used to be a
 * hardcoded array here, which meant a new theme did not propagate: the origin
 * served its CSS, but every app's bundled loader still refused the name and
 * its switcher never listed it. index.json was generated, served, and read by
 * nobody. Now the roster is fetched — origin first, bundled snapshot second,
 * the array below as an offline seed so first paint is never blocked. Adding
 * a theme is once again one commit + generate.py, with no app release.
 *
 * No build, no CDN, vanilla JS. Works as a plain <script> OR as an ESM
 * side-effect import (`import './loader.js'`): there are no import/export
 * statements; the API attaches to globalThis.GovernedCognition.
 *
 * The SELECTION is estate-wide (2026-08-30). It used to live in each app's
 * localStorage, which is per-origin: pick johannah in Diadem and the ai-usage
 * site still opened in governed-cognition — one theme system, N selections.
 * The origin now holds it (GET/PUT /settings) and is authoritative while it
 * answers; localStorage demotes to an offline cache and the fallback. Same
 * doctrine as everything else here, applied to the choice itself.
 *
 * Config (all optional):
 *   <script src="loader.js" data-gc-origin="http://127.0.0.1:7312"
 *           data-gc-fallback="/static/gc" data-gc-default-theme="governed-cognition">
 *   — or set globalThis.GC_CONFIG = {origin, fallback, defaultTheme} before import.
 *
 * The RUNTIME is delivered too (contract extended 2026-08-30). dist/ carries
 * loader.js, switcher.js and switcher.css alongside the values, and
 * GovernedCognition.mountSwitcher() fetches the picker on demand through the
 * same origin -> snapshot hops. A switcher vendored per app drifts the moment
 * it is copied, which is the opposite of standardizing it. This file stays the
 * one an app holds locally: it IS the fallback mechanism, so it cannot be
 * delivered by it.
 *
 * API: GovernedCognition.apply(theme, shape) — shape: 'rounded'|'slanted'|null
 *      (null = the theme's native shape). Selection persists in localStorage.
 *      .themes() / .nativeShape(t) read the live roster; .ready is a promise
 *      resolving once the fetched roster (or the seed, on failure) is settled;
 *      a `gc:roster` CustomEvent fires on document whenever it changes.
 *      .mountSwitcher(target, opts) -> Promise<rootElement> loads the picker
 *      (css + js, origin first) and mounts it; no <script> tag needed.
 *
 * Spec: Atrium uid 01a04ecd-8c2c-75ef-808c-c26c95de3a7f. */
(function () {
  'use strict';

  /* Offline seed. Not the source of truth — index.json is — but it lets the
   * first paint happen before any fetch resolves, and it is what an app with
   * no origin and no snapshot degrades to. */
  var SEED_THEMES = ['governed-cognition', 'neo-andean', 'johannah'];
  var SEED_NATIVE_SHAPE = {
    'governed-cognition': 'rounded',
    'neo-andean': 'rounded',
    'johannah': 'rounded'
  };
  /* hard and slanted are a pair: both are 'not round', and slanted is hard
   * plus the cuts. rounded is the odd one — the only shape whose look depends
   * on the theme's own radius scale, which is why a theme with a zero scale is
   * natively hard and does not offer rounded at all. */
  var SEED_SHAPES = ['rounded', 'hard', 'slanted'];
  var SEED_SHAPE_CSS = {
    rounded: null,
    hard: 'shape-hard.css',
    slanted: 'shape-slant.css'
  };
  var LS_THEME = 'gc.theme';
  var LS_SHAPE = 'gc.shape';
  var ROSTER_EVENT = 'gc:roster';
  var APPLY_EVENT = 'gc:apply';
  var SOURCE_EVENT = 'gc:source';

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
      'governed-cognition',
    /* data-gc-settings="local" keeps an app out of the estate selection —
     * it then persists to localStorage only, as before. */
    settings: (preset.settings !== undefined
      ? preset.settings
      : (script && script.getAttribute('data-gc-settings'))) !== 'local'
  };

  /* hardEdged is filled from index.json, not seeded: it only changes an
   * option's LABEL, so defaulting to false offline costs a slightly-wrong
   * word, not a wrong render — and a second seed map would be a second thing
   * to keep in sync with the repo. */
  var roster = {
    themes: SEED_THEMES.slice(),
    nativeShape: Object.assign({}, SEED_NATIVE_SHAPE),
    hardEdged: {},
    shapes: SEED_SHAPES.slice(),
    shapeCss: Object.assign({}, SEED_SHAPE_CSS),
    themeShapes: {},
    source: 'seed'
  };

  /* What the caller last ASKED for, kept separate from what is rendered. A
   * theme the seed does not know renders as the default but is not forgotten:
   * when the roster arrives it is applied for real. Coercing the stored value
   * instead would silently destroy a preference the origin could honour. */
  var desired = { theme: cfg.defaultTheme, shape: null };

  function joinUrl(base, file) {
    return base.replace(/\/+$/, '') + '/' + file;
  }

  /* link.href reads back ABSOLUTE, so a relative candidate ("/theme-x.css",
   * the usual data-gc-fallback shape) never string-matches the href it just
   * set. That made the "already loaded" check miss, and re-assigning an
   * unchanged href fires neither load nor error — so the timer walked a
   * perfectly good sheet to the next candidate and, with the origin down,
   * off the end of the chain into an unstyled page. Compare like with like. */
  function absUrl(url) {
    try {
      return new URL(url, document.baseURI).href;
    } catch (e) {
      return url;
    }
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

  /* Cold tailnet-proxy paths can exceed 2.5s on first hit. A hanging origin
   * never fires `error`, so without a timer the app waits forever on a socket
   * that will not answer — an outage that renders nothing, which is the one
   * thing this loader exists to prevent. (Ported from Diadem's loader,
   * 2026-08-30, so adopting this one is not a regression.) */
  var LOAD_TIMEOUT_MS = 4000;

  /* 0 = origin, 1 = bundled snapshot. Remembered per slot so the shape
   * overlay can try the SAME source the theme resolved from first: mixing an
   * origin theme with a vendored shape (or the reverse) risks skew when the
   * two snapshots differ. Also ported from Diadem. */
  var sourceIdx = { theme: 0, shape: 0 };

  /* render() runs more than once on a normal boot — paint from cache, then
   * again when the roster lands, then again if the estate selection differs.
   * Each call walking its own candidate chain over the SAME <link> means two
   * chains racing: both assign onload/onerror, both assign href, and the
   * loser's stale callback reports for a URL nobody is waiting on. Observed
   * leaving a dead-origin href live and the page unstyled — the exact failure
   * the fallback exists to prevent. A generation per slot retires the loser. */
  var chainGen = {};

  /* Each candidate carries its SOURCE IDENTITY, not just a url. Pinning
   * reorders this list, so a position is not a source: after preferring the
   * snapshot, position 0 is the snapshot. Reporting the position as the
   * source made the next render pin back to a dead origin and the page
   * rendered unstyled. src is 0 = origin, 1 = bundled snapshot, always. */
  function candidates(file, prefer) {
    var list = [{ src: 0, url: absUrl(joinUrl(cfg.origin, file)) }];
    if (cfg.fallback) list.push({ src: 1, url: absUrl(joinUrl(cfg.fallback, file)) });
    if (prefer === 1 && list.length === 2) list = [list[1], list[0]];
    return list;
  }

  /* Insert or update a managed <link>, walking origin -> snapshot. done(idx)
   * reports which source won, or -1 when the chain is exhausted (or when
   * there is no file for this slot, e.g. rounded has no overlay). */
  function setLink(id, file, afterId, prefer, done) {
    var gen = (chainGen[id] = (chainGen[id] || 0) + 1);
    function current() { return chainGen[id] === gen; }

    var link = document.getElementById(id);
    if (!file) {
      if (link) link.remove();
      if (done) done(-1);
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
    var urls = candidates(file, prefer);
    var i = 0;
    function attempt() {
      if (!current()) return; /* a newer render owns this link now */
      if (i >= urls.length) {
        if (done) done(-1);
        return;
      }
      /* Assigning an href its current value changes no attribute, so neither
       * load nor error fires — the timer would then walk us to the fallback
       * for a sheet that is already live. A parsed sheet on the same href is
       * the answer, not a miss. (link.sheet is readable cross-origin; only
       * cssRules is not.) */
      if (link.href === urls[i].url && link.sheet) {
        if (done) done(urls[i].src);
        return;
      }
      var settled = false;
      var timer = setTimeout(miss, LOAD_TIMEOUT_MS);
      function miss() {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        if (!current()) return;
        i += 1;
        attempt();
      }
      link.onerror = miss;
      link.onload = function () {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        if (!current()) return;
        if (done) done(urls[i].src);
      };
      link.href = urls[i].url;
    }
    attempt();
  }

  /* origin | fallback | none. Diadem attaches its token-bridge sheet only
   * while a GC theme is actually live: with no GC layer present the bridge's
   * var(--gc-*) references resolve to nothing and its own base tokens must
   * stand alone. Published so that decision does not need a private loader. */
  var themeSource = 'origin';

  function setSource(idx) {
    var next = idx === 0 ? 'origin' : (idx === 1 ? 'fallback' : 'none');
    if (next === themeSource) return;
    themeSource = next;
    if (document.documentElement) {
      document.documentElement.setAttribute('data-gc-theme-source', next);
    }
    if (typeof document !== 'undefined' && document.dispatchEvent) {
      document.dispatchEvent(new CustomEvent(SOURCE_EVENT, {
        detail: { source: next }
      }));
    }
  }

  function knownTheme(theme) {
    return roster.themes.indexOf(theme) !== -1;
  }

  /* Which shapes this theme meaningfully offers. A hard-edged theme's rounded
   * and hard render identically, so it offers hard and slanted only — listing
   * both would put a choice in the picker that changes nothing. */
  function shapesFor(theme) {
    var listed = roster.themeShapes && roster.themeShapes[theme];
    if (listed && listed.length) return listed;
    if (roster.hardEdged[theme] === true) {
      return roster.shapes.filter(function (s) { return s !== 'rounded'; });
    }
    return roster.shapes.slice();
  }

  function resolveShape(theme, shape) {
    if (shape === 'native') shape = null;
    var offered = shapesFor(theme);
    if (shape && offered.indexOf(shape) === -1) shape = null;
    return shape || roster.nativeShape[theme] || offered[0] || 'rounded';
  }

  /* Render whatever `desired` resolves to against the roster we currently
   * hold. Called on apply() and again whenever the roster changes. */
  function render() {
    var theme = knownTheme(desired.theme) ? desired.theme : cfg.defaultTheme;
    var resolved = resolveShape(theme, desired.shape);
    setLink('gc-theme', 'theme-' + theme + '.css', null, sourceIdx.theme,
      function (idx) {
        if (idx >= 0) sourceIdx.theme = idx;
        setSource(idx);
        /* The shape overlay loads AFTER the theme link so its :root override
         * wins, and pins to whichever source the theme actually came from.
         * rounded has no overlay: it IS the theme's own radius scale. */
        setLink('gc-shape', roster.shapeCss[resolved] || null, 'gc-theme',
          sourceIdx.theme, function (sidx) {
            if (sidx >= 0) sourceIdx.shape = sidx;
          });
      });
    if (document.documentElement) {
      document.documentElement.setAttribute('data-gc-theme', theme);
      document.documentElement.setAttribute('data-gc-shape', resolved);
    }
    /* Announced so any mounted picker reflects a selection it did not make —
     * a programmatic apply(), another switcher on the page, or the roster
     * arriving and making a persisted theme renderable at last. */
    if (typeof document !== 'undefined' && document.dispatchEvent) {
      document.dispatchEvent(new CustomEvent(APPLY_EVENT, {
        detail: { theme: theme, shape: resolved }
      }));
    }
    return { theme: theme, shape: resolved };
  }

  function apply(theme, shape) {
    if (typeof theme !== 'string' || !theme) theme = cfg.defaultTheme;
    desired.theme = theme;
    desired.shape = shape || null;
    store(LS_THEME, theme);
    store(LS_SHAPE, shape || null);
    publishSettings();
    return render();
  }

  /* Fire-and-forget: a selection that cannot reach the origin still applies
   * here and still persists locally. The estate simply learns about it the
   * next time this app is opened with the origin up. */
  function publishSettings() {
    if (!cfg.settings || typeof fetch !== 'function') return;
    try {
      fetch(joinUrl(cfg.origin, 'settings'), {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ theme: desired.theme, shape: desired.shape })
      }).catch(function () { /* origin down: local selection stands */ });
    } catch (e) { /* no fetch, or blocked: local selection stands */ }
  }

  /* The estate's selection, read once at load. Applied only if it differs
   * from what this app already rendered, so the common case (they agree) does
   * not touch the DOM. Does NOT re-publish — adopting a value is not a
   * choice, and echoing it back would race two apps opening at once. */
  function loadSettings() {
    if (!cfg.settings) return Promise.resolve(null);
    return fetchJson(joinUrl(cfg.origin, 'settings'))
      .then(function (data) {
        if (!data || typeof data !== 'object') return null;
        var theme = typeof data.theme === 'string' ? data.theme : desired.theme;
        var shape = data.shape === null || typeof data.shape === 'string'
          ? data.shape
          : desired.shape;
        if (theme === desired.theme && shape === desired.shape) return data;
        desired.theme = theme;
        desired.shape = shape || null;
        store(LS_THEME, theme);
        store(LS_SHAPE, shape || null);
        render();
        return data;
      })
      .catch(function () { return null; });
  }

  function current() {
    var rendered = knownTheme(desired.theme) ? desired.theme : cfg.defaultTheme;
    return {
      theme: rendered,
      requestedTheme: desired.theme,
      shape: desired.shape,
      resolvedShape: resolveShape(rendered, desired.shape)
    };
  }

  /* --- roster (index.json), origin -> bundled snapshot -> seed ------------ */

  function parseRoster(data) {
    if (!data || !Array.isArray(data.themes) || !data.themes.length) return null;
    var names = [];
    var native = {};
    var hard = {};
    var themeShapes = {};
    for (var i = 0; i < data.themes.length; i++) {
      var entry = data.themes[i];
      var name = entry && entry.name;
      if (typeof name !== 'string' || !name) continue;
      names.push(name);
      native[name] = entry.native_shape || 'rounded';
      themeShapes[name] = Array.isArray(entry.shapes) && entry.shapes.length
        ? entry.shapes.slice()
        : null;
      hard[name] = entry.hard_edged === true;
    }
    /* the shape roster is discovered too, css filenames and all — a shape
     * added upstream should not need an app release either */
    var shapes = [];
    var shapeCss = {};
    if (Array.isArray(data.shapes)) {
      for (var j = 0; j < data.shapes.length; j++) {
        var s = data.shapes[j];
        if (!s || typeof s.name !== 'string' || !s.name) continue;
        shapes.push(s.name);
        shapeCss[s.name] = s.css || null;
      }
    }
    if (!shapes.length) {
      shapes = SEED_SHAPES.slice();
      shapeCss = Object.assign({}, SEED_SHAPE_CSS);
    }
    return names.length
      ? {
          themes: names, nativeShape: native, hardEdged: hard,
          themeShapes: themeShapes, shapes: shapes, shapeCss: shapeCss
        }
      : null;
  }

  function adoptRoster(parsed, source) {
    var before = roster.themes.join(' ');
    roster = {
      themes: parsed.themes,
      nativeShape: parsed.nativeShape,
      hardEdged: parsed.hardEdged,
      shapes: parsed.shapes,
      shapeCss: parsed.shapeCss,
      themeShapes: parsed.themeShapes,
      source: source
    };
    render(); /* a theme that was unknown a moment ago may now be renderable */
    if (typeof document !== 'undefined' && document.dispatchEvent) {
      document.dispatchEvent(new CustomEvent(ROSTER_EVENT, {
        detail: {
          themes: roster.themes.slice(),
          source: source,
          changed: before !== roster.themes.join(' ')
        }
      }));
    }
  }

  function fetchJson(url) {
    if (typeof fetch !== 'function') return Promise.reject(new Error('no fetch'));
    return fetch(url, { cache: 'no-cache' }).then(function (res) {
      if (!res.ok) throw new Error('HTTP ' + res.status);
      return res.json();
    });
  }

  /* Same hop order as the stylesheets, for the same reason: the origin is
   * delivery, the snapshot is what an outage falls back to. A failure here is
   * not an error — the seed roster stands and the app renders. */
  function loadRoster() {
    return fetchJson(joinUrl(cfg.origin, 'index.json'))
      .then(function (data) { return { data: data, source: 'origin' }; })
      .catch(function () {
        if (!cfg.fallback) throw new Error('no fallback configured');
        return fetchJson(joinUrl(cfg.fallback, 'index.json'))
          .then(function (data) { return { data: data, source: 'fallback' }; });
      })
      .then(function (hit) {
        var parsed = parseRoster(hit.data);
        if (parsed) adoptRoster(parsed, hit.source);
        return roster;
      })
      .catch(function () { return roster; });
  }

  /* --- delivered runtime: the picker, fetched on demand --------------- */

  var switcherLoad = null;
  var settingsSource = 'local';

  /* Same two hops as everything else. A <script> cannot be retried in place,
   * so the fallback is a second tag appended after the first one errors. */
  function injectScript(file) {
    return new Promise(function (resolve, reject) {
      function attempt(base, onFail) {
        var el = document.createElement('script');
        el.src = joinUrl(base, file);
        el.async = false;
        el.onload = function () { resolve(); };
        el.onerror = onFail;
        (document.head || document.documentElement).appendChild(el);
      }
      attempt(cfg.origin, function () {
        if (!cfg.fallback) {
          reject(new Error('gc: cannot load ' + file + ' (no fallback configured)'));
          return;
        }
        attempt(cfg.fallback, function () {
          reject(new Error('gc: cannot load ' + file));
        });
      });
    });
  }

  /* Mount the standard picker without the app carrying its own copy. If the
   * app already loaded switcher.js by hand, that one is used as-is. */
  function mountSwitcher(target, opts) {
    if (globalThis.GovernedCognitionSwitcher) {
      return Promise.resolve(globalThis.GovernedCognitionSwitcher.mount(target, opts));
    }
    if (!switcherLoad) {
      setLink('gc-switcher-css', 'switcher.css');
      switcherLoad = injectScript('switcher.js');
    }
    return switcherLoad.then(function () {
      if (!globalThis.GovernedCognitionSwitcher) {
        throw new Error('gc: switcher.js loaded but did not register');
      }
      return globalThis.GovernedCognitionSwitcher.mount(target, opts);
    });
  }

  var GC = {
    themes: function () { return roster.themes.slice(); },
    /* shapes(theme) — the offered list for a theme; no arg = the current one */
    shapes: function (theme) {
      return shapesFor(theme || current().theme).slice();
    },
    nativeShape: function (theme) { return roster.nativeShape[theme] || 'rounded'; },
    /* A hard-edged theme has NO round geometry — every radius tier is zero.
     * Both shapes still work and still differ (slanted cuts corners, rounded
     * leaves them square); what changes is that "rounded" means square there,
     * so a picker should say so rather than promise a roundover. */
    isHardEdged: function (theme) { return roster.hardEdged[theme] === true; },
    rosterSource: function () { return roster.source; },
    rosterEvent: ROSTER_EVENT,
    applyEvent: APPLY_EVENT,
    config: cfg,
    apply: apply,
    current: current,
    mountSwitcher: mountSwitcher,
    settingsSource: function () { return settingsSource; },
    themeSource: function () { return themeSource; },
    sourceEvent: SOURCE_EVENT,
    ready: null
  };

  globalThis.GovernedCognition = GC;

  /* Apply the persisted (or default) selection immediately on load, from the
   * seed roster; then reconcile against the real one. */
  if (typeof document !== 'undefined') {
    /* Paint from the local cache first — never wait on the network to render.
     * apply() would PUT this stale value straight back at the origin and
     * clobber the estate selection, so seed `desired` directly instead. */
    desired.theme = read(LS_THEME) || cfg.defaultTheme;
    desired.shape = read(LS_SHAPE) || null;
    render();
    /* Then reconcile: the roster says which themes exist, the settings say
     * which one the estate is on. Both are origin-first with the local value
     * standing if the origin is down. */
    GC.ready = loadRoster().then(function () {
      return loadSettings().then(function (data) {
        settingsSource = data ? 'origin' : 'local';
        return roster;
      });
    });
  } else {
    GC.ready = Promise.resolve(roster);
  }
})();
