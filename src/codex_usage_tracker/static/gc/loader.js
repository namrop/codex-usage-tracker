(function() {
	//#region src/glass.ts
	var SVG_NS = "http://www.w3.org/2000/svg";
	var LAYER_CLASS = "gc-glass-layer";
	var LENS_CLASS = "gc-glass-lens";
	var CLIPPED_ATTR = "data-gc-clipped";
	var HOST_ATTR = "data-gc-glass-host";
	var KNOB_DEFAULTS = {
		displace: 48,
		aberration: 2
	};
	function readKnobs() {
		try {
			const cs = getComputedStyle(document.documentElement);
			const num = (name, fallback) => {
				const v = parseFloat(cs.getPropertyValue(name));
				return Number.isFinite(v) ? v : fallback;
			};
			return {
				displace: num("--gc-glass-displace", KNOB_DEFAULTS.displace),
				aberration: num("--gc-glass-aberration", KNOB_DEFAULTS.aberration)
			};
		} catch {
			return { ...KNOB_DEFAULTS };
		}
	}
	function lensMarkup(id, k) {
		const scale = -k.displace;
		const a = k.aberration;
		const stop = Math.max(30, 80 - a * 2);
		return "<defs><radialGradient id=\"" + id + "-edge-mask\" cx=\"50%\" cy=\"50%\" r=\"50%\"><stop offset=\"0%\" stop-color=\"black\" stop-opacity=\"0\"/><stop offset=\"" + stop + "%\" stop-color=\"black\" stop-opacity=\"0\"/><stop offset=\"100%\" stop-color=\"white\" stop-opacity=\"1\"/></radialGradient><filter id=\"" + id + "\" x=\"-35%\" y=\"-35%\" width=\"170%\" height=\"170%\" color-interpolation-filters=\"sRGB\"><feImage x=\"0\" y=\"0\" width=\"100%\" height=\"100%\" result=\"DISPLACEMENT_MAP\" href=\"data:image/jpeg;base64,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cUAUAkEkziomgGgkoxZGgxZFQFQlYnQHRdPfj/10KCSCKESCNiVkViPSLpD0h6I0Q0I0A2IoBWBIJIBKBIJoJIJ2R2J0JWBUJ0JUB0XTv479dFZDYiglYigkhEgjZFQjRFQjRFQjQigFYigHYigmgEgmglYlYnQlQlYlQHQlQnQ9P/kf1yVkNiNCNkNiVENiNiViNEViNkVCVgKCViViViSCViSCVgdCViVCViVCdgVCVCdD1D+U/XBWQ2I0I2Q2JUQ2I0JWQ0I2JUQ2JUI2JUI2J0JWJWJWA2R0BWJ0I2JUJ2BUJUJ0P//EABkQAQEBAQEBAAAAAAAAAAAAAAECABEDEP/aAAgBAQABAgB1atWrVq1atWrVq1atWrVq1atWrVq1atWrVq+OrVq1atWrVq1atWrVq1atWrVq1atWrVq1atXxVppppppdWrVq1atWrVq1NNNNNNNNNNNPVWmmmmms6tWrVq1atWpppppppppppppp6q0000uc51atWrVq1ammmmmmmmmmmmmt1Vpppc5znVq1atWrVqaaaaaaaaaaaaaeqtNLnOc51atWrVq1ammmmmmmmmmmmmnqrS5znOc6tWrVq16222mmmmmmlVppp6tKuc5znOrVq1a9TbbbbTTTTTSq000qtLnOc5zq1atWrW0222200000qqqtKqrnOc5zq1atTbbbbbbbbTTTSqqqqqq5znOc6tTTTbbbbbbbbTTTSqqqqrlVznOctNNNtttttttttNNNNKqqqrqznKqrTTTTbbbbbbbbbTTTSqqqqrqznOc5aaaabbbbbbbbbaaaaVVVVVdWc5znVq1NNttttttttttNNKqqqqudWc5znVq16tbbbbbbbbbbTTSqqqq5XVnOc6tWrVrb1tttttttttNNKqqqqrWrK5VWmmm2230bbbbbbaaaXOc5zlVa1KuVVppptttt9G22222mmlzlVznK6tWVVWmmmm2222222222mlznOc5znLWppVVWmmm22222229bTWrOc5znOcq1qaaVpWmm222222229erVqznOc5znKtatStK0rTbTTbbbberXr1as5znOc5aVpppppWlabaabbbb1ta9WrVnOc5znU0rTTTTTTTTTbTTbbbTWvVq1as5znOdTTStNNNNNNNNNtNNtttN6tWvVq1ZznOrU00rTTTTTTTTTTTTTbTWvVq1atWrOc6tTTTStNNNNNNNNNNtNNtNa9WrVq1Z1Z1NNNNNK1q1NNNNNNNNNNNtNatWrVq1atWrU00000rWrVq1atWrVq1alaaa1atWrVq1NNNammmmla1atWrVq1aterVq16tWrVnVqa1NK1qaaaVX/xAAWEAADAAAAAAAAAAAAAAAAAAAhgJD/2gAIAQEAAz8AaExf/8QAGhEBAQEBAQEBAAAAAAAAAAAAAQISEQADEP/aAAgBAgEBAgDx48ePHjx48ePHjx48ePHjx48ePHjx48ePHj86IiIiIiInjx48ePHjx48IiIiIj0oooooooooRERER73ve60UUUUUUVrWiiiiiihERERER73ve97ooooorRWiiiiihKERERER73ve973RRRRWtFFFFFFCIiIiIiPe973ve60UUVrRRRRRRQiIlCIiI973ve973pRRWiiiiiiiiiiiiiiihEe973ve973RRWtFFFFFFFFFFFFFFFFFFa13ve973WitaKKKKKKKKKKKKKKKKKK1rWtd1rutFa1oooooooooooosssooorWta1rWta1rRRRRRRRRRRZZZZZZZZZWta1rWta1rRRRRRRRRZZZZZZZZZZZZe9a1rWta1rWitaKLLLLLLLLLLLLLLLLL3rWta1rWtFbLLLLLLLLLLLLLLLLLLLL3vWta1rWita1ssssssss+hZZZZZZZZe961rWta0Vre97LLLLLLLLLLLPoWWWWWXrWta1oorWta3ssss+hZZZZ9Cyyyyyyyyiita1orWta1ve9llllllllllllllllFFa0VorWta1ve9llllllllllllllllllFFFaK1rWta1rWiyyyyyyyyyyyyiiiiiiitFFa1rWta1oosoosssssoooosoooorRRRWta1rWta0UUUUUWUUUUUUUUUUUVoooorWta1rWtaKKKKKKmiiiiiiiiiiiiiiitd73ve61oSiiipoqaKKKKKKKKKK0UUUVrve973vREREZoSihEooooorRRRRWtd73ve9EREREREoSiiiiitFllllla73ve9ERERERESiiiiiitH0PoWWWWVrXe96IiIiMoiJRRRRRRWjwlFFllllFFd6IiIiIlCUUUUUUUUUePHjx48ePCIiIiIiIiUUUUUUUUUUUePHjx48ePHjx48ePHjx48IiUUUUUUJRRRX//xAAWEQADAAAAAAAAAAAAAAAAAAABYJD/2gAIAQIBAz8AtEV7/8QAFxEBAQEBAAAAAAAAAAAAAAAAAAECEP/aAAgBAwEBAgCtNNNNNNNNNNNNNNNNNNNNNNNNNNNNNcrTTTTTTTTTTTTTTTTTTTTTTTTTTTTTXKrTTTTTTTU000000000000000000001FVpppppqampqaaaaaaaaaaaaaaaaaaaa5Vaaaaampqampqammmmmmmmmmmlaaaaaaiq0001NTU1NTU1NTTTTTTTTTTSqqtNNNcqtNNSyzU1LNTU1NTTTTTTTTTSqqq001ytNLLLLNTU1NTU1NTbbbTTTTTSqqq001ytNLLLLLNTU1NTU3NttttNNNNNKqq001KrSyyyyyzU1NTU3Nzc02220000qqqqrSqqyyyyyzU1NTU3Nzc3NttttNNNKqqqqqqssssss1NTU3Nzc3NzbbbbTTTSqqqqqqrLLLLLNTU1Nzc3Nzc22220000qqqqqqqqssss1NTU3Nzc3NzbbbbbTTSqqqqqqqqqqzU1NTc3Nzc3Nzbc22000qqqqqqqqqqqtTU3Nzc3Nzc3NtzbTTSqqqqrKqqqqqtNNzc23Nzc3Nzc3NTU1KqqqrKqqqqqtNNNNttzc3Nzc3NzU1NLLLLLKqqqqqqqq0022223Nzc3NzU1NSyyyyyyqqqqqqqrTTbbbbc3Nzc3NTU1LLLLLLKsqqqqqqrTTTTbbbc3Nzc1NTUsssssssqqqqqqrTTTTTbbbTc3NTU1NTUsssssqqqqqqqq0000222023NTU1NTUsssssqqqqqqqq000000003NTU1NTU1LLLLLNKrTSqqqqtNNNNNNtNNTU1NSzUssss00qq0qqqqrTTTTTTTTTU1NTUs1LLLNNNKrTTTSqqq00000000001NTU1LNTU0000qtNNNKqqqtNNNNNNNNTU1NTUs1NNNNNKss1NNNK00qtK0000001NNTU0s000000qq000001NKrStNNNNK1NNNNStNNNNNKqtNNNNNNNK0000000rU0000rTTTTTSq00000rTTTTTTTTTTTTTTTTStNNNNKr/xAAUEQEAAAAAAAAAAAAAAAAAAACg/9oACAEDAQM/AAAf/9k=\" preserveAspectRatio=\"xMidYMid slice\"/><feColorMatrix in=\"DISPLACEMENT_MAP\" type=\"matrix\" values=\"0.3 0.3 0.3 0 0 0.3 0.3 0.3 0 0 0.3 0.3 0.3 0 0 0 0 0 1 0\" result=\"EDGE_INTENSITY\"/><feComponentTransfer in=\"EDGE_INTENSITY\" result=\"EDGE_MASK\"><feFuncA type=\"discrete\" tableValues=\"0 " + a * .05 + " 1\"/></feComponentTransfer><feOffset in=\"SourceGraphic\" dx=\"0\" dy=\"0\" result=\"CENTER_ORIGINAL\"/><feDisplacementMap in=\"SourceGraphic\" in2=\"DISPLACEMENT_MAP\" scale=\"" + scale + "\" xChannelSelector=\"R\" yChannelSelector=\"B\" result=\"RED_DISPLACED\"/><feColorMatrix in=\"RED_DISPLACED\" type=\"matrix\" values=\"1 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 1 0\" result=\"RED_CHANNEL\"/><feDisplacementMap in=\"SourceGraphic\" in2=\"DISPLACEMENT_MAP\" scale=\"" + (scale + a * .05 * k.displace) + "\" xChannelSelector=\"R\" yChannelSelector=\"B\" result=\"GREEN_DISPLACED\"/><feColorMatrix in=\"GREEN_DISPLACED\" type=\"matrix\" values=\"0 0 0 0 0 0 1 0 0 0 0 0 0 0 0 0 0 0 1 0\" result=\"GREEN_CHANNEL\"/><feDisplacementMap in=\"SourceGraphic\" in2=\"DISPLACEMENT_MAP\" scale=\"" + (scale + a * .1 * k.displace) + "\" xChannelSelector=\"R\" yChannelSelector=\"B\" result=\"BLUE_DISPLACED\"/><feColorMatrix in=\"BLUE_DISPLACED\" type=\"matrix\" values=\"0 0 0 0 0 0 0 0 0 0 0 0 1 0 0 0 0 0 1 0\" result=\"BLUE_CHANNEL\"/><feBlend in=\"GREEN_CHANNEL\" in2=\"BLUE_CHANNEL\" mode=\"screen\" result=\"GB_COMBINED\"/><feBlend in=\"RED_CHANNEL\" in2=\"GB_COMBINED\" mode=\"screen\" result=\"RGB_COMBINED\"/><feGaussianBlur in=\"RGB_COMBINED\" stdDeviation=\"" + Math.max(.1, .5 - a * .1) + "\" result=\"ABERRATED_BLURRED\"/><feComposite in=\"ABERRATED_BLURRED\" in2=\"EDGE_MASK\" operator=\"in\" result=\"EDGE_ABERRATION\"/><feComponentTransfer in=\"EDGE_MASK\" result=\"INVERTED_MASK\"><feFuncA type=\"table\" tableValues=\"1 0\"/></feComponentTransfer><feComposite in=\"CENTER_ORIGINAL\" in2=\"INVERTED_MASK\" operator=\"in\" result=\"CENTER_CLEAN\"/><feComposite in=\"EDGE_ABERRATION\" in2=\"CENTER_CLEAN\" operator=\"over\"/></filter></defs>";
	}
	var HOST_PROPS = [
		"position",
		"z-index",
		"clip-path",
		"background-color",
		"background-image"
	];
	var hosts = /* @__PURE__ */ new Map();
	var seq = 0;
	var effectiveNow = false;
	var knobsNow = { ...KNOB_DEFAULTS };
	var settleTimers = [];
	function sizeLens(svg, host) {
		const r = host.getBoundingClientRect();
		const w = Math.max(1, Math.round(r.width || host.offsetWidth || 0));
		const h = Math.max(1, Math.round(r.height || host.offsetHeight || 0));
		svg.setAttribute("width", String(w));
		svg.setAttribute("height", String(h));
		svg.style.width = w + "px";
		svg.style.height = h + "px";
	}
	function saveHostProps(host) {
		const out = {};
		HOST_PROPS.forEach((p) => {
			out[p] = [host.style.getPropertyValue(p), host.style.getPropertyPriority(p)];
		});
		return out;
	}
	function restoreHostProps(host, saved) {
		HOST_PROPS.forEach((p) => {
			const [value, priority] = saved[p];
			if (value) host.style.setProperty(p, value, priority);
			else host.style.removeProperty(p);
		});
	}
	function handOff(host, m) {
		restoreHostProps(host, m.saved);
		const cs = getComputedStyle(host);
		const clip = cs.clipPath && cs.clipPath !== "none" ? cs.clipPath : "";
		if (cs.position === "static") host.style.setProperty("position", "relative");
		if (cs.zIndex === "auto") host.style.setProperty("z-index", "0");
		if (clip) {
			host.style.setProperty("clip-path", "none");
			host.style.setProperty("background-color", "transparent");
			host.style.setProperty("background-image", "none");
			m.layer.style.setProperty("clip-path", clip);
			m.layer.setAttribute(CLIPPED_ATTR, "");
			m.clipped = true;
		} else {
			host.style.setProperty("background-color", "var(--gc-glass-tint)");
			m.layer.style.removeProperty("clip-path");
			m.layer.removeAttribute(CLIPPED_ATTR);
			m.clipped = false;
		}
	}
	function mount(host) {
		const id = "gc-lens-" + ++seq;
		const layer = document.createElement("span");
		layer.className = LAYER_CLASS;
		layer.setAttribute("aria-hidden", "true");
		layer.style.setProperty("--gc-glass-lens", "url(#" + id + ")");
		const svg = document.createElementNS(SVG_NS, "svg");
		svg.id = id + "-svg";
		svg.setAttribute("class", LENS_CLASS);
		svg.setAttribute("aria-hidden", "true");
		svg.setAttribute("style", "position:fixed;left:0;top:0;opacity:0.01;pointer-events:none;z-index:-1000");
		svg.innerHTML = lensMarkup(id, knobsNow);
		const m = {
			layer,
			svg,
			id,
			ro: null,
			saved: saveHostProps(host),
			clipped: false
		};
		document.body.appendChild(svg);
		sizeLens(svg, host);
		host.appendChild(layer);
		host.setAttribute(HOST_ATTR, "");
		handOff(host, m);
		if (typeof ResizeObserver === "function") {
			m.ro = new ResizeObserver(() => sizeLens(svg, host));
			m.ro.observe(host);
		}
		return m;
	}
	function unmount(host, m) {
		if (m.ro) m.ro.disconnect();
		m.svg.remove();
		m.layer.remove();
		host.removeAttribute(HOST_ATTR);
		restoreHostProps(host, m.saved);
	}
	function retune() {
		knobsNow = readKnobs();
		hosts.forEach((m) => {
			if (m) m.svg.innerHTML = lensMarkup(m.id, knobsNow);
		});
	}
	function reconcile() {
		hosts.forEach((m, host) => {
			if (effectiveNow && !m) hosts.set(host, mount(host));
			else if (!effectiveNow && m) {
				unmount(host, m);
				hosts.set(host, null);
			}
		});
	}
	function settle() {
		settleTimers.forEach((t) => clearTimeout(t));
		settleTimers = [];
		const pass = () => {
			retune();
			hosts.forEach((m, host) => {
				if (m) handOff(host, m);
			});
		};
		pass();
		[
			150,
			400,
			900,
			2e3,
			4e3
		].forEach((ms) => {
			settleTimers.push(window.setTimeout(pass, ms));
		});
	}
	function glassAttach(el) {
		if (!(el instanceof HTMLElement)) return;
		if (hosts.has(el)) return;
		hosts.set(el, effectiveNow ? mount(el) : null);
	}
	function glassDetach(el) {
		if (!(el instanceof HTMLElement)) return;
		const m = hosts.get(el);
		if (m) unmount(el, m);
		hosts.delete(el);
	}
	function glassLayers() {
		let n = 0;
		hosts.forEach((m) => {
			if (m) n++;
		});
		return n;
	}
	/** called by the loader: the effective state changed (toggle, clamp) */
	function glassRendererSetEffective(effective) {
		effectiveNow = effective;
		if (effective) knobsNow = readKnobs();
		reconcile();
	}
	/** called by the loader on every apply (theme/shape): re-evaluate hand-offs */
	function glassRendererSettle() {
		if (typeof window === "undefined") return;
		settle();
	}
	//#endregion
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
		return "https://gc.sol.pharos.zone";
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
		glassRendererSettle();
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
		glassRendererSetEffective(effective);
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
		glassAttach,
		glassDetach,
		glassLayers,
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
