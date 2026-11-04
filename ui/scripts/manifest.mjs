// Writes python/su_multi_arm_ui/static/manifest.json after ``vite build``: the package version and
// the sha256 of every bundled file, so the server (and a reviewer) can identify exactly what is served.
import { createHash } from "node:crypto";
import { readFileSync, readdirSync, statSync, writeFileSync } from "node:fs";
import { join, relative } from "node:path";
import { fileURLToPath } from "node:url";

const root = fileURLToPath(new URL("..", import.meta.url));
const staticDir = join(root, "python", "su_multi_arm_ui", "static");
const pkg = JSON.parse(readFileSync(join(root, "package.json"), "utf8"));

function walk(dir) {
  return readdirSync(dir).flatMap((name) => {
    const path = join(dir, name);
    return statSync(path).isDirectory() ? walk(path) : [path];
  });
}

const files = {};
for (const path of walk(staticDir).sort()) {
  const rel = relative(staticDir, path).split("\\").join("/");
  if (rel === "manifest.json") continue;
  files[rel] = createHash("sha256").update(readFileSync(path)).digest("hex");
}
const content = createHash("sha256").update(JSON.stringify(files)).digest("hex");
writeFileSync(join(staticDir, "manifest.json"), JSON.stringify({ name: pkg.name, version: pkg.version, content_sha256: content, files }, null, 2) + "\n");
console.log(`manifest: ${Object.keys(files).length} files, content ${content.slice(0, 12)}`);
