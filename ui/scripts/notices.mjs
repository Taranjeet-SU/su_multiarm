// Writes python/su_multi_arm_ui/THIRD_PARTY_NOTICES.txt: the licence text of every runtime package
// bundled into the static assets (the dependencies in package.json and their runtime dependencies).
import { existsSync, readFileSync, readdirSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

const root = fileURLToPath(new URL("..", import.meta.url));
const pkg = JSON.parse(readFileSync(join(root, "package.json"), "utf8"));
const seen = new Map();
const queue = Object.keys(pkg.dependencies ?? {});
while (queue.length) {
  const name = queue.shift();
  if (seen.has(name)) continue;
  const dir = join(root, "node_modules", name);
  const meta = JSON.parse(readFileSync(join(dir, "package.json"), "utf8"));
  const licenceFile = readdirSync(dir).find((f) => /^(licen[cs]e|copying)(\.|$)/i.test(f));
  seen.set(name, { version: meta.version, license: meta.license ?? "UNKNOWN", text: licenceFile ? readFileSync(join(dir, licenceFile), "utf8").trim() : "(no licence file shipped)" });
  for (const dep of Object.keys(meta.dependencies ?? {})) if (existsSync(join(root, "node_modules", dep))) queue.push(dep);
}
const parts = ["Third-party software bundled in su-multi-arm-ui (static JavaScript assets).", ""];
for (const [name, info] of [...seen].sort(([a], [b]) => a.localeCompare(b))) {
  parts.push("=".repeat(78), `${name} ${info.version} — ${info.license}`, "=".repeat(78), info.text, "");
}
writeFileSync(join(root, "python", "su_multi_arm_ui", "THIRD_PARTY_NOTICES.txt"), parts.join("\n"));
console.log(`notices: ${seen.size} packages`);
