(function() {
	//#region src/switcher.ts
	var GEAR_PATH = "M19.14 12.94a7.07 7.07 0 0 0 0-1.88l2.03-1.58a.5.5 0 0 0 .12-.64l-1.92-3.32a.5.5 0 0 0-.61-.22l-2.39.96a7.3 7.3 0 0 0-1.63-.94l-.36-2.54a.5.5 0 0 0-.5-.42h-3.84a.5.5 0 0 0-.5.42l-.36 2.54c-.59.24-1.13.56-1.63.94l-2.39-.96a.5.5 0 0 0-.61.22L2.65 8.84a.5.5 0 0 0 .12.64l2.03 1.58a7.07 7.07 0 0 0 0 1.88l-2.03 1.58a.5.5 0 0 0-.12.64l1.92 3.32c.13.22.39.31.61.22l2.39-.96c.5.38 1.04.7 1.63.94l.36 2.54c.04.24.25.42.5.42h3.84c.25 0 .46-.18.5-.42l.36-2.54c.59-.24 1.13-.56 1.63-.94l2.39.96c.22.09.48 0 .61-.22l1.92-3.32a.5.5 0 0 0-.12-.64l-2.03-1.58zM12 15.6A3.6 3.6 0 1 1 12 8.4a3.6 3.6 0 0 1 0 7.2z";
	var SVG_NS = "http://www.w3.org/2000/svg";
	var seq = 0;
	function gearIcon() {
		const svg = document.createElementNS(SVG_NS, "svg");
		svg.setAttribute("viewBox", "0 0 24 24");
		svg.setAttribute("aria-hidden", "true");
		svg.setAttribute("focusable", "false");
		svg.setAttribute("class", "gc-switcher-gear-icon");
		const path = document.createElementNS(SVG_NS, "path");
		path.setAttribute("d", GEAR_PATH);
		path.setAttribute("fill", "currentColor");
		svg.appendChild(path);
		return svg;
	}
	function mount(target, opts) {
		const GC = globalThis.GovernedCognition;
		if (!GC) throw new Error("gc-switcher: load loader.js before switcher.js");
		const host = typeof target === "string" ? document.querySelector(target) : target;
		if (!host) throw new Error("gc-switcher: mount target not found");
		const o = opts || {};
		const id = "gc-switcher-" + ++seq;
		const label = o.label || "Theme";
		const root = document.createElement("div");
		root.className = "gc-switcher";
		if (o.align === "left") root.classList.add("gc-switcher-align-left");
		const gear = document.createElement("button");
		gear.type = "button";
		gear.className = "gc-switcher-gear";
		gear.setAttribute("aria-haspopup", "true");
		gear.setAttribute("aria-expanded", "false");
		gear.setAttribute("aria-controls", id + "-panel");
		gear.setAttribute("aria-label", label);
		gear.title = label;
		gear.appendChild(gearIcon());
		const panel = document.createElement("div");
		panel.className = "gc-switcher-panel";
		if (o.align === "left") panel.classList.add("gc-switcher-panel-align-left");
		panel.id = id + "-panel";
		panel.hidden = true;
		const themeGroup = document.createElement("fieldset");
		themeGroup.className = "gc-switcher-themes";
		const shapeGroup = document.createElement("fieldset");
		shapeGroup.className = "gc-switcher-shape";
		panel.appendChild(themeGroup);
		panel.appendChild(shapeGroup);
		root.appendChild(gear);
		host.appendChild(root);
		document.body.appendChild(panel);
		let state = GC.current();
		function option(group, name, value, text, checked, onPick, title) {
			const wrap = document.createElement("label");
			wrap.className = "gc-switcher-option";
			if (title) wrap.title = title;
			const input = document.createElement("input");
			input.type = "radio";
			input.name = name;
			input.value = value;
			input.checked = checked;
			input.addEventListener("change", () => {
				if (input.checked) onPick(value);
			});
			const span = document.createElement("span");
			span.textContent = text;
			wrap.appendChild(input);
			wrap.appendChild(span);
			group.appendChild(wrap);
			return input;
		}
		function fill(group, legendText, values, selected, name, onPick, describe) {
			group.textContent = "";
			const legend = document.createElement("legend");
			legend.textContent = legendText;
			group.appendChild(legend);
			values.forEach((value) => {
				const d = describe ? describe(value) : null;
				option(group, name, value, d && d.label || value, value === selected, onPick, d?.title);
			});
		}
		const SHAPE_TITLE = {
			rounded: "the theme's own radius scale",
			hard: "square corners, no cuts",
			slanted: "square corners with the corner cuts"
		};
		function describeShape(value) {
			const title = SHAPE_TITLE[value];
			return title ? { title } : null;
		}
		function renderOptions() {
			state = GC.current();
			fill(themeGroup, "theme", GC.themes(), state.theme, id + "-theme", (theme) => {
				state = {
					...state,
					...GC.apply(theme, state.shape)
				};
				renderOptions();
			});
			fill(shapeGroup, "shape", GC.shapes(state.theme), state.resolvedShape, id + "-shape", (shape) => {
				state.shape = shape;
				GC.apply(state.theme, shape);
			}, describeShape);
		}
		function place() {
			const r = gear.getBoundingClientRect();
			const margin = 4;
			panel.style.top = r.bottom + "px";
			panel.style.left = "0px";
			const w = panel.offsetWidth;
			const h = panel.offsetHeight;
			let left = panel.classList.contains("gc-switcher-panel-align-left") ? r.left : r.right - w;
			left = Math.max(margin, Math.min(left, window.innerWidth - w - margin));
			panel.style.left = left + "px";
			const overflow = r.bottom + h + margin - window.innerHeight;
			if (overflow > 0) panel.style.top = Math.max(margin, r.bottom - overflow) + "px";
		}
		const isOpen = () => !panel.hidden;
		function open() {
			if (isOpen()) return;
			panel.hidden = false;
			place();
			gear.setAttribute("aria-expanded", "true");
			document.addEventListener("pointerdown", onOutside, true);
			document.addEventListener("keydown", onKeydown, true);
			window.addEventListener("resize", place, true);
			window.addEventListener("scroll", place, true);
			const first = panel.querySelector("input:checked") || panel.querySelector("input");
			if (first) first.focus();
		}
		function close(refocus) {
			if (!isOpen()) return;
			panel.hidden = true;
			gear.setAttribute("aria-expanded", "false");
			document.removeEventListener("pointerdown", onOutside, true);
			document.removeEventListener("keydown", onKeydown, true);
			window.removeEventListener("resize", place, true);
			window.removeEventListener("scroll", place, true);
			if (refocus) gear.focus();
		}
		function onOutside(ev) {
			const t = ev.target;
			if (t && !root.contains(t) && !panel.contains(t)) close(false);
		}
		function onKeydown(ev) {
			if (ev.key === "Escape") {
				ev.stopPropagation();
				close(true);
			}
		}
		gear.addEventListener("click", () => {
			if (isOpen()) close(true);
			else open();
		});
		function onExternalChange() {
			renderOptions();
			if (isOpen()) place();
		}
		document.addEventListener(GC.rosterEvent || "gc:roster", onExternalChange);
		document.addEventListener(GC.applyEvent || "gc:apply", onExternalChange);
		renderOptions();
		root.destroy = () => {
			close(false);
			document.removeEventListener(GC.rosterEvent || "gc:roster", onExternalChange);
			document.removeEventListener(GC.applyEvent || "gc:apply", onExternalChange);
			if (panel.parentNode) panel.parentNode.removeChild(panel);
			if (root.parentNode) root.parentNode.removeChild(root);
		};
		return root;
	}
	globalThis.GovernedCognitionSwitcher = { mount };
	//#endregion
})();
