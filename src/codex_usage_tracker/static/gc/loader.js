(function() {
	//#region src/loader.ts
	var SEED_THEMES = [
		"governed-cognition",
		"neo-andean",
		"johannah"
	];
	var SEED_NATIVE_SHAPE = {
		"governed-cognition": "rounded",
		"neo-andean": "hard",
		johannah: "rounded"
	};
	var SEED_SHAPES = [
		"rounded",
		"hard",
		"slanted"
	];
	var SEED_SHAPE_CSS = {
		rounded: null,
		hard: "shape-hard.css",
		slanted: "shape-slant.css"
	};
	var LS_THEME = "gc.theme";
	var LS_SHAPE = "gc.shape";
	var LS_GLASS = "gc.glass";
	var LS_GLASS_OPACITY = "gc.glassOpacity";
	var GLASS_OPACITY_DEFAULT = .35;
	var ROSTER_EVENT = "gc:roster";
	var APPLY_EVENT = "gc:apply";
	var SOURCE_EVENT = "gc:source";
	var GLASS_EVENT = "gc:glass";
	function defaultOrigin() {
		return "http://" + (typeof location !== "undefined" && location.hostname || "127.0.0.1") + ":7312";
	}
	var script = typeof document !== "undefined" ? document.currentScript : null;
	var attr = (name) => script && "getAttribute" in script ? script.getAttribute(name) : null;
	var preset = typeof globalThis !== "undefined" && globalThis.GC_CONFIG || {};
	var cfg = {
		origin: preset.origin || attr("data-gc-origin") || defaultOrigin(),
		fallback: preset.fallback || attr("data-gc-fallback") || null,
		defaultTheme: preset.defaultTheme || attr("data-gc-default-theme") || "governed-cognition",
		settings: (preset.settings !== void 0 ? preset.settings : attr("data-gc-settings")) !== "local" && preset.settings !== false
	};
	var roster = {
		themes: SEED_THEMES.slice(),
		background: {},
		nativeShape: { ...SEED_NATIVE_SHAPE },
		hardEdged: {},
		shapes: SEED_SHAPES.slice(),
		shapeCss: { ...SEED_SHAPE_CSS },
		themeShapes: {},
		source: "seed"
	};
	var desired = {
		theme: cfg.defaultTheme,
		shape: null,
		glass: false,
		glassOpacity: GLASS_OPACITY_DEFAULT
	};
	function clampOpacity(value) {
		const n = typeof value === "number" ? value : parseFloat(String(value));
		if (!isFinite(n)) return GLASS_OPACITY_DEFAULT;
		return Math.max(0, Math.min(1, n));
	}
	function joinUrl(base, file) {
		return base.replace(/\/+$/, "") + "/" + file;
	}
	function absUrl(url) {
		try {
			return new URL(url, document.baseURI).href;
		} catch {
			return url;
		}
	}
	function store(key, value) {
		try {
			if (value === null) localStorage.removeItem(key);
			else localStorage.setItem(key, value);
		} catch {}
	}
	function read(key) {
		try {
			return localStorage.getItem(key);
		} catch {
			return null;
		}
	}
	var LOAD_TIMEOUT_MS = 4e3;
	var sourceIdx = {
		theme: 0,
		shape: 0
	};
	var chainGen = {};
	function candidates(file, prefer) {
		let list = [{
			src: 0,
			url: absUrl(joinUrl(cfg.origin, file))
		}];
		if (cfg.fallback) list.push({
			src: 1,
			url: absUrl(joinUrl(cfg.fallback, file))
		});
		if (prefer === 1 && list.length === 2) list = [list[1], list[0]];
		return list;
	}
	function setLink(id, file, afterId, prefer, done) {
		const gen = chainGen[id] = (chainGen[id] || 0) + 1;
		const current = () => chainGen[id] === gen;
		let link = document.getElementById(id);
		if (!file) {
			if (link) link.remove();
			if (done) done(-1);
			return;
		}
		if (!link) {
			link = document.createElement("link");
			link.id = id;
			link.rel = "stylesheet";
			const anchor = afterId ? document.getElementById(afterId) : null;
			if (anchor && anchor.nextSibling && anchor.parentNode) anchor.parentNode.insertBefore(link, anchor.nextSibling);
			else (document.head || document.documentElement).appendChild(link);
		}
		const urls = candidates(file, prefer);
		let i = 0;
		function attempt() {
			if (!current()) return;
			if (i >= urls.length) {
				if (done) done(-1);
				return;
			}
			const cand = urls[i];
			if (link.href === cand.url && link.sheet) {
				if (done) done(cand.src);
				return;
			}
			let settled = false;
			const timer = setTimeout(miss, LOAD_TIMEOUT_MS);
			function miss() {
				if (settled) return;
				settled = true;
				clearTimeout(timer);
				if (!current()) return;
				i += 1;
				attempt();
			}
			link.onerror = miss;
			link.onload = () => {
				if (settled) return;
				settled = true;
				clearTimeout(timer);
				if (!current()) return;
				if (done) done(cand.src);
			};
			link.href = cand.url;
		}
		attempt();
	}
	function publishBackground(theme) {
		const el = document.documentElement;
		const bg = roster.background[theme];
		if (!bg) {
			el.style.removeProperty("--gc-bg-image");
			el.style.removeProperty("--gc-bg-size");
			el.style.removeProperty("--gc-bg-repeat");
			return;
		}
		const base = sourceIdx.theme === 1 && cfg.fallback ? cfg.fallback : cfg.origin;
		el.style.setProperty("--gc-bg-image", "url(\"" + absUrl(joinUrl(base, bg.file)) + "\")");
		el.style.setProperty("--gc-bg-size", bg.size);
		el.style.setProperty("--gc-bg-repeat", bg.repeat);
	}
	var themeSource = null;
	function setSource(idx) {
		const next = idx === 0 ? "origin" : idx === 1 ? "fallback" : "none";
		if (next === themeSource) return;
		themeSource = next;
		if (document.documentElement) document.documentElement.setAttribute("data-gc-theme-source", next);
		document.dispatchEvent(new CustomEvent(SOURCE_EVENT, { detail: { source: next } }));
	}
	function knownTheme(theme) {
		return roster.themes.indexOf(theme) !== -1;
	}
	function shapesFor(theme) {
		const listed = roster.themeShapes[theme];
		if (listed && listed.length) return listed;
		if (roster.hardEdged[theme] === true) return roster.shapes.filter((s) => s !== "rounded");
		return roster.shapes.slice();
	}
	function resolveShape(theme, shape) {
		if (shape === "native") shape = null;
		const offered = shapesFor(theme);
		if (shape && offered.indexOf(shape) === -1) shape = null;
		return shape || roster.nativeShape[theme] || offered[0] || "rounded";
	}
	function render() {
		const theme = knownTheme(desired.theme) ? desired.theme : cfg.defaultTheme;
		const resolved = resolveShape(theme, desired.shape);
		setLink("gc-theme", "theme-" + theme + ".css", null, sourceIdx.theme, (idx) => {
			if (idx >= 0) sourceIdx.theme = idx;
			setSource(idx);
			publishBackground(theme);
			setLink("gc-shape", roster.shapeCss[resolved] || null, "gc-theme", sourceIdx.theme, (sidx) => {
				if (sidx >= 0) sourceIdx.shape = sidx;
			});
		});
		if (document.documentElement) {
			document.documentElement.setAttribute("data-gc-theme", theme);
			document.documentElement.setAttribute("data-gc-shape", resolved);
			publishBackground(theme);
		}
		document.dispatchEvent(new CustomEvent(APPLY_EVENT, { detail: {
			theme,
			shape: resolved
		} }));
		return {
			theme,
			shape: resolved
		};
	}
	function apply(theme, shape) {
		if (typeof theme !== "string" || !theme) theme = cfg.defaultTheme;
		desired.theme = theme;
		desired.shape = shape || null;
		store(LS_THEME, theme);
		store(LS_SHAPE, shape || null);
		publishSettings();
		return render();
	}
	function reducedTransparency() {
		try {
			return typeof matchMedia === "function" && matchMedia("(prefers-reduced-transparency: reduce)").matches;
		} catch {
			return false;
		}
	}
	var glassAnnounced = null;
	function renderGlass() {
		const effective = desired.glass && !reducedTransparency();
		if (document.documentElement) {
			document.documentElement.setAttribute("data-gc-glass", effective ? "on" : "off");
			document.documentElement.style.setProperty("--gc-glass-opacity", String(desired.glassOpacity));
		}
		setLink("gc-glass-css", desired.glass ? "glass.css" : null, "gc-shape");
		const announce = (desired.glass ? "on" : "off") + "/" + (effective ? "on" : "off") + "/" + desired.glassOpacity;
		if (announce === glassAnnounced) return;
		glassAnnounced = announce;
		document.dispatchEvent(new CustomEvent(GLASS_EVENT, { detail: {
			glass: desired.glass,
			effective,
			opacity: desired.glassOpacity
		} }));
	}
	function setGlass(on) {
		desired.glass = on === true;
		store(LS_GLASS, desired.glass ? "on" : "off");
		publishSettings();
		renderGlass();
		return desired.glass;
	}
	function setGlassOpacity(value) {
		desired.glassOpacity = clampOpacity(value);
		store(LS_GLASS_OPACITY, String(desired.glassOpacity));
		publishSettings();
		renderGlass();
		return desired.glassOpacity;
	}
	function publishSettings() {
		if (!cfg.settings || typeof fetch !== "function") return;
		try {
			fetch(joinUrl(cfg.origin, "settings"), {
				method: "PUT",
				headers: { "Content-Type": "application/json" },
				body: JSON.stringify({
					theme: desired.theme,
					shape: desired.shape,
					glass: desired.glass,
					glassOpacity: desired.glassOpacity
				})
			}).catch(() => {});
		} catch {}
	}
	function loadSettings() {
		if (!cfg.settings) return Promise.resolve(null);
		return fetchJson(joinUrl(cfg.origin, "settings")).then((data) => {
			if (!data || typeof data !== "object") return null;
			const theme = typeof data.theme === "string" ? data.theme : desired.theme;
			const shape = data.shape === null || typeof data.shape === "string" ? data.shape : desired.shape;
			const glass = typeof data.glass === "boolean" ? data.glass : desired.glass;
			const opacity = data.glassOpacity === void 0 ? desired.glassOpacity : clampOpacity(data.glassOpacity);
			if (glass !== desired.glass || opacity !== desired.glassOpacity) {
				desired.glass = glass;
				desired.glassOpacity = opacity;
				store(LS_GLASS, glass ? "on" : "off");
				store(LS_GLASS_OPACITY, String(opacity));
				renderGlass();
			}
			if (theme === desired.theme && shape === desired.shape) return data;
			desired.theme = theme;
			desired.shape = shape || null;
			store(LS_THEME, theme);
			store(LS_SHAPE, shape || null);
			render();
			return data;
		}).catch(() => null);
	}
	function current() {
		const rendered = knownTheme(desired.theme) ? desired.theme : cfg.defaultTheme;
		return {
			theme: rendered,
			requestedTheme: desired.theme,
			shape: desired.shape,
			resolvedShape: resolveShape(rendered, desired.shape)
		};
	}
	function parseRoster(data) {
		if (!data || !Array.isArray(data.themes) || !data.themes.length) return null;
		const names = [];
		const native = {};
		const hard = {};
		const themeShapes = {};
		const background = {};
		for (const entry of data.themes) {
			const name = entry && entry.name;
			if (typeof name !== "string" || !name) continue;
			names.push(name);
			native[name] = typeof entry.native_shape === "string" ? entry.native_shape : "rounded";
			themeShapes[name] = Array.isArray(entry.shapes) && entry.shapes.length ? entry.shapes.slice() : null;
			hard[name] = entry.hard_edged === true;
			const bg = entry.assets && entry.assets.background;
			background[name] = bg && typeof bg.file === "string" ? {
				file: bg.file,
				size: typeof bg.size === "string" ? bg.size : "cover",
				repeat: typeof bg.repeat === "string" ? bg.repeat : "no-repeat"
			} : null;
		}
		let shapes = [];
		let shapeCss = {};
		if (Array.isArray(data.shapes)) for (const s of data.shapes) {
			if (!s || typeof s.name !== "string" || !s.name) continue;
			shapes.push(s.name);
			shapeCss[s.name] = typeof s.css === "string" ? s.css : null;
		}
		if (!shapes.length) {
			shapes = SEED_SHAPES.slice();
			shapeCss = { ...SEED_SHAPE_CSS };
		}
		return names.length ? {
			themes: names,
			nativeShape: native,
			hardEdged: hard,
			themeShapes,
			shapes,
			shapeCss,
			background
		} : null;
	}
	function adoptRoster(parsed, source) {
		const before = roster.themes.join(" ");
		roster = {
			...parsed,
			source
		};
		render();
		document.dispatchEvent(new CustomEvent(ROSTER_EVENT, { detail: {
			themes: roster.themes.slice(),
			source,
			changed: before !== roster.themes.join(" ")
		} }));
	}
	function fetchJson(url) {
		if (typeof fetch !== "function") return Promise.reject(/* @__PURE__ */ new Error("no fetch"));
		return fetch(url, { cache: "no-cache" }).then((res) => {
			if (!res.ok) throw new Error("HTTP " + res.status);
			return res.json();
		});
	}
	function loadRoster() {
		return fetchJson(joinUrl(cfg.origin, "index.json")).then((data) => ({
			data,
			source: "origin"
		})).catch(() => {
			if (!cfg.fallback) throw new Error("no fallback configured");
			return fetchJson(joinUrl(cfg.fallback, "index.json")).then((data) => ({
				data,
				source: "fallback"
			}));
		}).then((hit) => {
			const parsed = parseRoster(hit.data);
			if (parsed) adoptRoster(parsed, hit.source);
			return roster;
		}).catch(() => roster);
	}
	var switcherLoad = null;
	var settingsSource = "local";
	function injectScript(file) {
		return new Promise((resolve, reject) => {
			function attempt(base, onFail) {
				const el = document.createElement("script");
				el.src = joinUrl(base, file);
				el.async = false;
				el.onload = () => resolve();
				el.onerror = onFail;
				(document.head || document.documentElement).appendChild(el);
			}
			attempt(cfg.origin, () => {
				if (!cfg.fallback) {
					reject(/* @__PURE__ */ new Error("gc: cannot load " + file + " (no fallback configured)"));
					return;
				}
				attempt(cfg.fallback, () => reject(/* @__PURE__ */ new Error("gc: cannot load " + file)));
			});
		});
	}
	function mountSwitcher(target, opts) {
		if (globalThis.GovernedCognitionSwitcher) return Promise.resolve(globalThis.GovernedCognitionSwitcher.mount(target, opts));
		if (!switcherLoad) {
			setLink("gc-switcher-css", "switcher.css");
			switcherLoad = injectScript("switcher.js");
		}
		return switcherLoad.then(() => {
			if (!globalThis.GovernedCognitionSwitcher) throw new Error("gc: switcher.js loaded but did not register");
			return globalThis.GovernedCognitionSwitcher.mount(target, opts);
		});
	}
	var GC = {
		themes: () => roster.themes.slice(),
		shapes: (theme) => shapesFor(theme || current().theme).slice(),
		nativeShape: (theme) => roster.nativeShape[theme] || "rounded",
		isHardEdged: (theme) => roster.hardEdged[theme] === true,
		rosterSource: () => roster.source,
		rosterEvent: ROSTER_EVENT,
		applyEvent: APPLY_EVENT,
		sourceEvent: SOURCE_EVENT,
		glassEvent: GLASS_EVENT,
		config: cfg,
		apply,
		current,
		mountSwitcher,
		settingsSource: () => settingsSource,
		themeSource: () => themeSource,
		glass: () => desired.glass,
		glassEffective: () => desired.glass && !reducedTransparency(),
		setGlass,
		glassOpacity: () => desired.glassOpacity,
		setGlassOpacity,
		ready: null
	};
	globalThis.GovernedCognition = GC;
	if (typeof document !== "undefined") {
		desired.theme = read(LS_THEME) || cfg.defaultTheme;
		desired.shape = read(LS_SHAPE) || null;
		desired.glass = read(LS_GLASS) === "on";
		const storedOpacity = read(LS_GLASS_OPACITY);
		if (storedOpacity !== null) desired.glassOpacity = clampOpacity(storedOpacity);
		render();
		renderGlass();
		try {
			if (typeof matchMedia === "function") {
				const mq = matchMedia("(prefers-reduced-transparency: reduce)");
				if (mq && typeof mq.addEventListener === "function") mq.addEventListener("change", renderGlass);
			}
		} catch {}
		GC.ready = loadRoster().then(() => loadSettings().then((data) => {
			settingsSource = data ? "origin" : "local";
			return roster;
		}));
	} else GC.ready = Promise.resolve(roster);
	//#endregion
})();
