// Synthetic checked-answer browser regression. Serve e2e/serve_synthetic.py first; this script
// intercepts only answer SSE, so it exercises the built UI without making any model calls.
// node e2e/answer_markdown.mjs <ui URL> <synthetic token> <artifact directory>
import { mkdirSync } from "node:fs";
import { chromium, expect } from "@playwright/test";

const [url, token, out] = process.argv.slice(2);
if (!url || !token || !out) throw new Error("usage: answer_markdown.mjs <ui URL> <token> <artifact directory>");
mkdirSync(out, { recursive: true });
const id = "aggregate:attention";
const answer = `## Needs attention

**Investigate worker timeouts and gateway latency.** These findings cover the sampled window.

| Issue | Supporting evidence | Suggested next check |
| :--- | ---: | :--- |
| Worker timeouts | 12 parsed errors [${id}] | Inspect the retry queue and recent deployment changes. |
| Gateway latency | 7 slow responses [${id}] | Check upstream health and timeout settings. |

### Follow-up

- Counts are approximate because these logs are sampled.
- Suggested checks are advice, not established causes.

Use \`timeout_ms\` when checking configuration.

\`\`\`text
Inspect configuration before making a change.
\`\`\`
`;
const result = {
  answer: { answer, citations: [id], claims: [], insufficient_evidence: false, short_answer: null },
  retrieval: { query: "What needs my attention?", generation: "synthetic-markdown", hits: [], arm_hits: {}, unsupported_constraints: [], trace: {}, aggregates: [{
    id, operation: "group_count", field: "service", exactness: "approximate", rendering: "Parsed sample: worker 12 errors; gateway 7 slow responses (approximate).", denominator: 19, basis: ["synthetic sampled logs"],
    rows: [{ service: "worker", issue: "timeouts", lines: 12 }, { service: "gateway", issue: "slow responses", lines: 7 }],
  }] }, trace: {}, unsupported_constraints: [], coverage: null, violations: [], contract: "passed",
};
const browser = await chromium.launch();
try {
  const page = await browser.newPage({ viewport: { width: 1360, height: 1000 } });
  const problems = [];
  page.on("pageerror", (error) => problems.push(error.message));
  await page.addInitScript(() => {
    document.addEventListener("securitypolicyviolation", (event) => {
      window.__cspViolations = [...(window.__cspViolations ?? []), `${event.violatedDirective} ${event.blockedURI}`];
    });
  });
  await page.route("**/v2/answer/stream", (route) => route.fulfill({ status: 200, contentType: "text/event-stream", body: `event: done\ndata: ${JSON.stringify({ seq: 1, event: "done", result })}\n\n` }));
  await page.goto(url);
  await page.locator("#token").first().fill(token);
  await page.getByRole("button", { name: "Use token" }).first().click();
  await page.getByTestId("dataset").filter({ hasText: "generation" }).waitFor();
  const today = await page.evaluate(() => {
    const now = new Date();
    return `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}-${String(now.getDate()).padStart(2, "0")}`;
  });
  await expect(page.locator("#as-of")).toHaveValue(today);
  await expect(page.getByRole("navigation", { name: "views" }).getByRole("link")).toHaveText(["Overview", "Ask", "Graph", "Charts", "Log shortcuts", "Patterns", "Communities", "Runs & spend", "Settings"]);
  await page.locator("#question").fill("What needs my attention?");
  await page.getByRole("button", { name: "Ask", exact: true }).click();
  const text = page.getByTestId("answer-text");
  await expect(text.getByRole("heading", { name: "Needs attention" })).toBeVisible();
  await expect(text.getByRole("table").getByRole("row")).toHaveCount(3);
  await expect(text.getByRole("listitem")).toHaveCount(2);
  await expect(page.getByTestId("answer-numbers").getByRole("table")).toBeVisible();
  await page.screenshot({ path: `${out}/answer-desktop.png`, fullPage: true });
  await text.getByRole("button", { name: `Show evidence ${id}` }).first().click();
  await expect(page.locator(`[data-evidence-id="${id}"]`)).toBeVisible();
  await page.setViewportSize({ width: 390, height: 844 });
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.screenshot({ path: `${out}/answer-mobile.png`, fullPage: true });
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
  expect(overflow).toBeLessThanOrEqual(1);
  expect(await text.locator("[style]").count()).toBe(0);
  await page.setViewportSize({ width: 1360, height: 1000 });
  expect(await page.evaluate(() => window.__cspViolations ?? [])).toEqual([]);
  expect(problems).toEqual([]);
  console.log("Answer Markdown: desktop/mobile, table, lists, citations, today's date, standard navigation and CSP passed.");
} finally {
  await browser.close();
}
