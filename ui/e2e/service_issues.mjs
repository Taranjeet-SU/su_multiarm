// Real synthetic-engine answer path; no model calls for the typed service/issue aggregation.
import { mkdirSync } from "node:fs";
import { chromium, expect } from "@playwright/test";
const [url, token, out, asOf = "2026-03-10", dimension = "services"] = process.argv.slice(2);
if (!url || !out) throw new Error("usage: service_issues.mjs <url> <token or -> <artifacts> [as-of] [services|clients]");
mkdirSync(out, { recursive: true });
const browser = await chromium.launch();
try {
  const page = await browser.newPage({ viewport: { width: 1360, height: 1000 } });
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.goto(`${url}#/ask`);
  if (token !== "-") {
    await page.locator("#token").first().fill(token);
    await page.getByRole("button", { name: "Use token" }).first().click();
  }
  await page.locator("#as-of").fill(asOf);
  await page.locator("#question").fill(`which ${dimension} failed in last 3 days and what was the issue`);
  await page.getByRole("button", { name: "Ask", exact: true }).click();
  const answer = page.getByTestId("answer-text");
  await expect(answer.getByRole("table")).toBeVisible();
  await expect(answer.getByRole("columnheader")).toHaveText([dimension === "clients" ? "Client" : "Service", "Error type", "Error count"]);
  await expect(page.getByTestId("answer-card").getByRole("table")).toHaveCount(1);
  const details = answer.locator("details");
  expect(await details.getAttribute("open")).toBeNull();
  await page.screenshot({ path: `${out}/${dimension}-issues-desktop.png`, fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.screenshot({ path: `${out}/${dimension}-issues-mobile.png`, fullPage: true });
  expect(await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth)).toBeLessThanOrEqual(1);
  await details.locator("summary").click();
  await expect(details).toHaveAttribute("open", "");
  expect(errors).toEqual([]);
  console.log("Issue answer: three columns, one table, collapsed supporting details, mobile and desktop passed.");
} finally { await browser.close(); }
