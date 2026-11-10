// Browser smoke over the real grouped log tool; uses synthetic gold data, no model calls.
import { mkdirSync } from "node:fs";
import { chromium, expect } from "@playwright/test";
const [url, token, out] = process.argv.slice(2);
if (!url || !out) throw new Error("usage: client_issue_graph.mjs <url> <token or -> <artifacts>");
mkdirSync(out, { recursive: true });
const browser = await chromium.launch();
try {
  const page = await browser.newPage({ viewport: { width: 1440, height: 1100 } });
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.goto(`${url}#/graph`);
  if (token !== "-") {
    await page.locator("#token").first().fill(token);
    await page.getByRole("button", { name: "Use token" }).first().click();
  }
  await expect(page.getByRole("heading", { name: "Clients, services & issues" })).toBeVisible();
  await expect(page.getByTestId("graph-canvas")).toBeVisible();
  const table = page.getByRole("table", { name: "Client and service issue evidence" });
  await expect(table.getByRole("columnheader")).toHaveText(["Client", "Service", "Issue", "Severity", "Error count", "Supporting pattern"]);
  expect(await table.locator("tbody tr").count()).toBeGreaterThan(0);
  expect(await page.getByTestId("graph-canvas").locator("canvas").count()).toBeGreaterThan(0);
  await page.screenshot({ path: `${out}/client-issue-graph-desktop.png`, fullPage: true });
  const clients = await page.locator("#graph-clients option").evaluateAll((options) => options.map((option) => option.value));
  expect(clients.length).toBeGreaterThan(1);
  const response = page.waitForResponse((reply) => reply.url().endsWith("/v2/tools/log_top") && reply.request().postDataJSON()?.tenant === clients[1]);
  await page.locator("#graph-client").fill(clients[1]);
  await page.getByRole("button", { name: "Apply client" }).click();
  expect((await response).status()).toBe(200);
  await expect(page.getByText("Loading client and service issues…")).toHaveCount(0);
  const clientCells = await table.locator("tbody tr td:first-child").allTextContents();
  expect(clientCells.every((name) => name === clients[1])).toBe(true);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.screenshot({ path: `${out}/client-issue-graph-mobile.png`, fullPage: true });
  expect(await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth)).toBeLessThanOrEqual(1);
  expect(errors).toEqual([]);
  console.log(`Client issue graph: real canvas, grouped evidence, client filter, desktop/mobile; ${clientCells.length} filtered rows.`);
} finally { await browser.close(); }
