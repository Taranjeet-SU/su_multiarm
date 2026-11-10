// Three-stage metrics over real synthetic source/parse/compose data. No model calls.
import { mkdirSync } from "node:fs";
import { chromium, expect } from "@playwright/test";
const [url, token, out] = process.argv.slice(2);
if (!url || !out) throw new Error("usage: coverage_stages.mjs <url> <token or -> <artifacts>");
mkdirSync(out, { recursive: true });
const browser = await chromium.launch();
try {
  const page = await browser.newPage({ viewport: { width: 1440, height: 1100 } });
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.goto(`${url}#/overview`);
  if (token !== "-") {
    await page.locator("#token").first().fill(token);
    await page.getByRole("button", { name: "Use token" }).first().click();
  }
  const panel = page.getByTestId("panel-Collection and extraction");
  await expect(panel.getByRole("table")).toBeVisible();
  const headers = await panel.getByRole("columnheader").allTextContents();
  expect(headers.slice(0, 4)).toEqual(["Service", "Completeness", "Scanned", "Extracted"]);
  if (headers.length > 4) expect(headers[4]).toBe("Incident matches / context");
  await expect(panel.getByText("Indexed scope only.")).toBeVisible();
  expect(await panel.getByRole("progressbar").count()).toBeGreaterThan(3);
  const recognition = panel.locator("details");
  expect(await recognition.getAttribute("open")).toBeNull();
  await panel.scrollIntoViewIfNeeded();
  await page.screenshot({ path: `${out}/coverage-desktop.png`, fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.screenshot({ path: `${out}/coverage-mobile.png`, fullPage: true });
  expect(await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth)).toBeLessThanOrEqual(1);
  expect(errors).toEqual([]);
  console.log("Coverage stages: source counters, three metrics, optional incident scope, collapsed recognition diagnostic, desktop/mobile passed.");
} finally { await browser.close(); }
