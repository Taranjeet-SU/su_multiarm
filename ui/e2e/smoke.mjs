// Browser smoke of the UI against a running server (``ui/e2e/serve_synthetic.py``): enters the token,
// asks a question, waits for the final answer and the contract badge, opens the evidence tabs, the
// graph explorer and a grouped chart, saves screenshots and fails on any CSP violation or page error.
//
//   node ui/e2e/smoke.mjs http://127.0.0.1:18099/ui/ smoke-token <screenshot dir>
import { mkdirSync } from "node:fs";
import { chromium } from "@playwright/test";

const [url, token, out] = process.argv.slice(2);
if (!url || !token || !out) throw new Error("usage: smoke.mjs <ui url> <token> <screenshot dir>");
mkdirSync(out, { recursive: true });

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1360, height: 900 } });
const problems = [];
page.on("pageerror", (e) => problems.push(`pageerror: ${e.message}`));
page.on("console", (m) => m.type() === "error" && !/401/.test(m.text()) && problems.push(`console: ${m.text()}`));
await page.addInitScript(() => {
  document.addEventListener("securitypolicyviolation", (e) => {
    window.__cspViolations = [...(window.__cspViolations ?? []), `${e.violatedDirective} ${e.blockedURI}`];
  });
});

await page.goto(url);
await page.locator("#token").first().fill(token);
await page.getByRole("button", { name: "Use token" }).first().click();
await page.getByTestId("dataset").filter({ hasText: "generation" }).waitFor();
await page.locator("#question").fill("db-client failed to connect redis");
await page.getByRole("button", { name: "Ask", exact: true }).click();
await page.getByTestId("answer-card").waitFor({ timeout: 30000 });
await page.screenshot({ path: `${out}/01-ask-answer.png`, fullPage: true });
await page.locator(".chip.cite").first().click();
await page.screenshot({ path: `${out}/02-citation-evidence.png`, fullPage: true });
await page.getByRole("tab", { name: /Graph/ }).click();
await page.screenshot({ path: `${out}/03-graph-tab.png`, fullPage: true });
await page.getByRole("tab", { name: /Trace/ }).click();
await page.screenshot({ path: `${out}/04-trace.png`, fullPage: true });

await page.goto(`${url}#/charts`);
await page.locator("#group-field").selectOption("service");
await page.getByRole("button", { name: "Chart" }).click();
await page.getByTestId("bar-chart").waitFor({ timeout: 15000 });
await page.screenshot({ path: `${out}/05-chart-structured.png`, fullPage: true });

await page.goto(`${url}#/graph`);
await page.locator("#entity").fill("redis");
await page.getByRole("button", { name: "Explore" }).click();
await page.getByTestId("graph-canvas").waitFor({ timeout: 15000 });
await page.waitForTimeout(800);
await page.screenshot({ path: `${out}/06-graph-explorer.png`, fullPage: true });

// Pattern review: proposals with examples and gate; the copy button shows the shlex-quoted command.
await page.goto(`${url}#/patterns`);
await page.getByTestId("proposal").first().waitFor({ timeout: 15000 });
await page.locator("#by").fill("reviewer");
await page.getByTestId("copy-promote").first().click();
await page.getByTestId("copied-command").waitFor({ timeout: 5000 });
const command = await page.getByTestId("copied-command").locator("pre").textContent();
if (!command?.startsWith("su-multi-arm --config=config.yaml patterns promote --by=reviewer")) problems.push(`unexpected command: ${command}`);
await page.screenshot({ path: `${out}/07-pattern-review.png`, fullPage: true });

await page.goto(`${url}#/runs`);
await page.getByText("Daily runs").waitFor();
await page.locator("details.hit").first().waitFor({ timeout: 15000 });
await page.screenshot({ path: `${out}/08-runs-spend.png`, fullPage: true });

await page.goto(`${url}#/communities`);
await page.waitForTimeout(500);
await page.screenshot({ path: `${out}/09-communities.png`, fullPage: true });

const csp = await page.evaluate(() => window.__cspViolations ?? []);
await browser.close();
if (csp.length || problems.length) {
  console.error(JSON.stringify({ csp, problems }, null, 2));
  process.exit(1);
}
console.log(`ok: 9 screenshots in ${out}; 0 CSP violations; 0 page errors`);
