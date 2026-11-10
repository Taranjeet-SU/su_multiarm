// Real API browser check; artifacts must be outside the public source checkout.
import { mkdirSync, writeFileSync } from "node:fs";
import { chromium, expect } from "@playwright/test";
const [url, token, out, asOf = "2026-03-06"] = process.argv.slice(2);
if (!url || !out) throw new Error("usage: outage_investigation.mjs <url> <token or -> <external artifacts> [as-of]");
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
  await page.locator("#question").fill("last 3 day outage");
  const response = page.waitForResponse((r) => r.url().endsWith("/v2/answer/stream"));
  await page.getByRole("button", { name: "Ask", exact: true }).click();
  const answer = page.getByTestId("answer-text");
  await expect(answer.getByRole("table")).toBeVisible();
  await expect(answer.getByRole("columnheader")).toHaveText(["Client", "Services", "Shared issue patterns", "Observed interval (UTC)", "Evidence"]);
  await expect(page.getByText("Answer contract", { exact: true })).toHaveCount(0);
  const wire = await (await response).text();
  const events = wire.split("\n").filter((line) => line.startsWith("data: ")).map((line) => JSON.parse(line.slice(6)));
  const done = events.find((event) => event.event === "done");
  expect(done).toBeTruthy();
  expect(events.some((event) => event.event === "error")).toBe(false);
  expect(Object.keys(done.result.retrieval.arm_hits).sort()).toEqual(["community", "graph"]);
  expect(done.result.trace.model_calls).toEqual([]);
  writeFileSync(`${out}/answer.json`, JSON.stringify(done.result, null, 2));
  await page.screenshot({ path: `${out}/outage-desktop.png`, fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.screenshot({ path: `${out}/outage-mobile.png`, fullPage: true });
  expect(await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth)).toBeLessThanOrEqual(1);
  await answer.getByRole("button", { name: /Show evidence/ }).first().click();
  expect(errors).toEqual([]);
  console.log(JSON.stringify({ table: true, arms: Object.keys(done.result.retrieval.arm_hits), model_calls: 0, hits: done.result.retrieval.hits.length, evidence_click: true, mobile_overflow: false, page_errors: 0 }));
} finally { await browser.close(); }
