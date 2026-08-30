/* Governed Cognition — theme switcher (the standard estate picker).
 *
 * Standardized 2026-08-30: a small gear button that expands a panel. Every
 * app mounts THIS rather than growing its own — one picker, one set of
 * affordances, one keyboard contract across the estate.
 *
 * Keeper-ruled placement, preserved: the shape toggle sits BESIDE the color
 * theme list (inside the expanded panel, side by side — not stacked).
 *
 * The theme list is rendered from the LIVE roster and re-renders when it
 * arrives (loader.js fetches index.json; see its header). A theme added to
 * the repo therefore appears in this menu with no app release.
 *
 * The panel is appended to <body> and positioned from the gear's rect, NOT
 * nested under it. Under slant the picker's natural home — a bar or card —
 * carries a clip-path, and a clip-path clips its descendants: a nested panel
 * is cut away entirely the moment it extends past the bar. (Caught in render,
 * 2026-08-30.) The portal also sidesteps ancestor overflow and z-index wars.
 *
 * Requires loader.js first (uses globalThis.GovernedCognition). Vanilla JS,
 * no build, no CDN; plain <script> or ESM side-effect import both work.
 *
 * Usage:  GovernedCognitionSwitcher.mount('#some-container')
 *         GovernedCognitionSwitcher.mount(el, { align: 'left', label: 'theme' })
 * Returns the root element; call .destroy() on it to unmount cleanly.
 *
 * Styling: switcher.css (token-pure — passes conformance.py css). */
(function () {
  'use strict';

  /* An 8-tooth gear, 24x24, filled with currentColor so it takes the token
   * color of whatever it is mounted into. */
  var GEAR_PATH =
    'M19.14 12.94a7.07 7.07 0 0 0 0-1.88l2.03-1.58a.5.5 0 0 0 .12-.64l-1.92-3.32' +
    'a.5.5 0 0 0-.61-.22l-2.39.96a7.3 7.3 0 0 0-1.63-.94l-.36-2.54a.5.5 0 0 0-.5-.42' +
    'h-3.84a.5.5 0 0 0-.5.42l-.36 2.54c-.59.24-1.13.56-1.63.94l-2.39-.96a.5.5 0 0 0-.61.22' +
    'L2.65 8.84a.5.5 0 0 0 .12.64l2.03 1.58a7.07 7.07 0 0 0 0 1.88l-2.03 1.58' +
    'a.5.5 0 0 0-.12.64l1.92 3.32c.13.22.39.31.61.22l2.39-.96c.5.38 1.04.7 1.63.94' +
    'l.36 2.54c.04.24.25.42.5.42h3.84c.25 0 .46-.18.5-.42l.36-2.54c.59-.24 1.13-.56 1.63-.94' +
    'l2.39.96c.22.09.48 0 .61-.22l1.92-3.32a.5.5 0 0 0-.12-.64l-2.03-1.58z' +
    'M12 15.6A3.6 3.6 0 1 1 12 8.4a3.6 3.6 0 0 1 0 7.2z';

  var SVG_NS = 'http://www.w3.org/2000/svg';
  var seq = 0;

  function gearIcon() {
    var svg = document.createElementNS(SVG_NS, 'svg');
    svg.setAttribute('viewBox', '0 0 24 24');
    svg.setAttribute('aria-hidden', 'true');
    svg.setAttribute('focusable', 'false');
    svg.setAttribute('class', 'gc-switcher-gear-icon');
    var path = document.createElementNS(SVG_NS, 'path');
    path.setAttribute('d', GEAR_PATH);
    path.setAttribute('fill', 'currentColor');
    svg.appendChild(path);
    return svg;
  }

  function mount(target, opts) {
    var GC = globalThis.GovernedCognition;
    if (!GC) throw new Error('gc-switcher: load loader.js before switcher.js');
    var host = typeof target === 'string' ? document.querySelector(target) : target;
    if (!host) throw new Error('gc-switcher: mount target not found');

    opts = opts || {};
    var id = 'gc-switcher-' + (++seq);
    var label = opts.label || 'Theme';

    var root = document.createElement('div');
    root.className = 'gc-switcher';
    if (opts.align === 'left') root.classList.add('gc-switcher-align-left');

    var gear = document.createElement('button');
    gear.type = 'button';
    gear.className = 'gc-switcher-gear';
    gear.setAttribute('aria-haspopup', 'true');
    gear.setAttribute('aria-expanded', 'false');
    gear.setAttribute('aria-controls', id + '-panel');
    gear.setAttribute('aria-label', label);
    gear.title = label;
    gear.appendChild(gearIcon());

    var panel = document.createElement('div');
    panel.className = 'gc-switcher-panel';
    if (opts.align === 'left') panel.classList.add('gc-switcher-panel-align-left');
    panel.id = id + '-panel';
    panel.hidden = true;

    var themeGroup = document.createElement('fieldset');
    themeGroup.className = 'gc-switcher-themes';
    var shapeGroup = document.createElement('fieldset');
    shapeGroup.className = 'gc-switcher-shape';

    panel.appendChild(themeGroup);
    panel.appendChild(shapeGroup); /* beside the color list — ruled */
    root.appendChild(gear);
    host.appendChild(root);
    /* the portal: out of any clipped ancestor, positioned from the gear */
    document.body.appendChild(panel);

    var state = GC.current();

    function option(group, name, value, text, checked, onPick, title) {
      var wrap = document.createElement('label');
      wrap.className = 'gc-switcher-option';
      if (title) wrap.title = title;
      var input = document.createElement('input');
      input.type = 'radio';
      input.name = name;
      input.value = value;
      input.checked = !!checked;
      input.addEventListener('change', function () {
        if (input.checked) onPick(value);
      });
      var span = document.createElement('span');
      span.textContent = text;
      wrap.appendChild(input);
      wrap.appendChild(span);
      group.appendChild(wrap);
      return input;
    }

    function fill(group, legendText, values, selected, name, onPick, describe) {
      group.textContent = '';
      var legend = document.createElement('legend');
      legend.textContent = legendText;
      group.appendChild(legend);
      values.forEach(function (value) {
        var d = describe ? describe(value) : null;
        option(group, name, value, (d && d.label) || value, value === selected,
          onPick, d && d.title);
      });
    }

    /* hard and slanted are a pair — both 'not round', slanted being hard plus
     * the cuts. A hard-edged theme does not offer rounded at all (its rounded
     * and its hard are the same render), so no option needs relabelling any
     * more; the vocabulary itself is honest. Titles say what each one does. */
    var SHAPE_TITLE = {
      rounded: "the theme's own radius scale",
      hard: 'square corners, no cuts',
      slanted: 'square corners with the corner cuts'
    };

    function describeShape(value) {
      return SHAPE_TITLE[value] ? { title: SHAPE_TITLE[value] } : null;
    }

    function renderOptions() {
      state = GC.current();
      fill(themeGroup, 'theme', GC.themes(), state.theme, id + '-theme',
        function (theme) {
          state = Object.assign(state, GC.apply(theme, state.shape));
          renderOptions(); /* a theme's native shape may change what is checked */
        });
      /* the offered list is per-theme, so this must be re-read on theme change */
      fill(shapeGroup, 'shape', GC.shapes(state.theme), state.resolvedShape, id + '-shape',
        function (shape) {
          state.shape = shape;
          GC.apply(state.theme, shape);
        }, describeShape);
    }

    /* --- placement -------------------------------------------------------- */

    /* Fixed to the viewport, anchored under the gear and clamped inside it.
     * The vertical gap is CSS (margin-top on the panel) so it stays a token;
     * only the anchor point is computed here. */
    function place() {
      var r = gear.getBoundingClientRect();
      var margin = 4;
      panel.style.top = r.bottom + 'px';
      panel.style.left = '0px';
      var w = panel.offsetWidth;
      var h = panel.offsetHeight;
      var left = panel.classList.contains('gc-switcher-panel-align-left')
        ? r.left
        : r.right - w;
      left = Math.max(margin, Math.min(left, window.innerWidth - w - margin));
      panel.style.left = left + 'px';
      var overflow = (r.bottom + h + margin) - window.innerHeight;
      if (overflow > 0) {
        panel.style.top = Math.max(margin, r.bottom - overflow) + 'px';
      }
    }

    /* --- open / close ----------------------------------------------------- */

    function isOpen() { return !panel.hidden; }

    function open() {
      if (isOpen()) return;
      panel.hidden = false;
      place();
      gear.setAttribute('aria-expanded', 'true');
      document.addEventListener('pointerdown', onOutside, true);
      document.addEventListener('keydown', onKeydown, true);
      window.addEventListener('resize', place, true);
      window.addEventListener('scroll', place, true);
      var first = panel.querySelector('input:checked') || panel.querySelector('input');
      if (first) first.focus();
    }

    function close(refocus) {
      if (!isOpen()) return;
      panel.hidden = true;
      gear.setAttribute('aria-expanded', 'false');
      document.removeEventListener('pointerdown', onOutside, true);
      document.removeEventListener('keydown', onKeydown, true);
      window.removeEventListener('resize', place, true);
      window.removeEventListener('scroll', place, true);
      if (refocus) gear.focus();
    }

    /* the panel is no longer a descendant of root, so "outside" is both */
    function onOutside(ev) {
      if (!root.contains(ev.target) && !panel.contains(ev.target)) close(false);
    }

    function onKeydown(ev) {
      if (ev.key === 'Escape') {
        ev.stopPropagation();
        close(true);
      }
    }

    gear.addEventListener('click', function () {
      if (isOpen()) close(true); else open();
    });

    /* The roster arrives after first paint (origin fetch). Re-render so a
     * theme published since this app was built shows up in the list. */
    function onExternalChange() {
      renderOptions();
      if (isOpen()) place();
    }
    /* roster: a theme published since this app was built. apply: a selection
     * this picker did not make (programmatic, or another mounted picker). */
    document.addEventListener(GC.rosterEvent || 'gc:roster', onExternalChange);
    document.addEventListener(GC.applyEvent || 'gc:apply', onExternalChange);

    renderOptions();

    root.destroy = function () {
      close(false);
      document.removeEventListener(GC.rosterEvent || 'gc:roster', onExternalChange);
      document.removeEventListener(GC.applyEvent || 'gc:apply', onExternalChange);
      if (panel.parentNode) panel.parentNode.removeChild(panel);
      if (root.parentNode) root.parentNode.removeChild(root);
    };
    return root;
  }

  globalThis.GovernedCognitionSwitcher = { mount: mount };
})();
