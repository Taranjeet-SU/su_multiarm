// Screenshot tour of the UI against the gold fixture (``serve_synthetic.py --fixture gold``):
// overview dashboard, a streamed log question with its timeline and answer, evidence tabs, graph
// explorer, pattern review, runs. Fails on any CSP violation or page error.
//
//   node ui/e2e/tour.mjs http://127.0.0.1:18100/ui/ smoke-token <screenshot dir>
import { mkdirSync } from "node:fs";
import { chromium } from "@playwright/test";

const [url, token, out] = process.argv.slice(2);
if (!url || !token || !out) throw new Error("usage: tour.mjs <ui url> <token> <screenshot dir>");
mkdirSync(out, { recursive: true });

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1440, height: 960 } });
const problems = [];
page.on("pageerror", (e) => problems.push(`pageerror: ${e.message}`));
page.on("console", (m) => m.type() === "error" && !/40[13]/.test(m.text()) && problems.push(`console: ${m.text()}`));
await page.addInitScript(() => {
  document.addEventListener("securitypolicyviolation", (e) => {
    window.__cspViolations = [...(window.__cspViolations ?? []), `${e.violatedDirective} ${e.blockedURI}`];
  });
});
const shot = async (name) => page.screenshot({ path: `${out}/${name}.png`, fullPage: true });

await page.goto(url);
await page.locator("#token").first().fill(token);
await page.getByRole("button", { name: "Use token" }).first().click();
await page.getByTestId("dataset").waitFor();

await page.goto(`${url}#/overview`);
await page.getByTestId("panel-Trend (last 10 days)").locator("svg").first().waitFor({ timeout: 30000 });
await page.getByTestId("panel-heatmap").locator("table").waitFor({ timeout: 30000 });
await page.waitForTimeout(500);
await shot("10-overview");

await page.goto(`${url}#/ask`);
await page.locator("#question").fill("Top errors for svc-api yesterday");
await page.getByRole("button", { name: "use last composed day" }).click().catch(() => undefined);
await page.getByRole("button", { name: "Ask", exact: true }).click();
await page.getByTestId("timeline").waitFor();
await page.waitForTimeout(250);
await shot("11-ask-streaming");
await page.getByTestId("answer-card").waitFor({ timeout: 30000 });
await shot("12-ask-answer-timeline");
await page.getByRole("tab", { name: /Structured/ }).click();
await shot("13-structured-tab");
await page.getByRole("tab", { name: /Lexical/ }).click();
await shot("14-lexical-tab");

await page.locator("#question").fill("What does dependency.connect_failed mean?");
await page.getByRole("button", { name: "Ask", exact: true }).click();
await page.getByTestId("answer-card").waitFor({ timeout: 30000 });
await page.getByRole("tab", { name: /Graph/ }).click();
await page.waitForTimeout(800);
await shot("15-graph-tab");

await page.goto(`${url}#/graph`);
await page.locator("#entity").fill("svc-api");
await page.getByRole("button", { name: "Explore" }).click();
await page.waitForTimeout(1500);
await shot("16-graph-explorer");

await page.goto(`${url}#/logs`);
await page.waitForTimeout(800);
await shot("17-log-shortcuts");

await page.goto(`${url}#/patterns`);
await page.getByTestId("proposal").first().waitFor({ timeout: 15000 });
await shot("18-pattern-review");

await page.goto(`${url}#/runs`);
await page.waitForTimeout(1200);
await shot("19-runs-spend");

const csp = await page.evaluate(() => window.__cspViolations ?? []);
await browser.close();
if (csp.length || problems.length) {
  console.error(JSON.stringify({ csp, problems }, null, 2));
  process.exit(1);
}
console.log(`ok: tour screenshots in ${out}; 0 CSP violations; 0 page errors`);
