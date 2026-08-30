/* Governed Cognition — theme switcher (small mountable UI).
 *
 * Keeper-ruled placement: the shape toggle sits BESIDE the color theme list.
 * Requires loader.js first (uses globalThis.GovernedCognition). Vanilla JS,
 * no build, no CDN; plain <script> or ESM side-effect import both work.
 *
 * Usage:  GovernedCognitionSwitcher.mount('#some-container')
 * Styling: switcher.css (token-pure — passes conformance.py css). */
(function () {
  'use strict';

  function mount(target) {
    var GC = globalThis.GovernedCognition;
    if (!GC) throw new Error('gc-switcher: load loader.js before switcher.js');
    var host = typeof target === 'string' ? document.querySelector(target) : target;
    if (!host) throw new Error('gc-switcher: mount target not found');

    var root = document.createElement('div');
    root.className = 'gc-switcher';

    var themeGroup = document.createElement('fieldset');
    themeGroup.className = 'gc-switcher-themes';
    var themeLegend = document.createElement('legend');
    themeLegend.textContent = 'theme';
    themeGroup.appendChild(themeLegend);

    var shapeGroup = document.createElement('fieldset');
    shapeGroup.className = 'gc-switcher-shape';
    var shapeLegend = document.createElement('legend');
    shapeLegend.textContent = 'shape';
    shapeGroup.appendChild(shapeLegend);

    var state = GC.current();

    function radio(group, name, value, label, checked, onPick) {
      var wrap = document.createElement('label');
      wrap.className = 'gc-switcher-option';
      var input = document.createElement('input');
      input.type = 'radio';
      input.name = name;
      input.value = value;
      input.checked = !!checked;
      input.addEventListener('change', function () {
        if (input.checked) onPick(value);
      });
      var text = document.createElement('span');
      text.textContent = label;
      wrap.appendChild(input);
      wrap.appendChild(text);
      group.appendChild(wrap);
      return input;
    }

    GC.themes().forEach(function (name) {
      radio(themeGroup, 'gc-theme-pick', name, name, name === state.theme, function (theme) {
        state = Object.assign(state, GC.apply(theme, state.shape));
      });
    });

    GC.shapes().forEach(function (name) {
      radio(shapeGroup, 'gc-shape-pick', name, name, name === state.resolvedShape, function (shape) {
        state.shape = shape;
        GC.apply(state.theme, shape);
      });
    });

    root.appendChild(themeGroup);
    root.appendChild(shapeGroup); /* beside the color list — ruled */
    host.appendChild(root);
    return root;
  }

  globalThis.GovernedCognitionSwitcher = { mount: mount };
})();
