// Node test runner for the pure frontend helpers (2026-09-14).
//
// The helpers under src/lib are plain TypeScript with no DOM at module
// scope, so they can run under Node's built-in test runner once bundled.
// Bundling uses the esbuild Vite already ships (no new dependency): each
// tests/*.test.ts is bundled to a temp dir and `node --test` runs the lot.
//
//   cd frontend && npm test          # or: node tests/run.mjs
//
// tests/test_capacity_navigation.py::test_frontend_node_unit_tests_pass
// runs this from pytest and asserts a clean TAP summary.

import { build } from "esbuild";
import { spawnSync } from "node:child_process";
import { mkdtempSync, readdirSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const entries = readdirSync(here)
  .filter((name) => name.endsWith(".test.ts"))
  .map((name) => join(here, name));

if (!entries.length) {
  console.error("no tests/*.test.ts files found");
  process.exit(1);
}

const outDir = mkdtempSync(join(tmpdir(), "ai-usage-frontend-tests-"));
try {
  await build({
    entryPoints: entries,
    bundle: true,
    platform: "node",
    format: "esm",
    target: "node22",
    outdir: outDir,
    outExtension: { ".js": ".mjs" },
    logLevel: "silent",
  });
  const files = readdirSync(outDir).filter((name) => name.endsWith(".test.mjs")).map((name) => join(outDir, name));
  const result = spawnSync(
    process.execPath,
    ["--test", "--test-reporter=tap", ...files],
    { stdio: "inherit" },
  );
  process.exit(result.status === null ? 1 : result.status);
} finally {
  rmSync(outDir, { recursive: true, force: true });
}
